#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

__all__ = (
    'SocaInputParamCliOptions',
    'SocaUserInputParamExposeOptions',
    'SocaUserInputParamType',
    'SocaUserInputChoice',
    'SocaUserInputRange',
    'SocaUserInputCondition',
    'SocaUserInputHandlers',
    'SocaUserInputParamCondition',
    'SocaUserInputValidate',
    'SocaUserInputParamMetadata',
    'SocaInputParamValidationEntry',
    'SocaInputParamValidationResult',
    'SocaUserInputGroupMetadata',
    'SocaUserInputSectionReview',
    'SocaUserInputSectionMetadata',
    'SocaUserInputModuleMetadata',
    'SocaUserInputTag',
    'SocaInputParamSpec'
)

from ideadatamodel import SocaBaseModel
from ideadatamodel.model_utils import ModelUtils

from pydantic import Field
from typing import Optional, List, Union, Any, Dict
from enum import Enum


class SocaInputParamCliOptions(SocaBaseModel):
    long_name: Optional[str] = Field(default=None)
    short_name: Optional[str] = Field(default=None)
    required: Optional[str] = Field(default=None)
    help_text: Optional[str] = Field(default=None)


class SocaUserInputParamExposeOptions(SocaBaseModel):
    cli: Optional[SocaInputParamCliOptions] = Field(default=None)
    web_app: Optional[bool] = Field(default=None)


# noinspection HardcodedPassword
class SocaUserInputParamType(str, Enum):
    TEXT = 'text'
    PASSWORD = 'password'
    NEW_PASSWORD = 'new-password'
    PATH = 'path'
    CONFIRM = 'confirm'
    SELECT = 'select'
    RAW_SELECT = 'raw_select'
    CHECKBOX = 'checkbox'
    AUTOCOMPLETE = 'autocomplete'
    SELECT_OR_TEXT = 'select_or_text'
    CHOICES = 'choices'
    AUTO = 'auto'
    IMAGE_UPLOAD = 'image_upload'
    HEADING1 = 'heading1'
    HEADING2 = 'heading2'
    HEADING3 = 'heading3'
    HEADING4 = 'heading4'
    HEADING5 = 'heading5'
    HEADING6 = 'heading6'
    PARAGRAPH = 'paragraph'
    CODE = 'code'
    DATEPICKER = 'datepicker'

    def __str__(self):
        return self.value


class SocaUserInputChoice(SocaBaseModel):
    title: Optional[str] = Field(default=None)
    value: Optional[Any] = Field(default=None)
    disabled: Optional[bool] = Field(default=None)
    checked: Optional[bool] = Field(default=None)
    options: Optional[List['SocaUserInputChoice']] = Field(default=None)
    description: Optional[str] = Field(default=None)

    def __repr__(self):
        return self.value

    def __str__(self):
        return self.value


class SocaUserInputRange(SocaBaseModel):
    type_: Optional[str] = Field(alias='type', default=None)
    from_: Optional[List[Union[Any]]] = Field(alias='from', default=None)
    to: Optional[List[Union[Any]]] = Field(default=None)


class SocaUserInputCondition(SocaBaseModel):
    eq: Optional[Any] = Field(default=None)
    not_eq: Optional[Any] = Field(default=None)
    in_: Optional[Union[str, List[Any]]] = Field(alias='in', default=None)
    not_in: Optional[Union[str, List[Any]]] = Field(default=None)
    gt: Optional[Union[Any]] = Field(default=None)
    gte: Optional[Union[Any]] = Field(default=None)
    lt: Optional[Union[Any]] = Field(default=None)
    lte: Optional[Union[Any]] = Field(default=None)
    min: Optional[Union[Any]] = Field(default=None)
    max: Optional[Union[Any]] = Field(default=None)
    range: Optional[SocaUserInputRange] = Field(default=None)
    not_in_range: Optional[SocaUserInputRange] = Field(default=None)
    regex: Optional[str] = Field(default=None)
    not_regex: Optional[str] = Field(default=None)
    exact: Optional[str] = Field(default=None)
    starts_with: Optional[str] = Field(default=None)
    ends_with: Optional[str] = Field(default=None)
    empty: Optional[bool] = Field(default=None)
    not_empty: Optional[bool] = Field(default=None)
    contains: Optional[Any] = Field(default=None)
    not_contains: Optional[Any] = Field(default=None)


