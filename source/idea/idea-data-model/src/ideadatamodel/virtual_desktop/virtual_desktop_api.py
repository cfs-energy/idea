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
    'CreateSessionRequest',
    'CreateSessionResponse',
    'BatchCreateSessionRequest',
    'BatchCreateSessionResponse',
    'GetSessionConnectionInfoRequest',
    'GetSessionConnectionInfoResponse',
    'GetSessionScreenshotRequest',
    'GetSessionScreenshotResponse',
    'UpdateSessionRequest',
    'UpdateSessionResponse',
    'SetSessionCleanupExemptionRequest',
    'SetSessionCleanupExemptionResponse',
    'BaseSoftwareStackAmiRefreshResult',
    'RefreshBaseSoftwareStackAmisRequest',
    'RefreshBaseSoftwareStackAmisResponse',
    'ListDesktopImagesRequest',
    'ListDesktopImagesResponse',
    'BuildDesktopImageRequest',
    'BuildDesktopImageResponse',
    'DesktopImageBuildStartResult',
    'BuildAllDesktopImagesRequest',
    'BuildAllDesktopImagesResponse',
    'UseBuiltDesktopImagesRequest',
    'UseBuiltDesktopImagesResponse',
    'GetSessionInfoRequest',
    'GetSessionInfoResponse',
    'DeleteSessionRequest',
    'DeleteSessionResponse',
    'StopSessionRequest',
    'StopSessionResponse',
    'RebootSessionRequest',
    'RebootSessionResponse',
    'ResumeSessionsRequest',
    'ResumeSessionsResponse',
    'ListSessionsResponse',
    'ListSessionsRequest',
    'CreateSoftwareStackRequest',
    'CreateSoftwareStackResponse',
    'UpdateSoftwareStackRequest',
    'UpdateSoftwareStackResponse',
    'DeleteSoftwareStackRequest',
    'DeleteSoftwareStackResponse',
    'GetSoftwareStackInfoRequest',
    'GetSoftwareStackInfoResponse',
    'ListSoftwareStackRequest',
    'ListSoftwareStackResponse',
    'ListPermissionsRequest',
    'ListPermissionsResponse',
    'ListSupportedOSRequest',
    'ListSupportedOSResponse',
    'CreateSoftwareStackFromSessionRequest',
    'CreateSoftwareStackFromSessionResponse',
    'DescribeServersRequest',
    'DescribeServersResponse',
    'DescribeSessionsRequest',
    'DescribeSessionsResponse',
    'ListScheduleTypesRequest',
    'ListScheduleTypesResponse',
    'ListSupportedGPURequest',
    'ListSupportedGPUResponse',
    'ListAllowedInstanceTypesRequest',
    'ListAllowedInstanceTypesResponse',
    'ListAllowedInstanceTypesForSessionRequest',
    'ListAllowedInstanceTypesForSessionResponse',
    'ReIndexUserSessionsRequest',
    'ReIndexUserSessionsResponse',
    'ReIndexSoftwareStacksRequest',
    'ReIndexSoftwareStacksResponse',
    'ListPermissionProfilesRequest',
    'ListPermissionProfilesResponse',
    'GetPermissionProfileRequest',
    'GetPermissionProfileResponse',
    'CreatePermissionProfileResponse',
    'CreatePermissionProfileRequest',
    'UpdatePermissionProfileRequest',
    'UpdatePermissionProfileResponse',
    'GetBasePermissionsRequest',
    'GetBasePermissionsResponse',
    'UpdateSessionPermissionRequest',
    'UpdateSessionPermissionResponse',
    'OPEN_API_SPEC_ENTRIES_VIRTUAL_DESKTOP',
)

from typing import Optional, List, Dict, Any

from ideadatamodel.api import SocaPayload, SocaListingPayload, IdeaOpenAPISpecEntry
from ideadatamodel.virtual_desktop.virtual_desktop_model import (
    VirtualDesktopSession,
    VirtualDesktopSessionBatchResponsePayload,
    VirtualDesktopSessionConnectionInfo,
    VirtualDesktopSessionScreenshot,
    VirtualDesktopSoftwareStack,
    VirtualDesktopPermissionProfile,
    VirtualDesktopPermission,
    VirtualDesktopSessionPermission,
)
from pydantic import Field
from ideadatamodel.aws import ImageBuildRecord, ImageInventoryRow

