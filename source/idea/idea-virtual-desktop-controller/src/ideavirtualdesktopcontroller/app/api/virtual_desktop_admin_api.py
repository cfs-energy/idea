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
from botocore.exceptions import ClientError
import re
from typing import Callable, Dict, Optional

import ideavirtualdesktopcontroller
from ideadatamodel import constants
from ideadatamodel import (
    GetUserRequest,
    VirtualDesktopSessionState,
    GetProjectRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    BatchCreateSessionRequest,
    BatchCreateSessionResponse,
    GetSessionConnectionInfoRequest,
    GetSessionConnectionInfoResponse,
    GetSessionScreenshotRequest,
    GetSessionScreenshotResponse,
    UpdateSessionRequest,
    UpdateSessionResponse,
    SetSessionCleanupExemptionRequest,
    SetSessionCleanupExemptionResponse,
    RefreshBaseSoftwareStackAmisRequest,
    RefreshBaseSoftwareStackAmisResponse,
    ListDesktopImagesRequest,
    ListDesktopImagesResponse,
    BuildDesktopImageRequest,
    BuildDesktopImageResponse,
    BuildAllDesktopImagesRequest,
    BuildAllDesktopImagesResponse,
    UseBuiltDesktopImagesRequest,
    UseBuiltDesktopImagesResponse,
    ListImageRowsRequest,
    RefreshImagesRequest,
    RollbackImageRequest,
    SetImagePinnedRequest,
    GetImageScheduleRequest,
    GetImageScheduleResponse,
    UpdateImageScheduleRequest,
    UpdateImageScheduleResponse,
    ImageRefreshSchedule,
    GetSessionInfoRequest,
    GetSessionInfoResponse,
    DeleteSessionRequest,
    DeleteSessionResponse,
    StopSessionRequest,
    StopSessionResponse,
    RebootSessionRequest,
    RebootSessionResponse,
    ResumeSessionsRequest,
    ResumeSessionsResponse,
    ListSessionsRequest,
    CreateSoftwareStackRequest,
    ExtendSoftwareStackRequest,
    ExtendSoftwareStackResponse,
    ListOrphanInstancesRequest,
    ListOrphanInstancesResponse,
    CreateSoftwareStackResponse,
    UpdateSoftwareStackRequest,
    UpdateSoftwareStackResponse,
    DeleteSoftwareStackResponse,
    GetSoftwareStackInfoRequest,
    GetSoftwareStackInfoResponse,
    ListSoftwareStackRequest,
    ListPermissionsRequest,
    CreateSoftwareStackFromSessionRequest,
    CreateSoftwareStackFromSessionResponse,
    ReIndexUserSessionsResponse,
    ReIndexSoftwareStacksResponse,
    CreatePermissionProfileResponse,
    CreatePermissionProfileRequest,
    UpdatePermissionProfileRequest,
    UpdatePermissionProfileResponse,
    UpdateSessionPermissionRequest,
    UpdateSessionPermissionResponse,
    VirtualDesktopSession,
    VirtualDesktopArchitecture,
    VirtualDesktopSoftwareStack,
)
from ideadatamodel import errorcodes, exceptions
from ideasdk.api import ApiInvocationContext
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.software_stacks.stack_expiry import (
    MAX_EXTEND_DAYS,
    OrphanInstances,
    StackExpiry,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import (
    SESSION_CHANGED_MESSAGE,
    VirtualDesktopAPI,
)
from datetime import datetime, timedelta
from ideavirtualdesktopcontroller.app.software_stacks.constants import (
    BASE_STACK_MIN_STORAGE_GB,
)

# the monthly rebuild; the scheduler reads the same key for compute rows
IMAGE_REFRESH_SCHEDULE_KEY = (
    'virtual-desktop-controller.software_stacks.image_refresh_schedule'
)
# epoch ms of the desktop monthly check's last run, written by that job
IMAGE_REFRESH_LAST_RUN_KEY = (
    'virtual-desktop-controller.software_stacks.image_refresh_last_run_on'
)


STACK_OWNER_PATTERN = re.compile(r'[A-Za-z0-9._@-]{0,128}')

# app (client-credentials) tokens are authorized by module scope, users by elevation:
# reads take <module>/read, mutations <module>/write. a namespace absent from this map
# stays elevated-user-only, because it either resolves the caller's own username, which
# an app token does not carry, or is maintenance with no machine-to-machine caller.
APP_TOKEN_NAMESPACE_ACCESS: Dict[str, str] = {
    'VirtualDesktopAdmin.GetSessionInfo': 'read',
    'VirtualDesktopAdmin.ListSessions': 'read',
    'VirtualDesktopAdmin.ListSoftwareStacks': 'read',
    'VirtualDesktopAdmin.GetSoftwareStackInfo': 'read',
    'VirtualDesktopAdmin.ListSessionPermissions': 'read',
    'VirtualDesktopAdmin.ListSharedPermissions': 'read',
    'VirtualDesktopAdmin.CreateSession': 'write',
    'VirtualDesktopAdmin.BatchCreateSessions': 'write',
    'VirtualDesktopAdmin.UpdateSession': 'write',
    'VirtualDesktopAdmin.DeleteSessions': 'write',
    'VirtualDesktopAdmin.StopSessions': 'write',
    'VirtualDesktopAdmin.RebootSessions': 'write',
    'VirtualDesktopAdmin.ResumeSessions': 'write',
    'VirtualDesktopAdmin.CreateSoftwareStack': 'write',
    'VirtualDesktopAdmin.UpdateSoftwareStack': 'write',
    'VirtualDesktopAdmin.DeleteSoftwareStack': 'write',
    'VirtualDesktopAdmin.CreateSoftwareStackFromSession': 'write',
    'VirtualDesktopAdmin.CreatePermissionProfile': 'write',
    'VirtualDesktopAdmin.UpdatePermissionProfile': 'write',
    'VirtualDesktopAdmin.UpdateSessionPermissions': 'write',
}


# an update naming only these changes the stack's status alone
STATUS_ONLY_STACK_FIELDS = {'stack_id', 'base_os', 'enabled'}


# the identity tags written only at launch, which no project tag may replace
MOVE_RESERVED_TAG_KEYS = {
    constants.IDEA_TAG_CLUSTER_NAME,
    constants.IDEA_TAG_NODE_TYPE,
    constants.IDEA_TAG_MODULE_ID,
    constants.IDEA_TAG_BACKUP_PLAN,
}
# the step a move stopped at, as its administrator reads it
MOVE_STEP_NAMES = {
    'row': "Writing the desktop's new project",
    'profile': "Changing the desktop's instance role",
    'instance tags': "Tagging the desktop's host",
    'volume tags': "Tagging the desktop's volumes",
}


class VirtualDesktopAdminAPI(VirtualDesktopAPI):
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context)
        self.context = context
        self._logger = context.logger('virtual-desktop-admin-api')
        self.namespace_handler_map: Dict[str, Callable] = {
            'VirtualDesktopAdmin.CreateSession': self.create_session,
            'VirtualDesktopAdmin.BatchCreateSessions': self.batch_create_sessions,
            'VirtualDesktopAdmin.UpdateSession': self.update_session,
            'VirtualDesktopAdmin.SetSessionCleanupExemption': self.set_session_cleanup_exemption,
            'VirtualDesktopAdmin.MoveSessionToProject': self.move_session_to_project,
            'VirtualDesktopAdmin.DeleteSessions': self.delete_sessions,
            'VirtualDesktopAdmin.GetSessionInfo': self.get_session_info,
            'VirtualDesktopAdmin.ListSessions': self.list_sessions,
            'VirtualDesktopAdmin.StopSessions': self.stop_sessions,
            'VirtualDesktopAdmin.RebootSessions': self.reboot_sessions,
            'VirtualDesktopAdmin.ResumeSessions': self.resume_sessions,
            'VirtualDesktopAdmin.GetSessionScreenshot': self.get_session_screenshots,
            'VirtualDesktopAdmin.GetSessionConnectionInfo': self.get_session_connection_info,
            'VirtualDesktopAdmin.CreateSoftwareStack': self.create_software_stack,
            'VirtualDesktopAdmin.UpdateSoftwareStack': self.update_software_stack,
            'VirtualDesktopAdmin.DeleteSoftwareStack': self.delete_software_stack,
            'VirtualDesktopAdmin.GetSoftwareStackInfo': self.get_software_stack_info,
            'VirtualDesktopAdmin.ListSoftwareStacks': self.list_software_stacks,
            'VirtualDesktopAdmin.ExtendSoftwareStack': self.extend_software_stack,
            'VirtualDesktopAdmin.ListOrphanInstances': self.list_orphan_instances,
            'VirtualDesktopAdmin.CreateSoftwareStackFromSession': self.create_software_stack_from_session,
            'VirtualDesktopAdmin.CreatePermissionProfile': self.create_permission_profile,
            'VirtualDesktopAdmin.UpdatePermissionProfile': self.update_permission_profile,
            'VirtualDesktopAdmin.ListSessionPermissions': self.list_session_permissions,
            'VirtualDesktopAdmin.ListSharedPermissions': self.list_shared_permissions,
            'VirtualDesktopAdmin.UpdateSessionPermissions': self.update_session_permission,
            'VirtualDesktopAdmin.ReIndexUserSessions': self.re_index_user_sessions,
            'VirtualDesktopAdmin.ReIndexSoftwareStacks': self.re_index_software_stacks,
            'VirtualDesktopAdmin.RefreshBaseSoftwareStackAmis': self.refresh_base_software_stack_amis,
            'VirtualDesktopAdmin.ListDesktopImages': self.list_desktop_images,
            'VirtualDesktopAdmin.BuildDesktopImage': self.build_desktop_image,
            'VirtualDesktopAdmin.BuildAllDesktopImages': self.build_all_desktop_images,
            'VirtualDesktopAdmin.UseBuiltDesktopImages': self.use_built_desktop_images,
            'VirtualDesktopAdmin.ListImageRows': self.list_image_rows,
            'VirtualDesktopAdmin.RefreshImages': self.refresh_images,
            'VirtualDesktopAdmin.RollbackImage': self.rollback_image,
            'VirtualDesktopAdmin.SetImagePinned': self.set_image_pinned,
            'VirtualDesktopAdmin.GetImageSchedule': self.get_image_schedule,
            'VirtualDesktopAdmin.UpdateImageSchedule': self.update_image_schedule,
        }
        self._desktop_images = None

    def _validate_resume_session_request(
        self, session: VirtualDesktopSession
    ) -> tuple[VirtualDesktopSession, bool]:
        return self.validate_resume_session_request(session)

    def _validate_reboot_session_request(self, session: VirtualDesktopSession) -> bool:
        return self.validate_stop_session_request(session)

    def _validate_stop_session_request(self, session: VirtualDesktopSession) -> bool:
        return self.validate_stop_session_request(session)

    def _validate_delete_session_request(
        self, session: VirtualDesktopSession
    ) -> tuple[VirtualDesktopSession, bool]:
        return self.validate_delete_session_request(session)

    def _validate_create_session_request(
        self, session: VirtualDesktopSession, custom_instance_type: bool = False
    ) -> tuple[VirtualDesktopSession, bool]:
        # Validate Session Object
        if Utils.is_empty(session):
            session = VirtualDesktopSession()
            session.failure_reason = 'Missing Create Session Info'
            return session, False

        # validate if the user belongs within allowed group -
        # Check is done for admin only since virtual-desktop-user-api already enforces the group
        # check for api-call

        # Shortcut 'clusteradmin' to avoid lockout scenarios
        if self.is_cluster_administrator_username(session.owner):
            return self.validate_create_session_request(session, custom_instance_type)

        response = self.context.accounts_client.get_user(
            GetUserRequest(username=session.owner)
        )
        users_groups = Utils.get_as_list(response.user.additional_groups, default=[])
        is_user_part_of_group = False

        for group in self.VDI_GROUPS:
            if group in users_groups:
                is_user_part_of_group = True
                break

        if not is_user_part_of_group:
            session.failure_reason = f'User {session.owner} does not belong to any of the groups: {self.VDI_GROUPS}'
            return session, False

        return self.validate_create_session_request(session, custom_instance_type)

    @staticmethod
    def _validate_update_software_stack_request(
        software_stack: VirtualDesktopSoftwareStack,
    ) -> tuple[VirtualDesktopSoftwareStack, bool]:
        if Utils.is_empty(software_stack):
            software_stack = VirtualDesktopSoftwareStack()
            software_stack.failure_reason = 'software_stack request missing'
            return software_stack, False

        if Utils.is_any_empty(software_stack.name):
            software_stack.failure_reason = 'software_stack.name missing'
            return software_stack, False

        if Utils.is_any_empty(software_stack.description):
            software_stack.failure_reason = 'software_stack.description missing'
            return software_stack, False

        if Utils.is_empty(software_stack.base_os):
            software_stack.failure_reason = 'software_stack.base_os missing'
            return software_stack, False

        if Utils.is_empty(software_stack.projects):
            software_stack.failure_reason = 'software_stack.projects missing'
            return software_stack, False

        if (
            (software_stack.stack_id or '').startswith('ss-base-')
            and software_stack.min_storage is not None
            and software_stack.min_storage.gb() < BASE_STACK_MIN_STORAGE_GB
        ):
            software_stack.failure_reason = f'Base software stacks require at least {BASE_STACK_MIN_STORAGE_GB} GB of storage'
            return software_stack, False

        if Utils.is_empty(software_stack.pool_enabled):
            software_stack.pool_enabled = False

        # If the ASG is empty - toggle back to disabled
        if Utils.is_empty(software_stack.pool_asg_name):
            software_stack.pool_enabled = False
            software_stack.pool_asg_name = None

        # Default to 'default' launch tenancy
        if Utils.is_empty(software_stack.launch_tenancy):
            software_stack.launch_tenancy = 'default'

        for project in software_stack.projects:
            if Utils.is_empty(project.project_id):
                software_stack.failure_reason = (
                    'software_stack.project.project_id missing'
                )
                return software_stack, False

        return software_stack, True

    def _validate_create_software_stack_request(
        self, software_stack: VirtualDesktopSoftwareStack
    ) -> tuple[VirtualDesktopSoftwareStack, bool]:
        software_stack, is_valid = self.validate_create_software_stack_request(
            software_stack
        )
        if not is_valid:
            self._logger.error(software_stack.failure_reason)
            return software_stack, False

        if Utils.is_empty(software_stack.min_ram):
            software_stack.failure_reason = 'software_stack.min_ram missing'
            return software_stack, False

        if Utils.is_empty(software_stack.min_storage):
            software_stack.failure_reason = 'software_stack.min_storage missing'
            return software_stack, False

        if Utils.is_empty(software_stack.gpu):
            software_stack.failure_reason = 'software_stack.gpu missing'
            return software_stack, False

        if Utils.is_empty(software_stack.ami_id):
            software_stack.failure_reason = 'software_stack.ami_id'
            return software_stack, False

        image_description = self.controller_utils.describe_image_id(
            software_stack.ami_id
        )
        if (
            Utils.is_empty(image_description)
            or Utils.get_value_as_string('ImageId', image_description, None)
            != software_stack.ami_id
        ):
            software_stack.failure_reason = (
                f'Invalid software_stack.ami_id: {software_stack.ami_id}'
            )
            return software_stack, False

        if (
            Utils.is_not_empty(software_stack.architecture)
            and Utils.get_value_as_string('Architecture', image_description, None)
            != software_stack.architecture.value
        ):
            software_stack.failure_reason = f'Invalid software_stack.ami_id: {software_stack.ami_id} with architecture: {software_stack.architecture.value}'
            return software_stack, False

        if Utils.is_empty(software_stack.architecture):
            software_stack.architecture = VirtualDesktopArchitecture(
                Utils.get_value_as_string('Architecture', image_description, None)
            )

        if Utils.is_empty(software_stack.enabled):
            software_stack.enabled = True

        # Pool defaults to disabled
        if Utils.is_empty(software_stack.pool_enabled):
            software_stack.pool_enabled = False

        # If the ASG/ARN is empty - make sure the pool is disabled too
        if Utils.is_empty(software_stack.pool_asg_name):
            software_stack.pool_enabled = False
            software_stack.pool_asg_name = None

        if Utils.is_empty(software_stack.launch_tenancy):
            software_stack.launch_tenancy = 'default'

        return software_stack, True

    def create_session(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(CreateSessionRequest)
        session = request.session
        admin_custom_instance_type = Utils.get_as_bool(
            request.admin_custom_instance_type, default=False
        )

        if admin_custom_instance_type and session is not None:
            # the same checks as any create, except the stack's allowed sizes
            if Utils.is_empty(session.hibernation_enabled):
                session.hibernation_enabled = False
            session, is_valid = self._validate_create_session_request(
                session, custom_instance_type=True
            )
            if not is_valid:
                context.fail(
                    message=session.failure_reason,
                    payload=CreateSessionResponse(session=session),
                    error_code=errorcodes.INVALID_PARAMS,
                )
                return
            session = self.complete_create_session_request(session, context)
            session.is_launched_by_admin = True
            session = self.session_utils.create_session(session)
            if Utils.is_empty(session.failure_reason):
                context.success(CreateSessionResponse(session=session))
            else:
                context.fail(
                    message=session.failure_reason,
                    payload=CreateSessionResponse(session=session),
                    error_code=errorcodes.CREATE_SESSION_FAILED,
                )
            return

        # Normal validation flow for non-custom instance types
        session, is_valid = self._validate_create_session_request(session)
        if not is_valid:
            context.fail(
                message=session.failure_reason,
                payload=CreateSessionResponse(session=session),
                error_code=errorcodes.INVALID_PARAMS,
            )
            return

        session = self.complete_create_session_request(session, context)
        session.is_launched_by_admin = True
        session = self.session_utils.create_session(session)
        if Utils.is_empty(session.failure_reason):
            context.success(CreateSessionResponse(session=session))
        else:
            context.fail(
                message=session.failure_reason,
                payload=CreateSessionResponse(session=session),
                error_code=errorcodes.CREATE_SESSION_FAILED,
            )

    def batch_create_sessions(self, context: ApiInvocationContext):
        """
        Creates multiple sessions.
        """
        sessions = context.get_request_payload_as(BatchCreateSessionRequest).sessions
        valid_sessions = []
        failed_sessions = []

        for session in sessions:
            session, is_valid = self._validate_create_session_request(session)
            if not is_valid:
                failed_sessions.append(session)
            else:
                session = self.complete_create_session_request(session, context)
                session.is_launched_by_admin = True
                session = self.session_utils.create_session(session)
                if Utils.is_empty(session.failure_reason):
                    valid_sessions.append(session)
                else:
                    failed_sessions.append(session)

        context.success(
            BatchCreateSessionResponse(success=valid_sessions, failed=failed_sessions)
        )

    def get_session_screenshots(self, context: ApiInvocationContext):
        screenshots = context.get_request_payload_as(
            GetSessionScreenshotRequest
        ).screenshots
        valid_screenshots = []
        fail_list = []

        for screenshot in screenshots:
            screenshot, is_valid = self.validate_get_session_screenshot_request(
                screenshot
            )
            if is_valid:
                valid_screenshots.append(screenshot)
            else:
                fail_list.append(screenshot)

        valid_screenshots = self.complete_get_session_screenshots_request(
            valid_screenshots, context
        )
        success_list, fail_list_response = self._get_session_screenshots(
            valid_screenshots
        )
        fail_list.extend(fail_list_response)

        context.success(
            GetSessionScreenshotResponse(success=success_list, failed=fail_list)
        )

    def get_software_stack_info(self, context: ApiInvocationContext):
        stack_id = context.get_request_payload_as(GetSoftwareStackInfoRequest).stack_id
        if Utils.is_empty(stack_id):
            context.fail(
                error_code=errorcodes.INVALID_PARAMS, message='stack_id is missing'
            )
            return

        context.success(
            GetSoftwareStackInfoResponse(
                software_stack=self._get_software_stack_info(stack_id)
            )
        )

    def get_session_info(self, context: ApiInvocationContext):
        session = context.get_request_payload_as(GetSessionInfoRequest).session
        self.validate_get_session_info_request(session)
        session = self.complete_get_session_info_request(session, context)
        session = self._get_session_info(session)
        if Utils.is_empty(session.failure_reason):
            context.success(GetSessionInfoResponse(session=session))
        else:
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message=session.failure_reason,
                payload=GetSessionInfoResponse(session=session),
            )

    def delete_sessions(self, context: ApiInvocationContext):
        """
        Deletes multiple sessions (session ids provided)
        """
        sessions = context.get_request_payload_as(DeleteSessionRequest).sessions
        valid_sessions = []
        failed_sessions = []
        for session in sessions:
            # Preserve the force flag before validation/completion
            force = getattr(session, 'force', None)

            session, is_valid = self._validate_delete_session_request(session)
            if is_valid:
                self.complete_delete_session_request(session, context)
                # Restore the force flag after validation/completion
                session.force = force
                valid_sessions.append(session)
            else:
                failed_sessions.append(session)

        success_list, failed_list = self.session_utils.terminate_sessions(
            valid_sessions
        )
        failed_list.extend(failed_sessions)
        context.success(DeleteSessionResponse(success=success_list, failed=failed_list))

    def reboot_sessions(self, context: ApiInvocationContext):
        sessions = context.get_request_payload_as(RebootSessionRequest).sessions
        failed_sessions = []
        sessions_to_reboot = []

        for session in sessions:
            is_valid = self._validate_reboot_session_request(session)
            if not is_valid:
                failed_sessions.append(session)
                continue

            self.complete_reboot_session_request(session, context)
            sessions_to_reboot.append(session)

        success, failed = self._reboot_sessions(sessions_to_reboot)
        failed.extend(failed_sessions)
        context.success(RebootSessionResponse(success=success, failed=failed))

    def stop_sessions(self, context: ApiInvocationContext):
        sessions = context.get_request_payload_as(StopSessionRequest).sessions
        failed_sessions = []
        sessions_to_stop = []

        for session in sessions:
            is_valid = self._validate_stop_session_request(session)
            if not is_valid:
                failed_sessions.append(session)
                continue

            self.complete_stop_session_request(session, context)
            sessions_to_stop.append(session)

        success, failed = self._stop_sessions(sessions_to_stop)
        failed.extend(failed_sessions)
        context.success(StopSessionResponse(success=success, failed=failed))

    def resume_sessions(self, context: ApiInvocationContext):
        sessions = context.get_request_payload_as(ResumeSessionsRequest).sessions
        failed_sessions = []
        sessions_to_resume = []
        for session in sessions:
            session, is_valid = self._validate_resume_session_request(session)
            if not is_valid:
                failed_sessions.append(session)
                continue

            self.complete_resume_session_request(session, context)
            sessions_to_resume.append(session)

        success_list, fail_list = self._resume_sessions(
            sessions_to_resume, by_admin=True
        )
        fail_list.extend(failed_sessions)
        context.success(ResumeSessionsResponse(success=success_list, failed=fail_list))

    def list_software_stacks(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListSoftwareStackRequest)
        result = self._annotate_software_stacks(self._list_software_stacks(request))
        context.success(result)

    def extend_software_stack(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ExtendSoftwareStackRequest)
        if Utils.is_any_empty(request.stack_id, request.base_os):
            raise exceptions.invalid_params('stack_id and base_os are required')
        if request.days is not None and not 1 <= request.days <= MAX_EXTEND_DAYS:
            raise exceptions.invalid_params(
                f'days must be between 1 and {MAX_EXTEND_DAYS}'
            )
        stack = self.software_stack_db.get(
            stack_id=request.stack_id, base_os=request.base_os
        )
        if stack is None:
            raise exceptions.invalid_params(f'no such stack: {request.stack_id}')
        expiry = StackExpiry(self.context)
        updated = self.software_stack_db.set_expiry(
            stack, expiry.extended_date(stack, request.days)
        )
        expiry.annotate([updated])
        context.success(ExtendSoftwareStackResponse(software_stack=updated))

    def list_orphan_instances(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListOrphanInstancesRequest)
        # the model has already made this an int; a grace past a year is no grace period, and
        # one past what a timedelta holds would fail as a server error
        minutes = request.min_age_minutes
        if minutes is not None and not 0 <= minutes <= 525600:
            raise exceptions.invalid_params(
                'min_age_minutes must be a whole number of minutes from 0 to 525600 (a year)'
            )
        finder = OrphanInstances(self.context, self.session_db)
        context.success(
            ListOrphanInstancesResponse(
                listing=finder.list()
                if minutes is None
                else finder.list(min_age=timedelta(minutes=minutes))
            )
        )

    def list_sessions(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListSessionsRequest)

        result = self.session_db.list_from_index(request)
        context.success(result)

    def update_session(self, context: ApiInvocationContext):
        session = context.get_request_payload_as(UpdateSessionRequest).session
        self.complete_update_session_request(session, context)

        session = self._update_session(session)
        if Utils.is_not_empty(session.failure_reason):
            context.fail(
                message=session.failure_reason,
                error_code=errorcodes.UPDATE_SESSION_FAILED,
                payload=UpdateSessionResponse(session=session),
            )
        else:
            context.success(UpdateSessionResponse(session=session))

    def set_session_cleanup_exemption(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(SetSessionCleanupExemptionRequest)
        session = self.session_db.get_from_db(
            idea_session_owner=request.owner, idea_session_id=request.idea_session_id
        )
        if Utils.is_empty(session):
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message=f'invalid session {request.idea_session_id} for owner {request.owner}',
                payload=SetSessionCleanupExemptionResponse(),
            )
            return

        session = self.session_utils.set_cleanup_exemption(
            session,
            exempt=Utils.get_as_bool(request.exempt, default=False),
            reason=request.reason,
            actor=context.get_username(),
        )
        context.success(SetSessionCleanupExemptionResponse(session=session))

    def update_session_permission(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(UpdateSessionPermissionRequest)
        # a request may send any one of the three lists
        request.create = request.create or []
        request.update = request.update or []
        request.delete = request.delete or []
        is_valid_request, request = self.validate_update_session_permission_request(
            request
        )

        if not is_valid_request:
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                payload=UpdateSessionPermissionResponse(
                    permissions=[] + request.create + request.update + request.delete
                ),
                message='Invalid request. Rejecting all permissions',
            )
        else:
            response = self.session_permissions_utils.update_permission_for_sessions(
                request
            )
            context.success(response)

    def update_software_stack(self, context: ApiInvocationContext):
        new_software_stack = context.get_request_payload_as(
            UpdateSoftwareStackRequest
        ).software_stack
        # a request naming only the stack and its status changes the status alone, so a
        # toggle sent from an older listing cannot restore that listing's other fields
        if (
            new_software_stack is not None
            and 'enabled' in new_software_stack.model_fields_set
            and new_software_stack.model_fields_set <= STATUS_ONLY_STACK_FIELDS
        ):
            self._set_software_stack_status(context, new_software_stack)
            return
        new_software_stack, is_valid = self._validate_update_software_stack_request(
            new_software_stack
        )
        if not is_valid:
            context.fail(
                message=new_software_stack.failure_reason,
                error_code=errorcodes.INVALID_PARAMS,
                payload=UpdateSoftwareStackResponse(software_stack=new_software_stack),
            )
            return

        if Utils.is_any_empty(new_software_stack.stack_id):
            new_software_stack.failure_reason = 'software_stack.stack_id missing'
            context.fail(
                message=new_software_stack.failure_reason,
                error_code=errorcodes.INVALID_PARAMS,
                payload=UpdateSoftwareStackResponse(software_stack=new_software_stack),
            )
            return

        old_software_stack = self.software_stack_db.get(
            stack_id=new_software_stack.stack_id, base_os=new_software_stack.base_os
        )
        if old_software_stack is None:
            new_software_stack.failure_reason = f'Invalid id {new_software_stack.stack_id} with base os {new_software_stack.base_os}'
            context.fail(
                message=new_software_stack.failure_reason,
                error_code=errorcodes.INVALID_PARAMS,
                payload=UpdateSoftwareStackResponse(software_stack=new_software_stack),
            )
            return

        if Utils.is_not_empty(new_software_stack.description):
            old_software_stack.description = new_software_stack.description
        # empty clears the owner; absent keeps it. a date only ever moves through here on purpose
        if new_software_stack.owner is not None:
            owner = new_software_stack.owner.strip()
            if not STACK_OWNER_PATTERN.fullmatch(owner):
                raise exceptions.invalid_params(
                    'owner must be a username, or empty for no owner'
                )
            old_software_stack.owner = owner
        if new_software_stack.clear_expires_on:
            old_software_stack.expires_on = None
        elif new_software_stack.expires_on is not None:
            old_software_stack.expires_on = new_software_stack.expires_on
        if Utils.is_not_empty(new_software_stack.name):
            old_software_stack.name = new_software_stack.name
        if Utils.is_not_empty(new_software_stack.enabled):
            old_software_stack.enabled = new_software_stack.enabled
        if Utils.is_not_empty(new_software_stack.projects):
            old_software_stack.projects = new_software_stack.projects
        if Utils.is_not_empty(new_software_stack.pool_enabled):
            old_software_stack.pool_enabled = new_software_stack.pool_enabled
        if Utils.is_not_empty(new_software_stack.pool_asg_name):
            old_software_stack.pool_asg_name = new_software_stack.pool_asg_name
        if Utils.is_not_empty(new_software_stack.launch_tenancy):
            old_software_stack.launch_tenancy = new_software_stack.launch_tenancy

        if Utils.is_not_empty(new_software_stack.ami_id):
            # Validate that the AMI ID exists
            image_description = self.controller_utils.describe_image_id(
                new_software_stack.ami_id
            )
            if (
                Utils.is_empty(image_description)
                or Utils.get_value_as_string('ImageId', image_description, None)
                != new_software_stack.ami_id
            ):
                new_software_stack.failure_reason = (
                    f'Invalid AMI ID: {new_software_stack.ami_id}'
                )
                context.fail(
                    message=new_software_stack.failure_reason,
                    error_code=errorcodes.INVALID_PARAMS,
                    payload=UpdateSoftwareStackResponse(
                        software_stack=new_software_stack
                    ),
                )
                return

            # an admin's own image on a base stack is never replaced by the image pipeline:
            # anything it did not validate pins the stack (SetImagePinned-style target pin)
            if (
                new_software_stack.image_pinned is None
                and new_software_stack.ami_id != old_software_stack.ami_id
                and (old_software_stack.stack_id or '').startswith('ss-base-')
            ):
                from ideasdk.aws.image_builds import validated_image_ids
                from ideavirtualdesktopcontroller.app.software_stacks.image_pipeline import (
                    pipeline_for,
                )

                validated = validated_image_ids(pipeline_for(self).records.list_all())
                if new_software_stack.ami_id not in validated:
                    old_software_stack.image_pinned = True

            # Update the AMI ID if validation passes
            old_software_stack.ami_id = new_software_stack.ami_id

        if new_software_stack.image_pinned is not None:
            old_software_stack.image_pinned = new_software_stack.image_pinned

        # Explicitly handle allowed_instance_types, including empty lists
        # This ensures that when users clear all instance types, the change persists
        if hasattr(
            new_software_stack, 'allowed_instance_types'
        ) or 'allowed_instance_types' in Utils.to_dict(new_software_stack):
            self._logger.debug(
                f'Updating allowed_instance_types: {new_software_stack.allowed_instance_types}'
            )
            old_software_stack.allowed_instance_types = (
                new_software_stack.allowed_instance_types
            )

        # Handle min_ram update
        if Utils.is_not_empty(new_software_stack.min_ram):
            self._logger.debug(f'Updating min_ram: {new_software_stack.min_ram}')
            old_software_stack.min_ram = new_software_stack.min_ram

        # Handle min_storage update
        if Utils.is_not_empty(new_software_stack.min_storage):
            self._logger.debug(
                f'Updating min_storage: {new_software_stack.min_storage}'
            )

            # Get the AMI to check for minimum root volume size requirements
            ami_id = new_software_stack.ami_id or old_software_stack.ami_id
            if Utils.is_not_empty(ami_id):
                image_description = self.controller_utils.describe_image_id(ami_id)
                if Utils.is_not_empty(image_description):
                    # Get the root volume size from the AMI's block device mappings
                    block_device_mappings = Utils.get_value_as_list(
                        'BlockDeviceMappings', image_description, []
                    )
                    ami_root_volume_size = 0

                    for mapping in block_device_mappings:
                        # Look for the root device or the first EBS volume
                        device_name = Utils.get_value_as_string(
                            'DeviceName', mapping, ''
                        )
                        root_device_name = Utils.get_value_as_string(
                            'RootDeviceName', image_description, ''
                        )

                        if device_name == root_device_name or (
                            ami_root_volume_size == 0 and 'Ebs' in mapping
                        ):
                            # Found the root volume or first EBS volume
                            ebs = Utils.get_value_as_dict('Ebs', mapping, {})
                            volume_size = Utils.get_value_as_int('VolumeSize', ebs, 0)
                            if volume_size > ami_root_volume_size:
                                ami_root_volume_size = volume_size

                    # Check if the requested min_storage is less than the AMI's root volume size
                    if (
                        ami_root_volume_size > 0
                        and new_software_stack.min_storage.value < ami_root_volume_size
                    ):
                        new_software_stack.failure_reason = f'Minimum storage size ({new_software_stack.min_storage.value} GB) cannot be less than the AMI root volume size ({ami_root_volume_size} GB)'
                        context.fail(
                            message=new_software_stack.failure_reason,
                            error_code=errorcodes.INVALID_PARAMS,
                            payload=UpdateSoftwareStackResponse(
                                software_stack=new_software_stack
                            ),
                        )
                        return

            old_software_stack.min_storage = new_software_stack.min_storage

        # one write and one index update: the date goes with the rest of the edit
        new_software_stack = self.software_stack_db.update(
            old_software_stack,
            clear_expires_on=bool(new_software_stack.clear_expires_on),
        )

        ss_projects = []
        for project in new_software_stack.projects:
            ss_projects.append(
                self.context.projects_client.get_project(
                    GetProjectRequest(project_id=project.project_id)
                ).project
            )
        new_software_stack.projects = ss_projects

        context.success(UpdateSoftwareStackResponse(software_stack=new_software_stack))

    def move_session_to_project(self, context: ApiInvocationContext):
        """
        move a stopped desktop to another project of its owner. everything is read and
        checked first; then the row, the instance profile, the instance's tags and the
        volumes' tags change in that order, each step safe to repeat, and a failure puts
        back every step already made. costs already recorded stay with the old project.
        """
        request = context.get_request_payload_as(UpdateSessionRequest).session

        def refuse(message, error_code=errorcodes.INVALID_PARAMS):
            request.failure_reason = message
            context.fail(
                message=message,
                error_code=error_code,
                payload=UpdateSessionResponse(session=request),
            )

        # the owner's decision gives this to administrators; managers do not move desktops
        if not context.is_administrator():
            raise exceptions.unauthorized_access()
        session, previous, failure_reason = self._session_movable_to_project(request)
        if failure_reason is not None:
            refuse(failure_reason)
            return
        plan, failure_reason = self._plan_project_move(
            session, previous, context.get_username()
        )
        if failure_reason is not None:
            refuse(failure_reason)
            return

        done = []
        step = 'row'
        tagged = []
        try:
            # the row records the profile the host is on once the move completes
            session.server.instance_profile_arn = plan['target_profile_arn']
            moved = self.session_db.update(session, check_version=True)
            done.append('row')
            if plan['profile_arn'] is not None:
                step = 'profile'
                self._swap_instance_profile(plan['instance_id'], plan['profile_arn'])
                done.append('profile')
            self.server_db.update(moved.server)
            for step, resources in (
                ('instance tags', [plan['instance_id']]),
                ('volume tags', plan['volume_ids']),
            ):
                if resources:
                    tagged.clear()
                    self._apply_tags(
                        resources, plan['set_tags'], plan['remove_keys'], tagged
                    )
                    done.append(step)
        except Exception as e:
            code = (
                e.response.get('Error', {}).get('Code')
                if isinstance(e, ClientError)
                else None
            )
            if code == 'ConditionalCheckFailedException' and not done:
                refuse(SESSION_CHANGED_MESSAGE)
                return
            # an AWS refusal changed nothing at this step; any other failure (a timeout, a
            # request botocore would not send) may have, and every undo step is safe to repeat
            # the row write can land and then fail on its run record or event, and a stopped
            # host's role change is two calls: either step may be part made whatever failed
            if step not in done and (
                step in ('row', 'profile') or code is None or tagged
            ):
                done.append(step)
            self._logger.error(
                f'moving desktop {session.idea_session_id} to project '
                f'{session.project.project_id} stopped at {step}: {e}'
            )
            undone = self._undo_project_move(plan, done, session, previous)
            what = MOVE_STEP_NAMES.get(step, 'A step')
            refuse(
                f'The desktop could not be moved: {what} failed, and '
                + (
                    'nothing was changed.'
                    if undone
                    else 'some of it could not be undone; check its project, role and tags.'
                ),
                errorcodes.GENERAL_ERROR,
            )
            return

        self._logger.info(
            f'{context.get_username()} moved desktop {moved.idea_session_id} of '
            f'{moved.owner} from project {previous.project_id if previous else None} to '
            f'{moved.project.project_id}'
        )
        context.success(UpdateSessionResponse(session=moved))

    def _session_movable_to_project(self, request: VirtualDesktopSession):
        """the stored desktop with its new project, or the reason it cannot move"""
        if (
            request is None
            or Utils.is_any_empty(request.idea_session_id, request.owner)
            or request.project is None
            or Utils.is_empty(request.project.project_id)
        ):
            return (
                None,
                None,
                'session.idea_session_id, session.owner and session.project.project_id are required.',
            )
        session = self.session_db.get_from_db(
            idea_session_owner=request.owner, idea_session_id=request.idea_session_id
        )
        if session is None:
            return None, None, 'The desktop could not be found.'
        # the instance role and project file systems are attached at start, so a running
        # desktop would keep the old project's until it restarted
        if session.state != VirtualDesktopSessionState.STOPPED:
            return None, None, 'Stop the desktop before moving it to another project.'
        project_id = request.project.project_id
        if session.project is not None and session.project.project_id == project_id:
            return None, None, 'The desktop is already in that project.'
        target = self.context.projects_client.get_project(
            GetProjectRequest(project_id=project_id)
        ).project
        if target is None:
            return None, None, f'Project {project_id} was not found.'
        if not target.is_enabled():
            return None, None, f'Project {target.name} is disabled.'
        if not self.is_cluster_administrator_username(session.owner) and not any(
            project.project_id == project_id
            for project in self.context.projects_client.get_user_projects(
                username=session.owner
            )
        ):
            return (
                None,
                None,
                f'{session.owner} is not a member of project {target.name}.',
            )
        stack = (
            self.software_stack_db.get(
                stack_id=session.software_stack.stack_id,
                base_os=session.software_stack.base_os,
            )
            if session.software_stack is not None
            else None
        )
        if stack is None:
            return None, None, "The desktop's software stack no longer exists."
        if not any(project.project_id == project_id for project in stack.projects):
            return (
                None,
                None,
                f'The software stack {stack.name} is not available in project {target.name}.',
            )
        previous = session.project
        session.project = target
        return session, previous, None

    def _plan_project_move(self, session, previous, moved_by: str):
        """every read the move needs, before anything changes"""
        instance_id = session.server.instance_id if session.server else None
        if Utils.is_empty(instance_id):
            return None, 'The desktop has no host to move.'
        instance = self.controller_utils.describe_instance(instance_id)
        if instance is None:
            return None, "The desktop's host could not be read. Try again."
        if not instance:
            return None, "The desktop's host no longer exists."
        if instance.get('State', {}).get('Name') != 'stopped':
            return None, 'Stop the desktop before moving it to another project.'

        # the host ends on the profile the target project resolves to, decided from what is
        # attached now: a source project that no longer resolves to the profile its host
        # carries (budget spent, Bedrock off) must not leave that profile on after the move
        current_arn = (instance.get('IamInstanceProfile') or {}).get('Arn')
        target_arn = self._resolve_instance_profile_arn(session)
        profile_arn = target_arn if target_arn != current_arn else None

        custom = Utils.convert_custom_tags_to_key_value_pairs(
            self.context.config().get_list('global-settings.custom_tags', default=[])
        )
        new_project_tags, failure_reason = self._movable_project_tags(session.project)
        if failure_reason is not None:
            return None, failure_reason
        # the stored row does not carry the old project's tags: they are read from it
        old_project = (
            self.context.projects_client.get_project(
                GetProjectRequest(project_id=previous.project_id)
            ).project
            if previous is not None and previous.project_id
            else None
        )
        old_project_tags = {
            t.key: t.value for t in ((old_project.tags if old_project else None) or [])
        }
        set_tags = {
            constants.IDEA_TAG_PROJECT: session.project.name,
            **new_project_tags,
            f'{constants.IDEA_TAG_PREFIX}ProjectMovedBy': moved_by,
            f'{constants.IDEA_TAG_PREFIX}ProjectMovedOn': str(Utils.current_time_ms()),
        }
        remove_keys = []
        for key in old_project_tags:
            if key in set_tags or key.startswith(constants.IDEA_TAG_PREFIX):
                # a system tag the old project overrode keeps its key
                continue
            if key in custom:
                # the cluster's own custom value returns under the key
                set_tags[key] = custom[key]
            else:
                remove_keys.append(key)
        # undo puts back what the instance carries now under every key the move touches;
        # the host copied the instance's tags onto its volumes, so they get the same
        current_tags = {
            t.get('Key'): t.get('Value')
            for t in instance.get('Tags', [])
            if t.get('Key')
        }
        touched = set(set_tags) | set(remove_keys)
        restore_tags = {k: current_tags[k] for k in touched if k in current_tags}
        volume_ids = [
            m['Ebs']['VolumeId']
            for m in instance.get('BlockDeviceMappings', [])
            if m.get('Ebs', {}).get('VolumeId')
        ]
        # the volumes are retagged only while they carry this cluster's name, which the
        # host's own bootstrap writes; a volume without it would refuse the whole step
        if volume_ids:
            try:
                volumes = self.controller_utils.ec2_client.describe_volumes(
                    VolumeIds=volume_ids
                ).get('Volumes', [])
            except Exception as e:
                self._logger.warning(
                    f'could not read the volumes of {instance_id}: {e}'
                )
                return None, "The desktop's volumes could not be read. Try again."
            for volume in volumes:
                tags = {t.get('Key'): t.get('Value') for t in volume.get('Tags', [])}
                if (
                    tags.get(constants.IDEA_TAG_CLUSTER_NAME)
                    != self.context.cluster_name()
                ):
                    return None, (
                        f"The desktop's volume {volume.get('VolumeId')} has no "
                        f'{constants.IDEA_TAG_CLUSTER_NAME}={self.context.cluster_name()} tag, so its '
                        f'tags cannot be moved. Add that tag to the volume, then move the desktop.'
                    )
        return {
            'instance_id': instance_id,
            'volume_ids': volume_ids,
            'profile_arn': profile_arn,
            'target_profile_arn': target_arn,
            'previous_profile_arn': current_arn,
            'set_tags': set_tags,
            'remove_keys': remove_keys,
            'restore_tags': restore_tags,
            'undo_remove_keys': [k for k in set_tags if k not in restore_tags],
        }, None

    def _movable_project_tags(self, project):
        """
        the project's tags as the move writes them, or why they cannot be: an empty value is
        left out, as at launch; a key AWS or this cluster reserves is refused before any write
        """
        tags = {}
        for tag in project.tags or []:
            key = (tag.key or '').strip()
            value = Utils.get_as_string(tag.value)
            if Utils.is_empty(value):
                continue
            if (
                not key
                or len(key) > 128
                or len(value) > 256
                or key.lower().startswith('aws:')
                or key in MOVE_RESERVED_TAG_KEYS
            ):
                return None, (
                    f'Project {project.name} has a tag ({key or "no key"}) that cannot be put '
                    f"on a desktop. Correct the project's tags, then move the desktop."
                )
            tags[key] = value
        return tags, None

    def _swap_instance_profile(
        self, instance_id: str, instance_profile_arn: Optional[str]
    ):
        """put the instance on the profile, or on none; repeating it changes nothing"""
        ec2 = self.controller_utils.ec2_client
        association = self.controller_utils.get_instance_profile_association(
            instance_id
        )
        if association is None:
            raise ClientError(
                {'Error': {'Code': 'AssociationUnreadable', 'Message': instance_id}},
                'DescribeIamInstanceProfileAssociations',
            )
        current = (association.get('IamInstanceProfile') or {}).get('Arn')
        if current == instance_profile_arn:
            return
        if instance_profile_arn is None:
            ec2.disassociate_iam_instance_profile(
                AssociationId=association['AssociationId']
            )
        else:
            # the move's host is stopped
            self.controller_utils.put_on_instance_profile(
                instance_id,
                instance_profile_arn,
                association.get('AssociationId'),
                stopped=True,
                previous_arn=current,
            )

    def _apply_tags(self, resources, set_tags: Dict, remove_keys, progress=None):
        """progress hears 'created' once the new values are on, before any key is removed"""
        ec2 = self.controller_utils.ec2_client
        ec2.create_tags(
            Resources=resources,
            Tags=[
                {'Key': k, 'Value': Utils.get_as_string(v)} for k, v in set_tags.items()
            ],
        )
        if progress is not None:
            progress.append('created')
        if remove_keys:
            ec2.delete_tags(
                Resources=resources, Tags=[{'Key': k} for k in sorted(remove_keys)]
            )

    def _undo_project_move(self, plan, done, session, previous) -> bool:
        """put back every step made, newest first; True when all of it went back"""
        undone = True
        for step in reversed(done):
            try:
                if step in ('instance tags', 'volume tags'):
                    resources = (
                        [plan['instance_id']]
                        if step == 'instance tags'
                        else plan['volume_ids']
                    )
                    self._apply_tags(
                        resources, plan['restore_tags'], plan['undo_remove_keys']
                    )
                elif step == 'profile':
                    self._swap_instance_profile(
                        plan['instance_id'], plan['previous_profile_arn']
                    )
                elif step == 'row':
                    # read again: the write may have landed even though it did not answer
                    current = self.session_db.get_from_db(
                        idea_session_owner=session.owner,
                        idea_session_id=session.idea_session_id,
                    )
                    if (
                        current is not None
                        and current.project is not None
                        and current.project.project_id == session.project.project_id
                    ):
                        current.project = previous
                        if current.server is not None:
                            current.server.instance_profile_arn = plan[
                                'previous_profile_arn'
                            ]
                        restored = self.session_db.update(current, check_version=True)
                        if restored.server is not None:
                            self.server_db.update(restored.server)
            except Exception as e:
                undone = False
                self._logger.error(
                    f'could not undo the {step} step of a project move of '
                    f'{plan["instance_id"]}: {e}'
                )
        return undone

    def _set_software_stack_status(
        self, context: ApiInvocationContext, request: VirtualDesktopSoftwareStack
    ):
        if (
            Utils.is_any_empty(request.stack_id, request.base_os)
            or request.enabled is None
        ):
            request.failure_reason = (
                'software_stack.stack_id, base_os and enabled are required'
            )
            context.fail(
                message=request.failure_reason,
                error_code=errorcodes.INVALID_PARAMS,
                payload=UpdateSoftwareStackResponse(software_stack=request),
            )
            return
        try:
            software_stack = self.software_stack_db.set_enabled(
                request, request.enabled
            )
        except ClientError as e:
            if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            request.failure_reason = (
                f'Invalid id {request.stack_id} with base os {request.base_os}'
            )
            context.fail(
                message=request.failure_reason,
                error_code=errorcodes.INVALID_PARAMS,
                payload=UpdateSoftwareStackResponse(software_stack=request),
            )
            return
        context.success(UpdateSoftwareStackResponse(software_stack=software_stack))

    def create_software_stack(self, context: ApiInvocationContext):
        software_stack = context.get_request_payload_as(
            CreateSoftwareStackRequest
        ).software_stack
        software_stack, is_valid = self._validate_create_software_stack_request(
            software_stack
        )
        if not is_valid:
            context.fail(
                message=software_stack.failure_reason,
                error_code=errorcodes.INVALID_PARAMS,
                payload=CreateSoftwareStackResponse(software_stack=software_stack),
            )
            return

        software_stack = self._create_software_stack(software_stack)
        context.success(CreateSoftwareStackResponse(software_stack=software_stack))

    def create_software_stack_from_session(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(CreateSoftwareStackFromSessionRequest)
        session = request.session
        if Utils.is_empty(session.owner):
            context.fail(
                message='session.owner missing',
                payload=None,
                error_code=errorcodes.INVALID_PARAMS,
            )
            return

        new_software_stack = request.new_software_stack
        new_software_stack, is_valid = self.validate_create_software_stack_request(
            new_software_stack
        )
        if not is_valid:
            context.fail(
                message=new_software_stack.failure_reason,
                payload=None,
                error_code=errorcodes.INVALID_PARAMS,
            )
            return

        new_software_stack = self._create_software_stack_from_session(
            session, new_software_stack
        )
        if Utils.is_empty(new_software_stack.failure_reason):
            context.success(
                CreateSoftwareStackFromSessionResponse(
                    software_stack=new_software_stack
                )
            )
        else:
            context.fail(
                message=new_software_stack.failure_reason,
                payload=CreateSoftwareStackFromSessionResponse(
                    software_stack=new_software_stack
                ),
                error_code=errorcodes.CREATED_SOFTWARE_STACK_FROM_SESSION_FAILED,
            )

    def update_permission_profile(self, context: ApiInvocationContext):
        permission_profile = context.get_request_payload_as(
            UpdatePermissionProfileRequest
        ).profile
        existing_profile = self.permission_profile_db.get(
            profile_id=permission_profile.profile_id
        )
        if Utils.is_empty(existing_profile):
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message=f'Profile ID: {permission_profile.profile_id} does not exist',
            )
            return
        permission_profile = self.permission_profile_db.update(permission_profile)
        context.success(UpdatePermissionProfileResponse(profile=permission_profile))

    def create_permission_profile(self, context: ApiInvocationContext):
        permission_profile = context.get_request_payload_as(
            CreatePermissionProfileRequest
        ).profile
        existing_profile = self.permission_profile_db.get(
            profile_id=permission_profile.profile_id
        )
        if Utils.is_not_empty(existing_profile):
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message=f'Profile ID: {permission_profile.profile_id} is not unique. Use a unique name',
            )
            return

        permission_profile = self.permission_profile_db.create(permission_profile)
        context.success(CreatePermissionProfileResponse(profile=permission_profile))

    def refresh_base_software_stack_amis(self, context: ApiInvocationContext):
        # elevated access is enforced for every namespace in invoke()
        request = context.get_request_payload_as(RefreshBaseSoftwareStackAmisRequest)
        results = self.software_stack_utils.refresh_base_software_stack_amis(
            stack_ids=request.stack_ids
        )
        context.success(RefreshBaseSoftwareStackAmisResponse(results=results))

    # desktop images (Custom AMIs page)

    @property
    def desktop_images(self):
        # created on first use: it opens the image-builds table
        if self._desktop_images is None:
            from ideavirtualdesktopcontroller.app.software_stacks.desktop_images import (
                DesktopImageService,
            )

            self._desktop_images = DesktopImageService(
                self.context, self.software_stack_db, self.software_stack_utils
            )
        return self._desktop_images

    def list_desktop_images(self, context: ApiInvocationContext):
        context.get_request_payload_as(ListDesktopImagesRequest)
        context.success(
            ListDesktopImagesResponse(listing=self.desktop_images.list_images())
        )

    def build_desktop_image(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(BuildDesktopImageRequest)
        record = self.desktop_images.build(request, requested_by=context.get_username())
        context.success(BuildDesktopImageResponse(record=record))

    def build_all_desktop_images(self, context: ApiInvocationContext):
        context.get_request_payload_as(BuildAllDesktopImagesRequest)
        results = self.desktop_images.build_all(requested_by=context.get_username())
        context.success(BuildAllDesktopImagesResponse(results=results))

    def use_built_desktop_images(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(UseBuiltDesktopImagesRequest)
        results = self.desktop_images.use_built_images(
            request.stack_ids, requested_by=context.get_username()
        )
        context.success(UseBuiltDesktopImagesResponse(results=results))

    # image pipeline (Images view): desktop rows. the bakes run in the controller
    # leader loop; these only list, queue, roll back and pin (image_pipeline.py)

    def list_image_rows(self, context: ApiInvocationContext):
        from ideavirtualdesktopcontroller.app.software_stacks.image_pipeline import (
            pipeline_for,
        )
        from ideadatamodel import ListImageRowsResponse

        request = context.get_request_payload_as(ListImageRowsRequest)
        pipeline = pipeline_for(self)
        pipeline._check_kind(request.filter)
        context.success(
            ListImageRowsResponse(listing=pipeline.list_rows(request.filter))
        )

    def refresh_images(self, context: ApiInvocationContext):
        from ideavirtualdesktopcontroller.app.software_stacks.image_pipeline import (
            pipeline_for,
        )
        from ideadatamodel import RefreshImagesResponse

        request = context.get_request_payload_as(RefreshImagesRequest)
        if request.force and not context.is_administrator():
            raise exceptions.unauthorized_access(
                'only an administrator can force a rebake'
            )
        results = pipeline_for(self).refresh(request, context.get_username())
        context.success(RefreshImagesResponse(results=results))

    def rollback_image(self, context: ApiInvocationContext):
        from ideavirtualdesktopcontroller.app.software_stacks.image_pipeline import (
            pipeline_for,
        )
        from ideadatamodel import RollbackImageResponse

        request = context.get_request_payload_as(RollbackImageRequest)
        if request.row is None:
            raise exceptions.invalid_params('row is required')
        record = pipeline_for(self).rollback(request.row, context.get_username())
        context.success(RollbackImageResponse(record=record))

    def set_image_pinned(self, context: ApiInvocationContext):
        from ideavirtualdesktopcontroller.app.software_stacks.image_pipeline import (
            pipeline_for,
        )
        from ideadatamodel import SetImagePinnedResponse

        request = context.get_request_payload_as(SetImagePinnedRequest)
        if request.row is None or request.pinned is None:
            raise exceptions.invalid_params('row and pinned are required')
        record = pipeline_for(self).set_pinned(request.row, request.pinned)
        context.success(SetImagePinnedResponse(record=record))

    def _image_schedule_next_run(self, schedule: ImageRefreshSchedule):
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(self.context.cluster_timezone()))
        return schedule.next_run_after(now)

    def get_image_schedule(self, context: ApiInvocationContext):
        context.get_request_payload_as(GetImageScheduleRequest)
        config = self.context.config()
        schedule = ImageRefreshSchedule(
            **dict(config.get_config(IMAGE_REFRESH_SCHEDULE_KEY, default={}) or {})
        )
        last_run_ms = config.get_int(IMAGE_REFRESH_LAST_RUN_KEY, default=None)
        context.success(
            GetImageScheduleResponse(
                schedule=schedule,
                last_run_on=Utils.to_datetime(last_run_ms) if last_run_ms else None,
                next_run_on=self._image_schedule_next_run(schedule),
            )
        )

    def update_image_schedule(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(UpdateImageScheduleRequest)
        schedule = request.schedule
        if schedule is None:
            raise exceptions.invalid_params('schedule is required')
        schedule = ImageRefreshSchedule(
            enabled=bool(schedule.enabled),
            day=(schedule.day or '').strip().lower(),
            hour=schedule.hour,
        )
        try:
            schedule.validate_rule()
        except ValueError as e:
            raise exceptions.invalid_params(str(e))
        config = self.context.config()
        for field in ('enabled', 'day', 'hour'):
            key = config.get_real_key(f'{IMAGE_REFRESH_SCHEDULE_KEY}.{field}')
            config.db.set_config_entry(key, getattr(schedule, field))
            # the settings stream catches up later; reads on this host see it now
            config.put(key, getattr(schedule, field))
        context.success(
            UpdateImageScheduleResponse(
                schedule=schedule, next_run_on=self._image_schedule_next_run(schedule)
            )
        )

    def re_index_software_stacks(self, context: ApiInvocationContext):
        # got a request to reindex everything again.
        request = ListSoftwareStackRequest()
        request.disabled_also = True
        response = self.software_stack_db.list_all_from_db(request)

        while True:
            for software_stack in response.listing:
                self.software_stack_utils.index_software_stack_entry_to_opensearch(
                    software_stack=software_stack
                )

            if Utils.is_empty(response.cursor):
                # this was the last page,
                break

            request.paginator = response.paginator
            response = self.software_stack_db.list_all_from_db(request)

        context.success(ReIndexSoftwareStacksResponse())

    def re_index_user_sessions(self, context: ApiInvocationContext):
        # got a request to reindex everything again.

        request = ListSessionsRequest()
        response = self.session_db.list_all_from_db(request)

        while True:
            for session in response.listing:
                self.session_utils.index_session_entry_to_opensearch(session=session)

            if Utils.is_empty(response.cursor):
                # this was the last page,
                break

            request.paginator = response.paginator
            response = self.session_db.list_all_from_db(request)

        context.success(ReIndexUserSessionsResponse())

    def get_session_connection_info(self, context: ApiInvocationContext):
        self._logger.info(
            f'received get session connection info request from user: {context.get_username()}'
        )
        connection_info_request = context.get_request_payload_as(
            GetSessionConnectionInfoRequest
        ).connection_info
        message, is_valid = self.validate_get_connection_info_request(
            connection_info_request
        )
        if not is_valid:
            context.fail(
                message=message,
                error_code=errorcodes.INVALID_PARAMS,
                payload=GetSessionConnectionInfoResponse(
                    connection_info=connection_info_request
                ),
            )
            return

        if Utils.is_empty(connection_info_request.username):
            connection_info_request.username = context.get_username()

        connection_info = self._get_session_connection_info(
            connection_info_request, context
        )

        if Utils.is_empty(connection_info.failure_reason):
            context.success(
                GetSessionConnectionInfoResponse(connection_info=connection_info)
            )
        else:
            context.fail(
                message=connection_info.failure_reason,
                error_code=errorcodes.SESSION_CONNECTION_ERROR,
                payload=GetSessionConnectionInfoResponse(
                    connection_info=connection_info
                ),
            )

    def list_shared_permissions(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListPermissionsRequest)

        username = request.username
        if Utils.is_empty(username):
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message='username missing',
                payload=request,
            )
            return

        response = self._list_shared_permissions(username=username, request=request)
        context.success(response)

    def list_session_permissions(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListPermissionsRequest)
        idea_session_id = request.idea_session_id
        if Utils.is_empty(idea_session_id):
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message='idea_session_id missing',
                payload=request,
            )
            return

        response = self._list_session_permissions(
            idea_session_id=idea_session_id, request=request
        )
        context.success(response)

    def delete_software_stack(self, context: ApiInvocationContext):
        """
        Delete one or more software stacks
        """
        # Get the raw request payload first
        request_payload = context.request_payload

        # Determine if we have software_stacks or software_stack in the raw payload
        software_stacks = []
        if (
            'software_stacks' in request_payload
            and request_payload['software_stacks'] is not None
        ):
            # Handle array of software stacks
            for stack_data in request_payload['software_stacks']:
                software_stack = VirtualDesktopSoftwareStack(**stack_data)
                software_stacks.append(software_stack)
        elif (
            'software_stack' in request_payload
            and request_payload['software_stack'] is not None
        ):
            # Handle single software stack
            software_stack = VirtualDesktopSoftwareStack(
                **request_payload['software_stack']
            )
            software_stacks.append(software_stack)
        else:
            # No valid stacks found
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message='Invalid request: software_stack or software_stacks is required',
            )
            return

        success_list = []
        failed_list = []

        for software_stack in software_stacks:
            try:
                if software_stack.stack_id is None:
                    # Create a minimal error response
                    error_stack = VirtualDesktopSoftwareStack()
                    error_stack.failure_reason = 'Invalid request: stack_id is required'
                    failed_list.append(error_stack)
                    continue

                # For deletions, we only need the stack_id - get the full object from the database
                stack_id = software_stack.stack_id

                # First try to get by stack_id and base_os if available
                db_software_stack = None
                if (
                    hasattr(software_stack, 'base_os')
                    and software_stack.base_os is not None
                ):
                    db_software_stack = self.software_stack_db.get(
                        stack_id=stack_id, base_os=software_stack.base_os
                    )

                # If that didn't work, try getting by just stack_id
                if db_software_stack is None:
                    db_software_stack = self.software_stack_db.get_from_index(
                        stack_id=stack_id
                    )

                if db_software_stack is None:
                    error_stack = VirtualDesktopSoftwareStack()
                    error_stack.stack_id = stack_id
                    error_stack.failure_reason = (
                        f'Software stack with ID {stack_id} not found'
                    )
                    failed_list.append(error_stack)
                    continue

                # Delete the software stack from the database
                self.software_stack_db.delete(db_software_stack)

                # Also delete from OpenSearch index
                self.software_stack_utils.delete_software_stack_entry_from_opensearch(
                    db_software_stack.stack_id
                )

                # Create a new VirtualDesktopSoftwareStack object with minimal info to return
                success_stack = VirtualDesktopSoftwareStack(
                    stack_id=db_software_stack.stack_id,
                    base_os=db_software_stack.base_os,
                    name=db_software_stack.name,
                )
                success_list.append(success_stack)
            except Exception as e:
                self._logger.error(f'Failed to delete software stack: {str(e)}')
                error_stack = VirtualDesktopSoftwareStack()
                # Copy any available identifying info
                if hasattr(software_stack, 'stack_id'):
                    error_stack.stack_id = software_stack.stack_id
                if hasattr(software_stack, 'name'):
                    error_stack.name = software_stack.name
                error_stack.failure_reason = (
                    f'Failed to delete software stack: {str(e)}'
                )
                failed_list.append(error_stack)

        # Single software stack response for backward compatibility
        single_software_stack = (
            success_list[0]
            if len(success_list) == 1 and len(failed_list) == 0
            else None
        )

        # Create response
        response = DeleteSoftwareStackResponse(
            software_stack=single_software_stack,
            software_stacks=success_list if len(success_list) > 0 else None,
            success=len(success_list) > 0 and len(failed_list) == 0,
            success_list=success_list,
            failed_list=failed_list,
        )

        context.success(payload=response)

    def invoke(self, context: ApiInvocationContext):
        namespace = context.namespace

        # an unmapped namespace passes no scopes, leaving elevated users as the only
        # callers.
        access = APP_TOKEN_NAMESPACE_ACCESS.get(namespace)
        scopes = [f'{self.context.module_id()}/{access}'] if access else None

        if not context.is_authorized(elevated_access=True, scopes=scopes):
            raise exceptions.unauthorized_access()

        if namespace in self.namespace_handler_map:
            self.namespace_handler_map[namespace](context)
