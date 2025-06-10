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

__all__ = ('FileData', 'FileList')

from ideadatamodel import SocaBaseModel

from typing import Optional, List
from datetime import datetime
from pydantic import Field


class FileData(SocaBaseModel):
    owner: Optional[str] = Field(default=None)
    group: Optional[str] = Field(default=None)
    file_id: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    ext: Optional[str] = Field(default=None)
    is_dir: Optional[bool] = Field(default=None)
    is_hidden: Optional[bool] = Field(default=None)
    is_sym_link: Optional[bool] = Field(default=None)
    is_encrypted: Optional[bool] = Field(default=None)
    size: Optional[int] = Field(default=None)
    mod_date: Optional[datetime] = Field(default=None)
    children_count: Optional[int] = Field(default=None)
    color: Optional[str] = Field(default=None)
    icon: Optional[str] = Field(default=None)
    folder_chain_icon: Optional[str] = Field(default=None)
    thumbnail_url: Optional[str] = Field(default=None)


class FileList(SocaBaseModel):
    cwd: Optional[str] = Field(default=None)
    files: Optional[List[FileData]] = Field(default=None)