# VirtualDesktopAdmin.CreateSession - Request
# VirtualDesktop.CreateSession - Request


class CreateSessionRequest(SocaPayload):
    session: Optional[VirtualDesktopSession] = Field(default=None)
    admin_custom_instance_type: Optional[bool] = Field(default=False)


# VirtualDesktopAdmin.CreateSession - Response
# VirtualDesktop.CreateSession - Response
class CreateSessionResponse(SocaPayload):
    session: Optional[VirtualDesktopSession] = Field(default=None)


# VirtualDesktopAdmin.BatchCreateSessions - Request
class BatchCreateSessionRequest(SocaPayload):
    sessions: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.BatchCreateSessions - Response
class BatchCreateSessionResponse(VirtualDesktopSessionBatchResponsePayload):
    success: Optional[List[VirtualDesktopSession]] = Field(default=None)
    failed: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.GetSessionConnectionInfo - Request
# VirtualDesktop.GetSessionConnectionInfo - Request
class GetSessionConnectionInfoRequest(SocaPayload):
    connection_info: Optional[VirtualDesktopSessionConnectionInfo] = Field(default=None)


# VirtualDesktopAdmin.GetSessionConnectionInfo - Response
# VirtualDesktop.GetSessionConnectionInfo - Response
class GetSessionConnectionInfoResponse(SocaPayload):
    connection_info: Optional[VirtualDesktopSessionConnectionInfo] = Field(default=None)


# VirtualDesktopAdmin.GetSessionScreenshot - Request
# VirtualDesktop.GetSessionScreenshot - Request
class GetSessionScreenshotRequest(SocaPayload):
    screenshots: Optional[List[VirtualDesktopSessionScreenshot]] = Field(default=None)


# VirtualDesktopAdmin.GetSessionScreenshot - Response
# VirtualDesktop.GetSessionScreenshot - Response
class GetSessionScreenshotResponse(VirtualDesktopSessionBatchResponsePayload):
    success: Optional[List[VirtualDesktopSessionScreenshot]] = Field(default=None)
    failed: Optional[List[VirtualDesktopSessionScreenshot]] = Field(default=None)


# VirtualDesktop.UpdateSession - Request
class UpdateSessionRequest(SocaPayload):
    session: Optional[VirtualDesktopSession] = Field(default=None)


# VirtualDesktop.UpdateSession - Response
class UpdateSessionResponse(SocaPayload):
    session: Optional[VirtualDesktopSession] = Field(default=None)


# VirtualDesktopAdmin.SetSessionCleanupExemption - Request
class SetSessionCleanupExemptionRequest(SocaPayload):
    idea_session_id: Optional[str] = Field(default=None)
    owner: Optional[str] = Field(default=None)
    exempt: Optional[bool] = Field(default=None)
    reason: Optional[str] = Field(default=None)


# VirtualDesktopAdmin.SetSessionCleanupExemption - Response
class SetSessionCleanupExemptionResponse(SocaPayload):
    session: Optional[VirtualDesktopSession] = Field(default=None)


# VirtualDesktopAdmin.RefreshBaseSoftwareStackAmis - per-stack result
class BaseSoftwareStackAmiRefreshResult(SocaPayload):
    stack_id: Optional[str] = Field(default=None)
    # updated (ami_id moved to the newest stock image) | base_updated (only the base
    # advanced; the stack keeps launching from a built image) | up_to_date | error
    status: Optional[str] = Field(default=None)
    old_ami: Optional[str] = Field(default=None)
    new_ami: Optional[str] = Field(default=None)
    new_base_ami: Optional[str] = Field(default=None)
    message: Optional[str] = Field(default=None)


# VirtualDesktopAdmin.RefreshBaseSoftwareStackAmis - Request
class RefreshBaseSoftwareStackAmisRequest(SocaPayload):
    # when set, refresh exactly these stacks; otherwise every ss-base-* stack
    stack_ids: Optional[List[str]] = Field(default=None)


