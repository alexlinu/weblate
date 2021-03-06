# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2018 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from glob import glob
import tempfile
import os
import re
import shutil
import fnmatch

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.encoding import force_text
from django.db.models import Q
from django.utils.text import slugify

from weblate.lang.models import Language
from weblate.trans.models import SubProject, Project
from weblate.trans.formats import FILE_FORMATS
from weblate.trans.util import is_repo_link, path_separator
from weblate.trans.vcs import VCS_REGISTRY
from weblate.logger import LOGGER


class Command(BaseCommand):
    """Command for mass importing of repositories into Weblate."""
    help = 'imports projects with more components'

    def add_arguments(self, parser):
        super(Command, self).add_arguments(parser)
        parser.add_argument(
            '--name-template',
            default='%s',
            help=(
                'Python formatting string, transforming the filemask '
                'match to a project name'
            )
        )
        parser.add_argument(
            '--component-regexp',
            default=None,
            help=(
                'Regular expression to match component out of filename'
            )
        )
        parser.add_argument(
            '--base-file-template',
            default='',
            help=(
                'Python formatting string, transforming the filemask '
                'match to a monolingual base file name'
            )
        )
        parser.add_argument(
            '--file-format',
            default='auto',
            help='File format type, defaults to autodetection',
        )
        parser.add_argument(
            '--language-regex',
            default=None,
            help=(
                'Language filter regular expression to be used for created'
                ' components'
            ),
        )
        parser.add_argument(
            '--no-skip-duplicates',
            action='store_true',
            default=False,
            dest='duplicates',
            help=(
                'Avoid skipping duplicate component names/slugs. '
                'Use this if importing project with long names '
            )
        )
        parser.add_argument(
            '--license',
            default=None,
            help='License of imported components',
        )
        parser.add_argument(
            '--license-url',
            default=None,
            help='License URL of imported components',
        )
        parser.add_argument(
            '--vcs',
            default=settings.DEFAULT_VCS,
            help='Version control system to use',
        )
        parser.add_argument(
            '--push-url',
            default='',
            help='Set push URL for the project',
        )
        parser.add_argument(
            '--push-url-same',
            action='store_true',
            default=False,
            help='Set push URL for the project to same as pull',
        )
        parser.add_argument(
            '--disable-push-on-commit',
            action='store_false',
            default=settings.DEFAULT_PUSH_ON_COMMIT,
            dest='push_on_commit',
            help='Disable push on commit for created components',
        )
        parser.add_argument(
            '--push-on-commit',
            action='store_true',
            default=settings.DEFAULT_PUSH_ON_COMMIT,
            dest='push_on_commit',
            help='Enable push on commit for created components',
        )
        parser.add_argument(
            '--main-component',
            default=None,
            help=(
                'Define which component will be used as main - including full'
                ' VCS repository'
            )
        )
        parser.add_argument(
            'project',
            help='Existing project slug',
        )
        parser.add_argument(
            'repo',
            help='VCS repository URL',
        )
        parser.add_argument(
            'branch',
            help='VCS repository branch',
        )
        parser.add_argument(
            'filemask',
            help='File mask',
        )

    def __init__(self, *args, **kwargs):
        super(Command, self).__init__(*args, **kwargs)
        self.filemask = None
        self.component_re = None
        self.file_format = None
        self.language_regex = None
        self.license = None
        self.license_url = None
        self.main_component = None
        self.name_template = None
        self.base_file_template = None
        self.vcs = None
        self.push_url = None
        self.logger = LOGGER
        self.push_on_commit = True
        self._mask_regexp = None

    def format_string(self, template, match):
        """Format template string with match."""
        if '%s' in template:
            return template % match
        return template

    def get_name(self, path):
        """Return file name from patch based on filemask."""
        matches = self.match_regexp.match(path)
        if matches is None:
            self.logger.warning('Skipping %s', path)
            return None, None
        return matches.group('name'), matches.group('language')

    @property
    def match_regexp(self):
        """Return regexp for file matching"""
        if self.component_re is not None:
            return self.component_re
        if self._mask_regexp is None:
            match = fnmatch.translate(self.filemask)
            match = match.replace('.*.*', '(?P<name>[[WILDCARD]])')
            match = match.replace('.*', '(?P<language>[[WILDCARD]])', 1)
            match = match.replace('.*', '(?P=language)')
            match = match.replace('[[WILDCARD]]', '.*')
            self._mask_regexp = re.compile(match)
        return self._mask_regexp

    def checkout_tmp(self, project, repo, branch):
        """Checkout project to temporary location."""
        # Create temporary working dir
        workdir = tempfile.mkdtemp(dir=project.full_path)
        # Make the temporary directory readable by others
        os.chmod(workdir, 0o755)

        # Initialize git repository
        self.logger.info('Cloning git repository...')
        gitrepo = VCS_REGISTRY[self.vcs].clone(repo, workdir)
        self.logger.info('Updating working copy in git repository...')
        with gitrepo.lock:
            gitrepo.configure_branch(branch)

        return workdir

    def get_matching_files(self, repo):
        """Return relative path of matched files."""
        matches = glob(os.path.join(repo, self.filemask))
        return [
            path_separator(f.replace(repo, '')).strip('/') for f in matches
        ]

    def get_matching_subprojects(self, repo):
        """Scan the master repository for names matching our mask"""
        # Find matching files
        matches = self.get_matching_files(repo)
        self.logger.info('Found %d matching files', len(matches))

        if not matches:
            raise CommandError('Your mask did not match any files!')

        # Parse subproject names out of them
        names = set()
        langs = set()
        for match in matches:
            name, lang = self.get_name(match)
            if name:
                names.add(name)
                langs.add(lang)
        self.logger.info('Found %d subprojects', len(names))
        self.logger.info('Found %d languages', len(langs))

        # Do some basic sanity check on languages
        if Language.objects.filter(code__in=langs).count() == 0:
            raise CommandError(
                'None of matched languages exists, maybe you have '
                'mixed * and ** in the mask?'
            )

        return sorted(names)

    def find_usable_slug(self, name, slug_len, project):
        base = name[:slug_len - 4]
        for i in range(1, 1000):
            newname = '{0} {1:03d}'.format(base, i)
            slug = slugify(newname)
            subprojects = SubProject.objects.filter(
                Q(name=newname) | Q(slug=slug),
                project=project
            )
            if not subprojects.exists():
                return newname, slug
        raise CommandError(
            'Failed to find suitable name for {0}'.format(name)
        )

    def parse_options(self, repo, options):
        """Parse parameters"""
        self.filemask = options['filemask']
        self.vcs = options['vcs']
        if options['push_url_same']:
            self.push_url = repo
        else:
            self.push_url = options['push_url']
        self.file_format = options['file_format']
        self.language_regex = options['language_regex']
        self.main_component = options['main_component']
        self.name_template = options['name_template']
        self.license = options['license']
        self.license_url = options['license_url']
        self.push_on_commit = options['push_on_commit']
        self.base_file_template = options['base_file_template']
        if options['component_regexp']:
            try:
                self.component_re = re.compile(
                    options['component_regexp'],
                    re.MULTILINE | re.DOTALL
                )
            except re.error as error:
                raise CommandError(
                    'Failed to compile regular expression "{0}": {1}'.format(
                        options['component_regexp'], error
                    )
                )
            if ('name' not in self.component_re.groupindex or
                    'language' not in self.component_re.groupindex):
                raise CommandError(
                    'Component regular expression lacks named group "name"'
                    ' and/or "language"'
                )

        # Is file format supported?
        if self.file_format not in FILE_FORMATS:
            raise CommandError(
                'Invalid file format: {0}'.format(options['file_format'])
            )

        # Is vcs supported?
        if self.vcs not in VCS_REGISTRY:
            raise CommandError(
                'Invalid vcs: {0}'.format(options['vcs'])
            )

        # Do we have correct mask?
        if ('**' not in self.filemask
                or '*' not in self.filemask.replace('**', '')):
            raise CommandError(
                'You need to specify double wildcard '
                'for subproject part of the match!'
            )

    def handle(self, *args, **options):
        """Automatic import of project."""
        # Read params
        repo = options['repo']
        branch = options['branch']
        self.parse_options(repo, options)

        # Try to get project
        try:
            project = Project.objects.get(slug=options['project'])
        except Project.DoesNotExist:
            raise CommandError(
                'Project "{0}" not found, please create it first!'.format(
                    options['project']
                )
            )

        # We need to limit slug length to avoid problems with MySQL
        # silent truncation
        # pylint: disable=protected-access
        slug_len = SubProject._meta.get_field('slug').max_length
        name_len = SubProject._meta.get_field('name').max_length

        if is_repo_link(repo):
            sharedrepo = repo
            try:
                sub_project = SubProject.objects.get_linked(repo)
            except SubProject.DoesNotExist:
                raise CommandError(
                    'Component "{0}" not found, '
                    'please create it first!'.format(
                        repo
                    )
                )
            matches = self.get_matching_subprojects(
                sub_project.full_path,
            )
        else:
            matches, sharedrepo = self.import_initial(
                project, repo, branch, slug_len, name_len
            )

        # Create remaining subprojects sharing git repository
        for match in matches:
            name = self.format_string(self.name_template, match)[:name_len]
            template = self.format_string(self.base_file_template, match)
            slug = slugify(name)[:slug_len]
            subprojects = SubProject.objects.filter(
                Q(name=name) | Q(slug=slug),
                project=project
            )
            if subprojects.exists():
                if not options['duplicates']:
                    self.logger.warning(
                        'Component %s already exists, skipping',
                        name
                    )
                    continue
                else:
                    name, slug = self.find_usable_slug(
                        name, slug_len, project
                    )

            self.logger.info('Creating component %s', name)
            SubProject.objects.create(
                name=name,
                slug=slug,
                project=project,
                repo=sharedrepo,
                branch=branch,
                template=template,
                filemask=self.filemask.replace('**', match),
                **self.get_project_attribs()
            )

    def get_project_attribs(self):
        result = {
            'file_format': self.file_format,
            'vcs': self.vcs,
            'push_on_commit': self.push_on_commit,
        }
        optionals = (
            'license',
            'license_url',
            'language_regex'
        )
        for key in optionals:
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result

    def import_initial(self, project, repo, branch, slug_len, name_len):
        """Import the first repository of a project"""
        # Checkout git to temporary dir
        workdir = self.checkout_tmp(project, repo, branch)
        matches = self.get_matching_subprojects(workdir)

        # Create first subproject (this one will get full git repo)
        if self.main_component:
            if self.main_component not in matches:
                raise CommandError(
                    'Specified --main-component was not found in matches!'
                )
            match = force_text(self.main_component)
            matches.remove(self.main_component)
        else:
            match = matches.pop()
        name = self.format_string(self.name_template, match)[:name_len]
        template = self.format_string(self.base_file_template, match)
        slug = slugify(name)[:slug_len]

        if SubProject.objects.filter(project=project, slug=slug).exists():
            self.logger.warning(
                'Component %s already exists, skipping and using it '
                'as main component',
                name
            )
            shutil.rmtree(workdir)
            return matches, 'weblate://{0}/{1}'.format(project.slug, slug)

        self.logger.info('Creating component %s as main one', name)

        # Rename gitrepository to new name
        os.rename(
            workdir,
            os.path.join(project.full_path, slug)
        )

        SubProject.objects.create(
            name=name,
            slug=slug,
            project=project,
            push=self.push_url,
            repo=repo,
            branch=branch,
            template=template,
            filemask=self.filemask.replace('**', match),
            **self.get_project_attribs()
        )

        sharedrepo = 'weblate://{0}/{1}'.format(project.slug, slug)

        return matches, sharedrepo
