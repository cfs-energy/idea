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

from ideadatamodel import SocaBaseModel

from typing import Optional
from datetime import datetime
from pydantic import Field


class EmailTemplate(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    template_type: Optional[str] = Field(default=None)
    subject: Optional[str] = Field(default=None)
    body: Optional[str] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)