# VirtualDesktopAdmin.RefreshBaseSoftwareStackAmis - Response
class RefreshBaseSoftwareStackAmisResponse(SocaPayload):
    results: Optional[List[BaseSoftwareStackAmiRefreshResult]] = Field(default=None)


# VirtualDesktopAdmin.ListDesktopImages - Request
class ListDesktopImagesRequest(SocaPayload):
    pass


# VirtualDesktopAdmin.ListDesktopImages - Response
class ListDesktopImagesResponse(SocaPayload):
    listing: Optional[List[ImageInventoryRow]] = Field(default=None)


# VirtualDesktopAdmin.BuildDesktopImage - Request
class BuildDesktopImageRequest(SocaPayload):
    base_os: Optional[str] = Field(default=None)
    # x86_64 (default) or arm64; selects the ss-base-<os>-<arch>-base stack
    architecture: Optional[str] = Field(default=None)
    # defaults to the newest stock image for base_os and architecture
    base_ami: Optional[str] = Field(default=None)
    instance_type: Optional[str] = Field(default=None)
    # point the base stack at the new image and reindex it when the build finishes (default true)
    update_stack: Optional[bool] = Field(default=None)


# VirtualDesktopAdmin.BuildDesktopImage - Response
class BuildDesktopImageResponse(SocaPayload):
    record: Optional[ImageBuildRecord] = Field(default=None)


# VirtualDesktopAdmin.BuildAllDesktopImages - per-stack outcome
class DesktopImageBuildStartResult(SocaPayload):
    stack_id: Optional[str] = Field(default=None)
    base_os: Optional[str] = Field(default=None)
    architecture: Optional[str] = Field(default=None)
    # started | skipped | error
    status: Optional[str] = Field(default=None)
    message: Optional[str] = Field(default=None)


# VirtualDesktopAdmin.BuildAllDesktopImages - Request
class BuildAllDesktopImagesRequest(SocaPayload):
    pass


# VirtualDesktopAdmin.BuildAllDesktopImages - Response
class BuildAllDesktopImagesResponse(SocaPayload):
    results: Optional[List[DesktopImageBuildStartResult]] = Field(default=None)


# VirtualDesktopAdmin.UseBuiltDesktopImages - Request
class UseBuiltDesktopImagesRequest(SocaPayload):
    # base stacks to repoint at their last completed build; every base stack when absent
    stack_ids: Optional[List[str]] = Field(default=None)


# VirtualDesktopAdmin.UseBuiltDesktopImages - Response (status updated | skipped | error)
class UseBuiltDesktopImagesResponse(SocaPayload):
    results: Optional[List[DesktopImageBuildStartResult]] = Field(default=None)


# VirtualDesktopAdmin.GetSessionInfo - Request
# VirtualDesktop.GetSessionInfo - Request
class GetSessionInfoRequest(SocaPayload):
    session: Optional[VirtualDesktopSession] = Field(default=None)


# VirtualDesktopAdmin.GetSessionInfo - Response
# VirtualDesktop.GetSessionInfo - Response
class GetSessionInfoResponse(SocaPayload):
    session: Optional[VirtualDesktopSession] = Field(default=None)


# VirtualDesktopAdmin.GetSoftwareStackInfo - Request
class GetSoftwareStackInfoRequest(SocaPayload):
    stack_id: Optional[str] = Field(default=None)


# VirtualDesktopAdmin.GetSoftwareStackInfo - Response
class GetSoftwareStackInfoResponse(SocaPayload):
    software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)


# VirtualDesktopAdmin.DeleteSessions - Request
# VirtualDesktop.DeleteSessions - Request
class DeleteSessionRequest(SocaPayload):
    sessions: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.DeleteSessions - Response
# VirtualDesktop.DeleteSessions - Response
class DeleteSessionResponse(VirtualDesktopSessionBatchResponsePayload):
    success: Optional[List[VirtualDesktopSession]] = Field(default=None)
    failed: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.StopSessions - Request
# VirtualDesktop.StopSessions - Request
class StopSessionRequest(SocaPayload):
    sessions: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.StopSessions - Response
# VirtualDesktop.StopSessions - Response
class StopSessionResponse(VirtualDesktopSessionBatchResponsePayload):
    success: Optional[List[VirtualDesktopSession]] = Field(default=None)
    failed: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.RebootSessions - Request
