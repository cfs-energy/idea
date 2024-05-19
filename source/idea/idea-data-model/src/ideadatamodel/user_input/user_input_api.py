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
    'GetModuleMetadataRequest',
    'GetModuleMetadataResult',
    'GetParamChoicesRequest',
    'GetParamChoicesResult',
    'GetParamDefaultRequest',
    'GetParamDefaultResult',
    'SetParamRequest',
    'SetParamResult',
    'GetParamsRequest',
    'GetParamsResult'
)

from pydantic import Field

from ideadatamodel import SocaPayload, SocaListingPayload
from ideadatamodel.user_input.user_input_model import SocaUserInputModuleMetadata, SocaUserInputChoice

from typing import Optional, List, Any, Dict


# Installer.GetModuleMetadata

class GetModuleMetadataRequest(SocaPayload):
    module: Optional[str] = Field(default=None)


class GetModuleMetadataResult(SocaPayload):
    module: Optional[SocaUserInputModuleMetadata] = Field(default=None)


# Installer.GetParamChoices

class GetParamChoicesRequest(SocaListingPayload):
    module: Optional[str] = Field(default=None)
    param: Optional[str] = Field(default=None)
    refresh: Optional[bool] = Field(default=None)


class GetParamChoicesResult(SocaListingPayload):
    listing: Optional[List[SocaUserInputChoice]] = Field(default=None)


# Installer.GetParamDefault

class GetParamDefaultRequest(SocaPayload):
    module: Optional[str] = Field(default=None)
    param: Optional[str] = Field(default=None)
    reset: Optional[bool] = Field(default=None)


class GetParamDefaultResult(SocaPayload):
    default: Optional[Any] = Field(default=None)


# Installer.SetParam

class SetParamRequest(SocaPayload):
    module: Optional[str] = Field(default=None)
    param: Optional[str] = Field(default=None)
    value: Optional[Any] = Field(default=None)


class SetParamResult(SocaPayload):
    value: Optional[Any] = Field(default=None)
    refresh: Optional[bool] = Field(default=None)


# Installer.GetParams

class GetParamsRequest(SocaPayload):
    module: Optional[str] = Field(default=None)
    format: Optional[str] = Field(default=None)


class GetParamsResult(SocaPayload):
    params: Optional[Dict[str, Any]] = Field(default=None)
    yaml: Optional[str] = Field(default=None)
