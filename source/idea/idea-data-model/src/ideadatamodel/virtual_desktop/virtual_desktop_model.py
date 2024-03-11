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
    'VirtualDesktopSessionState',
    'VirtualDesktopSessionType',
    'VirtualDesktopServer',
    'VirtualDesktopSession',
    'VirtualDesktopApplicationProfile',
    'VirtualDesktopSessionScreenshot',
    'VirtualDesktopSessionConnectionInfo',
    'VirtualDesktopBaseOS',
    'VirtualDesktopGPU',
    'VirtualDesktopArchitecture',
    'VirtualDesktopSoftwareStack',
    'VirtualDesktopTenancy',
    'DayOfWeek',
    'VirtualDesktopScheduleType',
    'VirtualDesktopSchedule',
    'VirtualDesktopWeekSchedule',
    'VirtualDesktopPermission',
    'VirtualDesktopPermissionProfile',
    'VirtualDesktopSessionPermission',
    'VirtualDesktopSessionPermissionActorType',
    'VirtualDesktopSessionBatchResponsePayload'
)

from ideadatamodel import SocaBaseModel, SocaMemory, SocaBatchResponsePayload, Project, constants

from typing import Optional, List
from datetime import datetime
from enum import Enum
from pydantic import Field

class VirtualDesktopSessionState(str, Enum):
    PROVISIONING = 'PROVISIONING'
    CREATING = 'CREATING'
    INITIALIZING = 'INITIALIZING'
    READY = 'READY'
    RESUMING = 'RESUMING'
    STOPPING = 'STOPPING'
    STOPPED = 'STOPPED'
    ERROR = 'ERROR'
    DELETING = 'DELETING'
    DELETED = 'DELETED'


class VirtualDesktopSessionType(str, Enum):
    CONSOLE = 'CONSOLE'
    VIRTUAL = 'VIRTUAL'


class VirtualDesktopTenancy(str, Enum):
    DEFAULT = 'default'
    DEDICATED = 'dedicated'
    HOST = 'host'


class VirtualDesktopGPU(str, Enum):
    NO_GPU = 'NO_GPU'
    NVIDIA = 'NVIDIA'
    AMD = 'AMD'


class VirtualDesktopArchitecture(str, Enum):
    X86_64 = 'x86_64'
    ARM64 = 'arm64'


class DayOfWeek(str, Enum):
    MONDAY = 'monday'
    TUESDAY = 'tuesday'
    WEDNESDAY = 'wednesday'
    THURSDAY = 'thursday'
    FRIDAY = 'friday'
    SATURDAY = 'saturday'
    SUNDAY = 'sunday'


class VirtualDesktopScheduleType(str, Enum):
    WORKING_HOURS = 'WORKING_HOURS'
    STOP_ALL_DAY = 'STOP_ALL_DAY'
    START_ALL_DAY = 'START_ALL_DAY'
    CUSTOM_SCHEDULE = 'CUSTOM_SCHEDULE'
    NO_SCHEDULE = 'NO_SCHEDULE'


class VirtualDesktopBaseOS(str, Enum):
    AMAZON_LINUX2 = constants.OS_AMAZONLINUX2
    AMAZON_LINUX2023 = constants.OS_AMAZONLINUX2023
    CENTOS7 = constants.OS_CENTOS7
    RHEL7 = constants.OS_RHEL7
    RHEL8 = constants.OS_RHEL8
    RHEL9 = constants.OS_RHEL9
    ROCKY8 = constants.OS_ROCKY8
    ROCKY9 = constants.OS_ROCKY9
    WINDOWS = constants.OS_WINDOWS


class VirtualDesktopSessionPermissionActorType(str, Enum):
    USER = 'USER'
    GROUP = 'GROUP'


class VirtualDesktopSessionScreenshot(SocaBaseModel):
    image_type: Optional[str] = Field(default=None)
    image_data: Optional[str] = Field(default=None)
    dcv_session_id: Optional[str] = Field(default=None)
    idea_session_id: Optional[str] = Field(default=None)
    idea_session_owner: Optional[str] = Field(default=None)
    create_time: Optional[str] = Field(default=None)
    failure_reason: Optional[str] = Field(default=None)


class VirtualDesktopSessionConnectionInfo(SocaBaseModel):
    dcv_session_id: Optional[str] = Field(default=None)
    idea_session_id: Optional[str] = Field(default=None)
    idea_session_owner: Optional[str] = Field(default=None)
    endpoint: Optional[str] = Field(default=None)
    username: Optional[str] = Field(default=None)
    web_url_path: Optional[str] = Field(default=None)
    access_token: Optional[str] = Field(default=None)
    failure_reason: Optional[str] = Field(default=None)


class VirtualDesktopSoftwareStack(SocaBaseModel):
    stack_id: Optional[str] = Field(default=None)
    base_os: Optional[VirtualDesktopBaseOS] = Field(default=None)
    name: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)
    ami_id: Optional[str] = Field(default=None)
    failure_reason: Optional[str] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)
    min_storage: Optional[SocaMemory] = Field(default=None)
    min_ram: Optional[SocaMemory] = Field(default=None)
    architecture: Optional[VirtualDesktopArchitecture] = Field(default=None)
    gpu: Optional[VirtualDesktopGPU] = Field(default=None)
    projects: Optional[List[Project]] = Field(default=None)
    pool_enabled: Optional[bool] = Field(default=None)
    pool_asg_name: Optional[str] = Field(default=None)
    launch_tenancy: Optional[VirtualDesktopTenancy] = Field(default=None)