# VirtualDesktop.RebootSessions - Request
class RebootSessionRequest(SocaPayload):
    sessions: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.RebootSessions - Response
# VirtualDesktop.RebootSessions - Response
class RebootSessionResponse(VirtualDesktopSessionBatchResponsePayload):
    success: Optional[List[VirtualDesktopSession]] = Field(default=None)
    failed: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.ResumeSessions - Request
# VirtualDesktop.ResumeSessions - Request
class ResumeSessionsRequest(SocaPayload):
    sessions: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.ResumeSessions - Response
# VirtualDesktop.ResumeSessions - Response
class ResumeSessionsResponse(VirtualDesktopSessionBatchResponsePayload):
    success: Optional[List[VirtualDesktopSession]] = Field(default=None)
    failed: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.ListSessions - Request
# VirtualDesktop.ListSessions - Request
class ListSessionsRequest(SocaListingPayload):
    pass


# VirtualDesktopAdmin.ListSessions - Response
# VirtualDesktop.ListSessions - Response
class ListSessionsResponse(SocaListingPayload):
    listing: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopAdmin.CreateSoftwareStackFromSession - Request
class CreateSoftwareStackFromSessionRequest(SocaPayload):
    session: Optional[VirtualDesktopSession] = Field(default=None)
    new_software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)


# VirtualDesktopAdmin.CreateSoftwareStackFromSession - Response
class CreateSoftwareStackFromSessionResponse(SocaPayload):
    software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)


# VirtualDesktopAdmin.CreateSoftwareStack - Request
class CreateSoftwareStackRequest(SocaPayload):
    software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)


# VirtualDesktopAdmin.CreateSoftwareStack - Response
class CreateSoftwareStackResponse(SocaPayload):
    software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)


# VirtualDesktopAdmin.UpdateSoftwareStack - Request
class UpdateSoftwareStackRequest(SocaPayload):
    software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)


# VirtualDesktopAdmin.UpdateSoftwareStack - Response
class UpdateSoftwareStackResponse(SocaPayload):
    software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)


# VirtualDesktopAdmin.DeleteSoftwareStack - Request
class DeleteSoftwareStackRequest(SocaPayload):
    software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)
    software_stacks: Optional[List[VirtualDesktopSoftwareStack]] = Field(default=None)


# VirtualDesktopAdmin.DeleteSoftwareStack - Response
class DeleteSoftwareStackResponse(SocaPayload):
    software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)
    software_stacks: Optional[List[VirtualDesktopSoftwareStack]] = Field(default=None)
    success: Optional[bool] = Field(default=False)
    success_list: Optional[List[VirtualDesktopSoftwareStack]] = Field(default=None)
    failed_list: Optional[List[VirtualDesktopSoftwareStack]] = Field(default=None)


# VirtualDesktopAdmin.ListSoftwareStacks - Request
# VirtualDesktop.ListSoftwareStacks - Request
class ListSoftwareStackRequest(SocaListingPayload):
    disabled_also: Optional[bool] = Field(default=None)
    project_id: Optional[str] = Field(default=None)


# VirtualDesktopAdmin.ListSoftwareStacks - Response
# VirtualDesktop.ListSoftwareStacks - Request
class ListSoftwareStackResponse(SocaListingPayload):
    listing: Optional[List[VirtualDesktopSoftwareStack]] = Field(default=None)


# VirtualDesktopDCV.DescribeServers - Request
class DescribeServersRequest(SocaPayload):
    pass


# VirtualDesktopDCV.DescribeServers - Response
class DescribeServersResponse(SocaPayload):
    response: Optional[Dict] = Field(default=None)


# VirtualDesktopDCV.DescribeSessions - Request
class DescribeSessionsRequest(SocaPayload):
    sessions: Optional[List[VirtualDesktopSession]] = Field(default=None)


# VirtualDesktopDCV.DescribeSessions - Response
class DescribeSessionsResponse(SocaPayload):
    response: Optional[Dict] = Field(default=None)


# VirtualDesktopUtils.ListScheduleTypes - Request
class ListScheduleTypesRequest(SocaPayload):
    pass


