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
    'User',
    'Group',
    'AuthResult',
    'DecodedToken'
)

from pydantic import Field

from ideadatamodel import SocaBaseModel

from typing import Optional, List
from datetime import datetime


class User(SocaBaseModel):
    username: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)
    email: Optional[str] = Field(default=None)
    uid: Optional[int] = Field(default=None)
    gid: Optional[int] = Field(default=None)
    group_name: Optional[str] = Field(default=None)
    ds_group_name: Optional[str] = Field(default=None)
    additional_groups: Optional[List[str]] = Field(default=None)
    login_shell: Optional[str] = Field(default=None)
    home_dir: Optional[str] = Field(default=None)
    sudo: Optional[bool] = Field(default=None)
    status: Optional[str] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)
    password_last_set: Optional[datetime] = Field(default=None)
    password_max_age: Optional[float] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)


class Group(SocaBaseModel):
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    ds_name: Optional[str] = Field(default=None)
    gid: Optional[int] = Field(default=None)
    group_type: Optional[str] = Field(default=None)
    ref: Optional[str] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)


class AuthResult(SocaBaseModel):
    access_token: Optional[str] = Field(default=None)
    id_token: Optional[str] = Field(default=None)
    refresh_token: Optional[str] = Field(default=None)
    expires_in: Optional[int] = Field(default=None)
    token_type: Optional[str] = Field(default=None)


class DecodedToken(SocaBaseModel):
    pass