class VirtualDesktopServer(SocaBaseModel):
    server_id: Optional[str] = Field(default=None)
    idea_sesssion_id: Optional[str] = Field(default=None)
    idea_session_owner: Optional[str] = Field(default=None)
    instance_id: Optional[str] = Field(default=None)
    instance_type: Optional[str] = Field(default=None)
    private_ip: Optional[str] = Field(default=None)
    private_dns_name: Optional[str] = Field(default=None)
    public_ip: Optional[str] = Field(default=None)
    public_dns_name: Optional[str] = Field(default=None)
    availability: Optional[str] = Field(default=None)
    unavailability_reason: Optional[str] = Field(default=None)
    console_session_count: Optional[int] = Field(default=None)
    virtual_session_count: Optional[int] = Field(default=None)
    max_concurrent_sessions_per_user: Optional[int] = Field(default=None)
    max_virtual_sessions: Optional[int] = Field(default=None)
    state: Optional[str] = Field(default=None)
    locked: Optional[bool] = Field(default=None)
    root_volume_size: Optional[SocaMemory] = Field(default=None)
    root_volume_iops: Optional[int] = Field(default=None)
    instance_profile_arn: Optional[str] = Field(default=None)
    security_groups: Optional[List[str]] = Field(default=None)
    subnet_id: Optional[str] = Field(default=None)
    key_pair_name: Optional[str] = Field(default=None)


class VirtualDesktopSchedule(SocaBaseModel):
    schedule_id: Optional[str] = Field(default=None)
    idea_session_id: Optional[str] = Field(default=None)
    idea_session_owner: Optional[str] = Field(default=None)
    day_of_week: Optional[DayOfWeek] = Field(default=None)
    start_up_time: Optional[str] = Field(default=None)
    shut_down_time: Optional[str] = Field(default=None)
    schedule_type: Optional[VirtualDesktopScheduleType] = Field(default=None)


class VirtualDesktopPermission(SocaBaseModel):
    key: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)


class VirtualDesktopWeekSchedule(SocaBaseModel):
    monday: Optional[VirtualDesktopSchedule] = Field(default=None)
    tuesday: Optional[VirtualDesktopSchedule] = Field(default=None)
    wednesday: Optional[VirtualDesktopSchedule] = Field(default=None)
    thursday: Optional[VirtualDesktopSchedule] = Field(default=None)
    friday: Optional[VirtualDesktopSchedule] = Field(default=None)
    saturday: Optional[VirtualDesktopSchedule] = Field(default=None)
    sunday: Optional[VirtualDesktopSchedule] = Field(default=None)


class VirtualDesktopPermissionProfile(SocaBaseModel):
    profile_id: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    permissions: Optional[List[VirtualDesktopPermission]] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)

    def get_permission(self, permission_key: str) -> Optional[VirtualDesktopPermission]:
        if self.permissions is None:
            return None

        for permission in self.permissions:
            if permission.key == permission_key:
                return permission
        return None


class VirtualDesktopSessionPermission(SocaBaseModel):
    idea_session_id: Optional[str] = Field(default=None)
    idea_session_owner: Optional[str] = Field(default=None)
    idea_session_name: Optional[str] = Field(default=None)
    idea_session_instance_type: Optional[str] = Field(default=None)
    idea_session_state: Optional[VirtualDesktopSessionState] = Field(default=None)
    idea_session_base_os: Optional[VirtualDesktopBaseOS] = Field(default=None)
    idea_session_created_on: Optional[datetime] = Field(default=None)
    idea_session_hibernation_enabled: Optional[bool] = Field(default=None)
    idea_session_type: Optional[VirtualDesktopSessionType] = Field(default=None)
    permission_profile: Optional[VirtualDesktopPermissionProfile] = Field(default=None)
    actor_type: Optional[VirtualDesktopSessionPermissionActorType] = Field(default=None)
    actor_name: Optional[str] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)
    expiry_date: Optional[datetime] = Field(default=None)
    failure_reason: Optional[str] = Field(default=None)


class VirtualDesktopSession(SocaBaseModel):
    dcv_session_id: Optional[str] = Field(default=None)
    idea_session_id: Optional[str] = Field(default=None)
    base_os: Optional[VirtualDesktopBaseOS] = Field(default=None)
    name: Optional[str] = Field(default=None)
    owner: Optional[str] = Field(default=None)
    type: Optional[VirtualDesktopSessionType] = Field(default=None)
    server: Optional[VirtualDesktopServer] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)
    state: Optional[VirtualDesktopSessionState] = Field(default=None)
    description: Optional[str] = Field(default=None)
    software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)
    project: Optional[Project] = Field(default=None)
    schedule: Optional[VirtualDesktopWeekSchedule] = Field(default=None)
    connection_count: Optional[int] = Field(default=None)
    force: Optional[bool] = Field(default=None)
    hibernation_enabled: Optional[bool] = Field(default=None)
    is_launched_by_admin: Optional[bool] = Field(default=None)
    locked: Optional[bool] = Field(default=None)
    # Transient field, to be used for API responses only.
    failure_reason: Optional[str] = Field(default=None)


class VirtualDesktopSessionBatchResponsePayload(SocaBatchResponsePayload):
    failed: Optional[List[VirtualDesktopSession]] = Field(default=None)
    success: Optional[List[VirtualDesktopSession]] = Field(default=None)


class VirtualDesktopApplicationProfile(SocaBaseModel):
    pass