class SocaUserInputHandlers(SocaBaseModel):
    class_: Optional[str] = Field(alias='class', default=None)
    choices: Optional[str] = Field(default=None)
    default: Optional[str] = Field(default=None)
    validate_: Optional[str] = Field(alias='validate', default=None)
    autocomplete: Optional[str] = Field(default=None)
    filter: Optional[str] = Field(default=None)


class SocaUserInputParamCondition(SocaUserInputCondition):
    param: Optional[str] = Field(default=None)
    and_: Optional[List['SocaUserInputParamCondition']] = Field(alias='and', default=None)
    or_: Optional[List['SocaUserInputParamCondition']] = Field(alias='or', default=None)


SocaUserInputParamCondition.model_rebuild()


class SocaUserInputValidate(SocaUserInputCondition):
    required: Optional[bool] = Field(default=None)
    auto_prefix: Optional[str] = Field(default=None)


class SocaUserInputParamMetadata(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    template: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    prompt: Optional[bool] = Field(default=None)
    description: Optional[str] = Field(default=None)
    description2: Optional[str] = Field(default=None)
    help_text: Optional[str] = Field(default=None)
    param_type: Optional[SocaUserInputParamType] = Field(default=None)
    data_type: Optional[str] = Field(default=None)
    custom_type: Optional[str] = Field(default=None)
    multiple: Optional[bool] = Field(default=None)
    multiline: Optional[bool] = Field(default=None)
    auto_enter: Optional[bool] = Field(default=None)
    auto_focus: Optional[bool] = Field(default=None)
    unique: Optional[bool] = Field(default=None)
    default: Optional[Union[List, Any]] = Field(default=None)
    readonly: Optional[bool] = Field(default=None)
    validate_: Optional[SocaUserInputValidate] = Field(alias='validate', default=None)
    choices: Optional[List[SocaUserInputChoice]] = Field(default=None)
    choices_meta: Optional[Dict[str, Any]] = Field(default=None)
    dynamic_choices: Optional[bool] = Field(default=None)
    choices_empty_label: Optional[str] = Field(default=None)
    refreshable: Optional[bool] = Field(default=None)
    ignore_case: Optional[bool] = Field(default=None)
    match_middle: Optional[bool] = Field(default=None)
    tag: Optional[str] = Field(default=None)
    export: Optional[bool] = Field(default=None)
    when: Optional[SocaUserInputParamCondition] = Field(default=None)
    expose: Optional[SocaUserInputParamExposeOptions] = Field(default=None)
    markdown: Optional[str] = Field(default=None)
    developer_notes: Optional[str] = Field(default=None)
    custom: Optional[Dict] = Field(default=None)

    @property
    def is_required(self) -> bool:
        validate = self.validate_
        if ModelUtils.is_empty(validate):
            return False
        return ModelUtils.get_as_bool(validate.required, False)

    @property
    def is_prompt(self) -> bool:
        return ModelUtils.get_as_bool(self.prompt, True)

    @property
    def is_multiple(self) -> bool:
        return ModelUtils.get_as_bool(self.multiple, False)

    @property
    def is_export(self) -> bool:
        return ModelUtils.get_as_bool(self.export, True)

    @property
    def is_cli_exposed(self) -> bool:
        if self.expose is None:
            return False
        if self.expose.cli is None:
            return False
        return not ModelUtils.is_empty(self.expose.cli.long_name)

    @property
    def cli_arg_name(self) -> Optional[str]:
        if not self.is_cli_exposed:
            return None
        return self.expose.cli.long_name.replace('--', '', 1).replace('-', '_')

    @property
    def is_cli_arg_required(self) -> bool:
        if not self.is_cli_exposed:
            return False
        return ModelUtils.get_as_bool(self.expose.cli.required, False)

    @property
    def cli_arg_format(self) -> Optional[str]:
        if not self.is_cli_exposed:
            return None
        cli_options = self.expose.cli
        long_name = cli_options.long_name
        short_name = cli_options.short_name
        s = ''
        if long_name:
            s += long_name
        if short_name:
            s += f'/{short_name}'
        return s

    @property
    def cli_help(self) -> str:
        help_text = None
        if self.is_cli_exposed:
            help_text = self.expose.cli.help_text
        if ModelUtils.is_empty(help_text):
            help_text = self.description
        if ModelUtils.is_empty(help_text):
            help_text = self.title

        if help_text.endswith(':'):
            help_text = help_text[:-1]

        help_text = self.try_convert_select_to_enter(help_text)

        return help_text

    @staticmethod
    def try_convert_select_to_enter(message: str) -> str:
        """
        on a best effort basis - lazy implementation
        devs can set install_params.yml -> (param) -> expose.cli.help_text to set the exact text.
        also called for description2 use case from SocaQuestion
        """
        token = message.strip()
        if token.startswith(('Select the', 'Select an', 'Select a', 'Choose ')):
            token = token.replace('Select the', 'Enter the', 1)
            token = token.replace('Select an', 'Enter a', 1)
            token = token.replace('Select a', 'Enter a', 1)
            token = token.replace('Choose ', 'Enter ', 1)
            token = token.replace('Choose the', 'Enter the', 1)
        return token

    @staticmethod
    def get_choice_value(choice: Union[str, SocaUserInputChoice]) -> Any:
        if isinstance(choice, str):
            return choice
        else:
            return choice.value

    def get_first_choice(self) -> Optional[Any]:
        choices = ModelUtils.get_as_list(self.choices, [])
        if len(choices) == 0:
            return None
        return self.get_choice_value(choices[0])

    def get_default(self) -> Optional[Any]:

        multiple = ModelUtils.get_as_bool(self.multiple, False)
        if self.data_type == 'bool':
            if multiple:
                default = ModelUtils.get_as_bool_list(self.default)
            else:
                default = self.default
                if default == '$first':
                    default = self.get_first_choice()
                default = ModelUtils.get_as_bool(default)
        elif self.data_type == 'int':
            if multiple:
                default = ModelUtils.get_as_int_list(self.default)
            else:
                default = self.default
                if default == '$first':
                    default = self.get_first_choice()
                default = ModelUtils.get_as_int(default)
        elif self.data_type == 'float':
            if multiple:
                default = ModelUtils.get_as_float_list(self.default)
            else:
                default = self.default
                if default == '$first':
                    default = self.get_first_choice()
                default = ModelUtils.get_as_float(default)
        else:
            if multiple:
                default = ModelUtils.get_as_string_list(self.default)
            else:
                default = self.default
                if default == '$first':
                    default = self.get_first_choice()

        if multiple:
            first = ModelUtils.get_first(self.default)
            if first is not None:
                if first == '$all':
                    default = []
                    choices = ModelUtils.get_as_list(self.choices, [])
                    for choice in choices:
                        default.append(self.get_choice_value(choice))
                if first == '$first':
                    default = self.get_first_choice()
                    if default is not None:
                        default = [default]

        return default


class SocaInputParamValidationEntry(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    section: Optional[str] = Field(default=None)
    message: Optional[str] = Field(default=None)
    meta: Optional[SocaUserInputParamMetadata] = Field(default=None)


class SocaInputParamValidationResult(SocaBaseModel):
    entries: Optional[List[SocaInputParamValidationEntry]] = Field(default=None)

    def __bool__(self):
        return self.is_valid

    @property
    def is_valid(self) -> bool:
        return len(self.entries) == 0


class SocaUserInputGroupMetadata(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    module: Optional[str] = Field(default=None)
    section: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    params: Optional[List[SocaUserInputParamMetadata]] = Field(default=None)


class SocaUserInputSectionReview(SocaBaseModel):
    prompt: Optional[str] = Field(default=None)


class SocaUserInputSectionMetadata(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    module: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    required: Optional[bool] = Field(default=None)
    review: Optional[SocaUserInputSectionReview] = Field(default=None)
    params: Optional[List[SocaUserInputParamMetadata]] = Field(default=None)
    groups: Optional[List[SocaUserInputGroupMetadata]] = Field(default=None)
    markdown: Optional[str] = Field(default=None)

    @property
    def is_required(self) -> bool:
        return ModelUtils.get_as_bool(self.required, False)


class SocaUserInputModuleMetadata(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    sections: Optional[List[SocaUserInputSectionMetadata]] = Field(default=None)
    markdown: Optional[str] = Field(default=None)


class SocaUserInputTag(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    markdown: Optional[str] = Field(default=None)
    unicode: Optional[str] = Field(default=None)
    ascii: Optional[str] = Field(default=None)
    icon: Optional[str] = Field(default=None)


class SocaInputParamSpec(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    version: Optional[str] = Field(default=None)
    tags: Optional[List[SocaUserInputTag]] = Field(default=None)
    modules: Optional[List[SocaUserInputModuleMetadata]] = Field(default=None)
    params: Optional[List[SocaUserInputParamMetadata]] = Field(default=None)
