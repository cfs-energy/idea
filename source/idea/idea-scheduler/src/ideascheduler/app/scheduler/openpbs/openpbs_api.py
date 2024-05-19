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

from ideadatamodel.api import SocaPayload
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSEvent

from typing import Optional, Dict
from pydantic import Field


class OpenPBSHookRequest(SocaPayload):
    event: Optional[OpenPBSEvent] = Field(default=None)


class OpenPBSHookResult(SocaPayload):
    formatted_user_message: Optional[str] = Field(default=None)
    queue: Optional[str] = Field(default=None)
    project: Optional[str] = Field(default=None)
    resources_updated: Optional[Dict[str, Optional[str]]] = Field(default=None)
    resources_deleted: Optional[Dict[str, Optional[str]]] = Field(default=None)
    accept: Optional[bool] = Field(default=None)