# VirtualDesktopUtils.ListScheduleTypes - Response
class ListScheduleTypesResponse(SocaListingPayload):
    listing: Optional[List[str]] = Field(default=None)


# VirtualDesktopUtils.ListSupportedOS - Request
class ListSupportedOSRequest(SocaPayload):
    pass


# VirtualDesktopUtils.ListSupportedOS - Response
class ListSupportedOSResponse(SocaListingPayload):
    listing: Optional[List[str]] = Field(default=None)


# VirtualDesktopUtils.ListSupportedGPU - Request
class ListSupportedGPURequest(SocaPayload):
    pass


# VirtualDesktopUtils.ListSupportedGPU - Response
class ListSupportedGPUResponse(SocaListingPayload):
    listing: Optional[List[str]] = Field(default=None)


# VirtualDesktopUtils.ListAllowedInstanceTypes - Request
class ListAllowedInstanceTypesRequest(SocaPayload):
    hibernation_support: Optional[bool] = Field(default=None)
    software_stack: Optional[VirtualDesktopSoftwareStack] = Field(default=None)


# VirtualDesktopUtils.ListAllowedInstanceTypes - Response
class ListAllowedInstanceTypesResponse(SocaListingPayload):
    listing: List[Any]


# VirtualDesktopUtils.ListAllowedInstanceTypesForSession - Request
class ListAllowedInstanceTypesForSessionRequest(SocaPayload):
    session: Optional[VirtualDesktopSession] = Field(default=None)


# VirtualDesktopUtils.ListAllowedInstanceTypesForSession - Response
class ListAllowedInstanceTypesForSessionResponse(SocaListingPayload):
    listing: Optional[List[Any]] = Field(default=None)


# VirtualDesktop.CreateSharedSession
# VirtualDesktop.DeleteSharedSession

# VirtualDesktopAdmin.CreateSharedSession
# VirtualDesktopAdmin.DeleteSharedSession


# VirtualDesktopAdmin.ReIndexUserSessions - Request
class ReIndexUserSessionsRequest(SocaPayload):
    pass


# VirtualDesktopAdmin.ReIndexUserSessions - Response
class ReIndexUserSessionsResponse(SocaPayload):
    pass


# VirtualDesktopAdmin.ReIndexSoftwareStacks - Request
class ReIndexSoftwareStacksRequest(SocaPayload):
    pass


# VirtualDesktopAdmin.ReIndexSoftwareStacks - Response
class ReIndexSoftwareStacksResponse(SocaPayload):
    pass


# VirtualDesktopUtils.ListPermissionProfiles - Request
class ListPermissionProfilesRequest(SocaListingPayload):
    pass


# VirtualDesktopUtils.ListPermissionProfiles - Response
class ListPermissionProfilesResponse(SocaListingPayload):
    listing: Optional[List[VirtualDesktopPermissionProfile]] = Field(default=None)


# VirtualDesktopUtils.GetPermissionProfile - Request
class GetPermissionProfileRequest(SocaListingPayload):
    profile_id: Optional[str] = Field(default=None)


# VirtualDesktopUtils.GetPermissionProfile - Response
class GetPermissionProfileResponse(SocaPayload):
    profile: Optional[VirtualDesktopPermissionProfile] = Field(default=None)


# VirtualDesktopAdmin.UpdatePermissionProfile - Request
class UpdatePermissionProfileRequest(SocaPayload):
    profile: Optional[VirtualDesktopPermissionProfile] = Field(default=None)


# VirtualDesktopAdmin.UpdatePermissionProfile - Response
class UpdatePermissionProfileResponse(SocaPayload):
    profile: Optional[VirtualDesktopPermissionProfile] = Field(default=None)


# VirtualDesktopAdmin.CreatePermissionProfile - Request
class CreatePermissionProfileRequest(SocaPayload):
    profile: Optional[VirtualDesktopPermissionProfile] = Field(default=None)


# VirtualDesktopAdmin.CreatePermissionProfile - Response
class CreatePermissionProfileResponse(SocaPayload):
    profile: Optional[VirtualDesktopPermissionProfile] = Field(default=None)


# VirtualDesktopUtils.GetBasePermissions - Request
class GetBasePermissionsRequest(SocaPayload):
    pass


