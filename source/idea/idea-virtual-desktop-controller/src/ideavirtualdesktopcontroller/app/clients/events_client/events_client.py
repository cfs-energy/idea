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
from enum import Enum
from typing import Optional, Dict
from pydantic import Field
from ideadatamodel import SocaBaseModel
from ideasdk.protocols import SocaContextProtocol
from ideasdk.utils import Utils


class VirtualDesktopEventType(str, Enum):
    VALIDATE_SOFTWARE_STACK_CREATION_EVENT = 'VALIDATE_SOFTWARE_STACK_CREATION_EVENT'
    VALIDATE_DCV_SESSION_DELETION_EVENT = 'VALIDATE_DCV_SESSION_DELETION_EVENT'
    VALIDATE_DCV_SESSION_CREATION_EVENT = 'VALIDATE_DCV_SESSION_CREATION_EVENT'
    IDEA_SESSION_RESUME_SESSION_COMMAND_PROGRESS_EVENT = (
        'IDEA_SESSION_RESUME_SESSION_COMMAND_PROGRESS_EVENT'
    )
    IDEA_SESSION_CPU_UTILIZATION_COMMAND_PROGRESS_EVENT = (
        'IDEA_SESSION_CPU_UTILIZATION_COMMAND_PROGRESS_EVENT'
    )
    ENABLE_USERDATA_WINDOWS_COMMAND_PROGRESS_EVENT = (
        'ENABLE_USERDATA_WINDOWS_COMMAND_PROGRESS_EVENT'
    )
    DISABLE_USERDATA_WINDOWS_COMMAND_PROGRESS_EVENT = (
        'DISABLE_USERDATA_WINDOWS_COMMAND_PROGRESS_EVENT'
    )
    IDEA_SESSION_SCHEDULED_RESUME_EVENT = 'IDEA_SESSION_SCHEDULED_RESUME_EVENT'
    IDEA_SESSION_SCHEDULED_STOP_EVENT = 'IDEA_SESSION_SCHEDULED_STOP_EVENT'
    IDEA_SESSION_TERMINATE_EVENT = 'IDEA_SESSION_TERMINATE_EVENT'
    IDEA_SESSION_SOFTWARE_STACK_UPDATED_EVENT = (
        'IDEA_SESSION_SOFTWARE_STACK_UPDATED_EVENT'
    )
    IDEA_SESSION_PERMISSIONS_ENFORCE_EVENT = 'IDEA_SESSION_PERMISSIONS_ENFORCE_EVENT'
    IDEA_SESSION_PERMISSIONS_UPDATE_EVENT = 'IDEA_SESSION_PERMISSIONS_UPDATE_EVENT'
    EC2_INSTANCE_STATE_CHANGED_EVENT = 'EC2_INSTANCE_STATE_CHANGED_EVENT'
    DB_ENTRY_CREATED_EVENT = 'DB_ENTRY_CREATED_EVENT'
    DB_ENTRY_UPDATED_EVENT = 'DB_ENTRY_UPDATED_EVENT'
    DB_ENTRY_DELETED_EVENT = 'DB_ENTRY_DELETED_EVENT'
    DCV_HOST_READY_EVENT = 'DCV_HOST_READY_EVENT'
    DCV_HOST_REBOOT_COMPLETE_EVENT = 'DCV_HOST_REBOOT_COMPLETE_EVENT'
    DCV_BROKER_USERDATA_EXECUTION_COMPLETE_EVENT = (
        'DCV_BROKER_USERDATA_EXECUTION_COMPLETE_EVENT'
    )
    SCHEDULED_EVENT = 'SCHEDULED_EVENT'
    USER_CREATED_EVENT = 'USER_CREATED_EVENT'
    USER_DISABLED_EVENT = 'USER_DISABLED_EVENT'


class VirtualDesktopEvent(SocaBaseModel):
    event_group_id: Optional[str] = Field(default=None)
    event_type: Optional[VirtualDesktopEventType] = Field(default=None)
    detail: Optional[Dict] = Field(default=None)

    def model_dump(self, **kwargs):
        """Override model_dump to ensure correct serialization of enum values"""
        data = super().model_dump(**kwargs)
        if data.get('event_type') is not None:
            # Ensure event_type is serialized as the string value, not the enum representation
            data['event_type'] = str(data['event_type'])
        return data


class EventsClient:
    def __init__(self, context: SocaContextProtocol):
        """
        :param context: Application Context
        """
        self.context = context
        self._logger = context.logger('events-client')

    def publish_event(self, event: VirtualDesktopEvent):
        if Utils.is_empty(event):
            return

        events_sqs_queue_url = self.context.config().get_string(
            'virtual-desktop-controller.events_sqs_queue_url', default=None
        )
        if Utils.is_empty(events_sqs_queue_url):
            return

        # Use model_dump to ensure correct serialization of enum values
        event_data = event.model_dump()

        self.context.aws().sqs().send_message(
            QueueUrl=events_sqs_queue_url,
            MessageBody=Utils.to_json(event_data),
            MessageGroupId=event.event_group_id.replace(' ', '_'),
        )
