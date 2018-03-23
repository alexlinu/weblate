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

from __future__ import unicode_literals

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Div

from django import forms
from django.utils.translation import ugettext_lazy as _

from weblate.trans.formats import FILE_FORMAT_CHOICES
from weblate.utils.validators import validate_render, validate_re


class BaseAddonForm(forms.Form):
    def __init__(self, addon, instance=None, *args, **kwargs):
        self._addon = addon
        super(BaseAddonForm, self).__init__(*args, **kwargs)

    def save(self):
        self._addon.configure(self.cleaned_data)
        return self._addon.instance


class GenerateForm(BaseAddonForm):
    filename = forms.CharField(
        label=_('Name of generated file'),
        required=True,
    )
    template = forms.CharField(
        widget=forms.Textarea(),
        label=_('Content of generated file'),
        required=True,
    )

    def __init__(self, *args, **kwargs):
        super(GenerateForm, self).__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.layout = Layout(
            Field('filename'),
            Field('template'),
            Div(template='addons/generate_help.html'),
        )

    def test_render(self, value):
        translation = self._addon.instance.component.translation_set.all()[0]
        validate_render(value, translation=translation)

    def clean_filename(self):
        self.test_render(self.cleaned_data['filename'])
        return self.cleaned_data['filename']

    def clean_template(self):
        self.test_render(self.cleaned_data['template'])
        return self.cleaned_data['template']


class GettextCustomizeForm(BaseAddonForm):
    width = forms.ChoiceField(
        label=_('Long lines wrapping'),
        choices=[
            (77, _('Wrap lines at 77 chars and at newlines (default gettext behavior)')),
            (65535, _('Only wrap lines at newlines (gettext behavior with --no-wrap)')),
            (-1, _('No line wrapping')),
        ],
        required=True,
        initial=77,
    )


class JSONCustomizeForm(BaseAddonForm):
    sort_keys = forms.BooleanField(
        label=_('Sort JSON keys'),
        required=False
    )
    indent = forms.IntegerField(
        label=_('JSON indentation'),
        min_value=0,
        initial=4,
        required=True,
    )


class DiscoveryForm(BaseAddonForm):
    match = forms.CharField(
        label=_('Regular expression to match translation files'),
        required=True,
    )
    file_format = forms.ChoiceField(
        label=_('File format'),
        choices=FILE_FORMAT_CHOICES,
        initial='auto',
        required=True,
        help_text=_(
            'Automatic detection might fail for some formats '
            'and is slightly slower.'
        ),
    )
    name_template = forms.CharField(
        label=_('Customise the component name'),
        initial='{{ component }}',
        required=True,
    )
    base_file_template = forms.CharField(
        label=_('Define the monolingual base filename'),
        initial='',
        required=False,
        help_text=_('Keep empty for bilingual translation files.'),
    )
    remove = forms.BooleanField(
        label=_('Remove components for non existing files'),
        required=False
    )
    confirm = forms.BooleanField(
        label=_('Please review the above matches and confirm the selection'),
        required=False
    )
    preview = forms.BooleanField(
        required=False,
        widget=forms.HiddenInput,
    )

    def __init__(self, *args, **kwargs):
        super(DiscoveryForm, self).__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.layout = Layout(
            Field('match'),
            Field('file_format'),
            Field('name_template'),
            Field('base_file_template'),
            Field('remove'),
            Field('preview'),
            Div(template='addons/discovery_help.html'),
        )
        if self.is_bound:
            # Here we do not care about result
            self.is_valid()
            if self.cleaned_data['show_preview']:
                self.helper.layout.insert(
                    0,
                    Field('confirm'),
                )
                self.helper.layout.insert(
                    0,
                    Div(template='addons/discovery_preview.html'),
                )

    def clean(self):
        self.cleaned_data['show_preview'] = self.cleaned_data['preview']
        if self.cleaned_data['preview']:
            if not self.cleaned_data['confirm']:
                raise forms.ValidationError(
                    _('Please confirm matched components.')
                )
            self.cleaned_data['preview'] = False
            self.cleaned_data['confirm'] = False
        else:
            self.cleaned_data['preview'] = True
            self.cleaned_data['confirm'] = False

    def clean_match(self):
        match = self.cleaned_data['match']
        validate_re(match, ('component', 'language'))
        return match

    def test_render(self, value):
        validate_render(value, component='test')

    def clean_name_template(self):
        self.test_render(self.cleaned_data['name_template'])
        return self.cleaned_data['name_template']

    def clean_base_file_template(self):
        self.test_render(self.cleaned_data['base_file_template'])
        return self.cleaned_data['base_file_template']