# VirtualDesktopUtils.GetBasePermissions - Response
class GetBasePermissionsResponse(SocaPayload):
    permissions: Optional[List[VirtualDesktopPermission]] = Field(default=None)


# VirtualDesktop.UpdateSessionPermission - Request
# VirtualDesktopAdmin.UpdateSessionPermission - Request
class UpdateSessionPermissionRequest(SocaPayload):
    create: Optional[List[VirtualDesktopSessionPermission]] = Field(default=None)
    delete: Optional[List[VirtualDesktopSessionPermission]] = Field(default=None)
    update: Optional[List[VirtualDesktopSessionPermission]] = Field(default=None)


# VirtualDesktop.UpdateSessionPermission - Response
# VirtualDesktopAdmin.UpdateSessionPermission - Response
class UpdateSessionPermissionResponse(SocaPayload):
    permissions: Optional[List[VirtualDesktopSessionPermission]] = Field(default=None)


# VirtualDesktop.ListSessionPermissions - Request
# VirtualDesktopAdmin.ListSessionPermissions - Request
# VirtualDesktop.ListSharedPermissions - Request
class ListPermissionsRequest(SocaListingPayload):
    idea_session_id: Optional[str] = Field(default=None)
    username: Optional[str] = Field(default=None)


# VirtualDesktop.ListSessionPermissions - Response
# VirtualDesktopAdmin.ListSessionPermissions - Response
# VirtualDesktop.ListSharedPermissions - Response
class ListPermissionsResponse(SocaListingPayload):
    listing: Optional[List[VirtualDesktopSessionPermission]] = Field(default=None)


OPEN_API_SPEC_ENTRIES_VIRTUAL_DESKTOP = [
    # VirtualDesktop.* STARTS #
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.CreateSession',
        request=CreateSessionRequest,
        result=CreateSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.UpdateSession',
        request=UpdateSessionRequest,
        result=UpdateSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.DeleteSessions',
        request=DeleteSessionRequest,
        result=DeleteSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.GetSessionInfo',
        request=GetSessionInfoRequest,
        result=GetSessionInfoResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.GetSessionScreenshot',
        request=GetSessionScreenshotRequest,
        result=GetSessionScreenshotResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.GetSessionConnectionInfo',
        request=GetSessionConnectionInfoRequest,
        result=GetSessionConnectionInfoResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.ListSessions',
        request=ListSessionsRequest,
        result=ListSessionsResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.StopSessions',
        request=StopSessionRequest,
        result=StopSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.ResumeSessions',
        request=ResumeSessionsRequest,
        result=ResumeSessionsResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.RebootSessions',
        request=RebootSessionRequest,
        result=RebootSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.ListSoftwareStacks',
        request=ListSoftwareStackRequest,
        result=ListSoftwareStackResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.ListSharedPermissions',
        request=ListPermissionsRequest,
        result=ListPermissionsResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.ListSessionPermissions',
        request=ListPermissionsRequest,
        result=ListPermissionsResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktop.UpdateSessionPermissions',
        request=UpdateSessionPermissionRequest,
        result=UpdateSessionPermissionResponse,
        is_listing=False,
        is_public=False,
    ),
    # VirtualDesktop.* ENDS #
    # VirtualDesktopAdmin.* STARTS #
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.CreateSession',
        request=CreateSessionRequest,
        result=CreateSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.BatchCreateSessions',
        request=BatchCreateSessionRequest,
        result=BatchCreateSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.UpdateSession',
        request=UpdateSessionRequest,
        result=UpdateSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.SetSessionCleanupExemption',
        request=SetSessionCleanupExemptionRequest,
        result=SetSessionCleanupExemptionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.ListDesktopImages',
        request=ListDesktopImagesRequest,
        result=ListDesktopImagesResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.BuildDesktopImage',
        request=BuildDesktopImageRequest,
        result=BuildDesktopImageResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.BuildAllDesktopImages',
        request=BuildAllDesktopImagesRequest,
        result=BuildAllDesktopImagesResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.UseBuiltDesktopImages',
        request=UseBuiltDesktopImagesRequest,
        result=UseBuiltDesktopImagesResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.RefreshBaseSoftwareStackAmis',
        request=RefreshBaseSoftwareStackAmisRequest,
        result=RefreshBaseSoftwareStackAmisResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.DeleteSessions',
        request=DeleteSessionRequest,
        result=DeleteSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.GetSessionInfo',
        request=GetSessionInfoRequest,
        result=GetSessionInfoResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.ListSessions',
        request=ListSessionsRequest,
        result=ListSessionsResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.StopSessions',
        request=StopSessionRequest,
        result=StopSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.RebootSessions',
        request=RebootSessionRequest,
        result=RebootSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.ResumeSessions',
        request=ResumeSessionsRequest,
        result=ResumeSessionsResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.GetSessionScreenshot',
        request=GetSessionScreenshotRequest,
        result=GetSessionScreenshotResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.GetSessionConnectionInfo',
        request=GetSessionConnectionInfoRequest,
        result=GetSessionConnectionInfoResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.CreateSoftwareStack',
        request=CreateSoftwareStackRequest,
        result=CreateSoftwareStackResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.UpdateSoftwareStack',
        request=UpdateSoftwareStackRequest,
        result=UpdateSoftwareStackResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.DeleteSoftwareStack',
        request=DeleteSoftwareStackRequest,
        result=DeleteSoftwareStackResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.GetSoftwareStackInfo',
        request=GetSoftwareStackInfoRequest,
        result=GetSoftwareStackInfoResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.ListSoftwareStacks',
        request=ListSoftwareStackRequest,
        result=ListSoftwareStackResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.CreateSoftwareStackFromSession',
        request=CreateSoftwareStackFromSessionRequest,
        result=CreateSoftwareStackFromSessionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.CreatePermissionProfile',
        request=CreatePermissionProfileRequest,
        result=CreatePermissionProfileResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.UpdatePermissionProfile',
        request=UpdatePermissionProfileRequest,
        result=UpdatePermissionProfileResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.ListSessionPermissions',
        request=ListPermissionsRequest,
        result=ListPermissionsResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.ListSharedPermissions',
        request=ListPermissionsRequest,
        result=ListPermissionsResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.UpdateSessionPermissions',
        request=UpdateSessionPermissionRequest,
        result=UpdateSessionPermissionResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.ReIndexUserSessions',
        request=ReIndexUserSessionsRequest,
        result=ReIndexUserSessionsResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopAdmin.ReIndexSoftwareStacks',
        request=ReIndexSoftwareStacksRequest,
        result=ReIndexSoftwareStacksResponse,
        is_listing=False,
        is_public=False,
    ),
    # VirtualDesktopAdmin.* ENDS #
    # VirtualDesktopUtils.* STARTS #
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopUtils.ListSupportedOS',
        request=ListSupportedOSRequest,
        result=ListSupportedOSResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopUtils.ListSupportedGPU',
        request=ListSupportedGPURequest,
        result=ListSupportedGPUResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopUtils.ListScheduleTypes',
        request=ListScheduleTypesRequest,
        result=ListScheduleTypesResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopUtils.ListAllowedInstanceTypes',
        request=ListAllowedInstanceTypesRequest,
        result=ListAllowedInstanceTypesResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopUtils.ListAllowedInstanceTypesForSession',
        request=ListAllowedInstanceTypesForSessionRequest,
        result=ListAllowedInstanceTypesForSessionResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopUtils.GetBasePermissions',
        request=GetBasePermissionsRequest,
        result=GetBasePermissionsResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopUtils.ListPermissionProfiles',
        request=ListPermissionProfilesRequest,
        result=ListPermissionProfilesResponse,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopUtils.GetPermissionProfile',
        request=GetPermissionProfileRequest,
        result=GetPermissionProfileResponse,
        is_listing=False,
        is_public=False,
    ),
    # VirtualDesktopUtils.* ENDS #
    # VirtualDesktopDCV.* STARTS #
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopDCV.DescribeServers',
        request=DescribeServersRequest,
        result=DescribeServersResponse,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='VirtualDesktopDCV.DescribeSessions',
        request=DescribeSessionsRequest,
        result=DescribeSessionsResponse,
        is_listing=False,
        is_public=False,
    ),
    # VirtualDesktopDCV.* ENDS #
]
