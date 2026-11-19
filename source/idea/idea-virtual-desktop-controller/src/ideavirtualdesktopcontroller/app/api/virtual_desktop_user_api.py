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
import threading
from typing import Dict, Callable

import ideavirtualdesktopcontroller
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_utils import (
    GROUP_UNREADABLE_MESSAGE,
    group_state,
)
from ideavirtualdesktopcontroller.app.session_permissions.constants import (
    GROUP_ACTOR_PREFIX,
    bare_actor_name,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    PROJECT_HOLD_MESSAGES,
    owner_project_refusal,
)
from python_dynamodb_lock.python_dynamodb_lock import DynamoDBLockError
from ideadatamodel import GetUserRequest, VirtualDesktopSessionPermissionActorType
from ideadatamodel import (
    CreateSessionRequest,
    CreateSessionResponse,
    GetSessionConnectionInfoRequest,
    GetSessionConnectionInfoResponse,
    GetSessionScreenshotRequest,
    GetSessionScreenshotResponse,
    UpdateSessionRequest,
    UpdateSessionResponse,
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
    ListSoftwareStackRequest,
    ListPermissionsRequest,
    UpdateSessionPermissionRequest,
    UpdateSessionPermissionResponse,
    VirtualDesktopSession,
    VirtualDesktopSessionScreenshot,
    VirtualDesktopSessionConnectionInfo,
    SocaFilter,
)
from ideadatamodel import errorcodes, exceptions
from ideasdk.api import ApiInvocationContext
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.software_stacks.stack_expiry import StackExpiry
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import VirtualDesktopAPI
from ideavirtualdesktopcontroller.app.software_stacks.constants import (
    SOFTWARE_STACK_DB_FILTER_PROJECT_ID_KEY,
)
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_db import (
    VirtualDesktopSessionPermissionDB,
)


# one create at a time per user in this process, so two threads never hold the same lock key
# username -> [lock, requests holding or waiting]; an entry goes when its last request ends
_CREATE_LOCKS: Dict[str, list] = {}
_CREATE_LOCKS_GUARD = threading.Lock()
# the distributed lock gives up after its lease (30 s) plus one heartbeat (5 s); the
# in-process wait is bounded the same way
CREATE_LOCK_WAIT_SECONDS = 35
CREATE_BUSY_MESSAGE = (
    'Another desktop request of yours is still being processed. Try again shortly.'
)


class VirtualDesktopUserAPI(VirtualDesktopAPI):
    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        super().__init__(context)
        self.context = context
        self._logger = context.logger('virtual-desktop-user-api')

        self.namespace_handler_map: Dict[str, Callable] = {
            'VirtualDesktop.CreateSession': self.create_session,
            'VirtualDesktop.UpdateSession': self.update_session,
            'VirtualDesktop.DeleteSessions': self.delete_sessions,
            'VirtualDesktop.GetSessionInfo': self.get_session_info,
            'VirtualDesktop.GetSessionScreenshot': self.get_session_screenshots,
            'VirtualDesktop.GetSessionConnectionInfo': self.get_session_connection_info,
            'VirtualDesktop.ListSessions': self.list_sessions,
            'VirtualDesktop.StopSessions': self.stop_sessions,
            'VirtualDesktop.ResumeSessions': self.resume_sessions,
            'VirtualDesktop.RebootSessions': self.reboot_sessions,
            'VirtualDesktop.ListSoftwareStacks': self.list_software_stacks,
            'VirtualDesktop.ListSharedPermissions': self.list_shared_permissions,
            'VirtualDesktop.ListSessionPermissions': self.list_session_permissions,
            'VirtualDesktop.UpdateSessionPermissions': self.update_session_permission,
        }

    @staticmethod
    def _validate_owner(owner: str, context: ApiInvocationContext) -> tuple[str, bool]:
        if not Utils.is_empty(owner) and owner != context.get_username():
            return (
                'Invalid owner provided. Non Admin users can submit requests for themselves only',
                False,
            )
        return '', True

    def _validate_reboot_session_request(
        self, session: VirtualDesktopSession, context: ApiInvocationContext
    ) -> tuple[VirtualDesktopSession, bool]:
        self.validate_reboot_session_request(session)
        message, is_valid = self._validate_owner(session.owner, context)
        if not is_valid:
            session.failure_reason = message

        return session, is_valid

    def _validate_stop_session_request(
        self, session: VirtualDesktopSession, context: ApiInvocationContext
    ) -> tuple[VirtualDesktopSession, bool]:
        self.validate_stop_session_request(session)
        message, is_valid = self._validate_owner(session.owner, context)
        if not is_valid:
            session.failure_reason = message

        return session, is_valid

    def _validate_delete_session_request(
        self, session: VirtualDesktopSession, context: ApiInvocationContext
    ) -> tuple[VirtualDesktopSession, bool]:
        valid_response = True
        session, is_valid = self.validate_delete_session_request(session)
        if not is_valid:
            valid_response = False

        error_message, is_valid = self._validate_owner(session.owner, context)
        if not is_valid:
            session.failure_reason = Utils.get_as_string(
                value=session.failure_reason, default=''
            ) + str(error_message)
            valid_response = False
        return session, valid_response

    def _validate_get_session_info_request(
        self, session: VirtualDesktopSession, context: ApiInvocationContext
    ) -> tuple[VirtualDesktopSession, bool]:
        self.validate_get_session_info_request(session)
        message, is_valid = self._validate_owner(session.owner, context)
        if not is_valid:
            session.failure_reason = message

        return session, is_valid

    def _validate_get_connection_info_request(
        self,
        connection_info: VirtualDesktopSessionConnectionInfo,
        context: ApiInvocationContext,
    ) -> tuple[VirtualDesktopSessionConnectionInfo, bool]:
        message, is_valid = self.validate_get_connection_info_request(connection_info)
        if not is_valid:
            connection_info.failure_reason = message
            return connection_info, False

        message, is_valid = self._validate_owner(connection_info.username, context)
        if not is_valid:
            connection_info.failure_reason = message
            return connection_info, False

        if not self._may_connect(connection_info, context.get_username()):
            if connection_info.failure_reason not in (
                *PROJECT_HOLD_MESSAGES,
                GROUP_UNREADABLE_MESSAGE,
            ):
                connection_info.failure_reason = 'The desktop could not be found.'
            return connection_info, False
        return connection_info, True

    def _may_connect(
        self, connection_info: VirtualDesktopSessionConnectionInfo, username: str
    ) -> bool:
        # a broker token only for a desktop the caller owns or holds a share on;
        # a dcv_session_id alone names no owner, so the desktop's id is required
        if Utils.is_empty(connection_info.idea_session_id):
            return False
        owner = connection_info.idea_session_owner or username
        session = self.session_db.get_from_db(
            idea_session_owner=owner, idea_session_id=connection_info.idea_session_id
        )
        if session is None:
            return False
        # a desktop whose owner left its project waits for an administrator to move it;
        # nobody starts a new connection to it, its owner or a share recipient
        refusal = owner_project_refusal(self.context, session)
        if refusal is not None:
            connection_info.failure_reason = refusal
            return False
        if owner == username:
            return True
        permission = self.session_permissions_db.get(
            idea_session_id=connection_info.idea_session_id, actor=username
        )
        # the row under the caller's own name must be a user share: a group row stored
        # under a bare name before group shares were keyed admits nobody
        if not (
            permission is not None
            and permission.actor_type == VirtualDesktopSessionPermissionActorType.USER
            and self._share_admits(permission, owner)
        ):
            self._group_unreadable = False
            permission = self._group_share_for(
                connection_info.idea_session_id, owner, username
            )
            if permission is None and self._group_unreadable:
                connection_info.failure_reason = GROUP_UNREADABLE_MESSAGE
        if permission is None:
            return False
        # the share was checked against the desktop's project when it was made; a recipient
        # who has since left the project, or lost desktop access, connects no more
        if session.project is None:
            return False
        allowed, _ = self._validate_user_and_project_membership(
            username, session.project.project_id
        )
        if (
            allowed
            and permission.actor_type == VirtualDesktopSessionPermissionActorType.GROUP
        ):
            # the host lists a group's members as of its last write: rewrite it now, so a
            # member just added is admitted by the display server as well as by this check
            try:
                self.context.dcv_broker_client.enforce_session_permissions(session)
            except Exception as e:
                self._logger.warning(
                    f'could not refresh the permissions of {session.idea_session_id} '
                    f'for group member {username}: {e}'
                )
        return allowed

    @staticmethod
    def _share_admits(permission, owner: str) -> bool:
        return (
            permission is not None
            and permission.idea_session_owner == owner
            and not VirtualDesktopSessionPermissionDB.is_expired(permission)
        )

    def _group_share_for(self, idea_session_id: str, owner: str, username: str):
        """
        a group share on the desktop naming a group the caller is in now, by the accounts
        service; it is read at every connection, so a member who was removed cannot connect
        """
        group_shares = [
            permission
            for permission in self.session_permissions_db.get_for_session(
                idea_session_id
            )
            if permission.actor_type == VirtualDesktopSessionPermissionActorType.GROUP
            and (permission.actor_name or '').startswith(GROUP_ACTOR_PREFIX)
            and self._share_admits(permission, owner)
        ]
        if not group_shares:
            return None
        user = self.context.accounts_client.get_user(
            GetUserRequest(username=username)
        ).user
        groups = set(Utils.get_as_list(user.additional_groups, []) if user else [])
        for permission in group_shares:
            group = bare_actor_name(permission.actor_name)
            if group not in groups:
                continue
            state = group_state(self.context, group)
            if state == 'enabled':
                return permission
            if state == 'unreadable':
                self._group_unreadable = True
        return None

    @staticmethod
    def _validate_update_session_permission_request(
        request: UpdateSessionPermissionRequest, context: ApiInvocationContext
    ) -> tuple[str, bool]:
        for permission in request.create + request.delete + request.update:
            if permission.idea_session_owner != context.get_username():
                return 'Can update permission for session owned by user only', False
        return '', True

    def _validate_update_session_request(
        self, session: VirtualDesktopSession, context: ApiInvocationContext
    ) -> tuple[VirtualDesktopSession, bool]:
        message, is_valid = self._validate_owner(session.owner, context)
        if not is_valid:
            session.failure_reason = message
            return session, False

        session.owner = context.get_username()
        if Utils.is_empty(session.server) or Utils.is_empty(
            session.server.instance_type
        ):
            return session, True

        current_session = self.session_db.get_from_db(
            idea_session_id=session.idea_session_id,
            idea_session_owner=session.owner,
        )
        if Utils.is_empty(current_session):
            return session, True
        # the rename and schedule forms send the current type back; only a change is checked,
        # so a desktop on a type that is no longer allowed can still be renamed or scheduled.
        if (
            Utils.is_not_empty(current_session.server)
            and session.server.instance_type == current_session.server.instance_type
        ):
            return session, True

        # the stack as an administrator now has it: the copy on the desktop is not refreshed
        # when its allowed sizes change. a stack since deleted falls back to that copy.
        software_stack = current_session.software_stack
        if software_stack is not None and Utils.is_not_empty(software_stack.stack_id):
            software_stack = (
                self.software_stack_db.get(
                    stack_id=software_stack.stack_id, base_os=software_stack.base_os
                )
                or software_stack
            )
        failure_reason = self.controller_utils.get_instance_type_rejection_reason(
            instance_type_name=session.server.instance_type,
            hibernation_support=current_session.hibernation_enabled,
            software_stack=software_stack,
            username=session.owner,
        )
        if Utils.is_not_empty(failure_reason):
            session.failure_reason = failure_reason
            return session, False

        return session, True

    def _validate_create_session_request(
        self, session: VirtualDesktopSession, context: ApiInvocationContext
    ) -> tuple[VirtualDesktopSession, bool]:
        # Validate Session Object
        if Utils.is_empty(session):
            session = VirtualDesktopSession()
            session.failure_reason = 'Missing Create Session Info'
            return session, False

        message, is_valid = self._validate_owner(session.owner, context)
        if not is_valid:
            session.failure_reason = Utils.get_as_string(
                value=session.failure_reason, default=''
            ) + str(message)
            self._logger.error(session.failure_reason)
            return session, False

        session.owner = context.get_username()
        session_count_for_user = self.session_db.get_session_count_for_user(
            context.get_username()
        )
        if session_count_for_user >= self.context.config().get_int(
            'virtual-desktop-controller.dcv_session.allowed_sessions_per_user',
            required=True,
        ):
            session.failure_reason = f'User {session.owner} has exceeded the allowed number of sessions: {session_count_for_user}. Please contact the System Administrators if you need to create more sessions.'
            return session, False

        return self.validate_create_session_request(session)

    def _validate_get_session_screenshot_request(
        self, screenshot: VirtualDesktopSessionScreenshot, context: ApiInvocationContext
    ) -> tuple[VirtualDesktopSessionScreenshot, bool]:
        # Only the stored, owned session may select the broker resource.
        if Utils.is_empty(screenshot.idea_session_id):
            screenshot.failure_reason = 'Provide idea_session_id'
            return screenshot, False
        screenshot.dcv_session_id = None
        screenshot, is_valid = self.validate_get_session_screenshot_request(screenshot)
        validation_response = True
        if not is_valid:
            validation_response = False

        error_message, is_valid = self._validate_owner(
            screenshot.idea_session_owner, context
        )
        if not is_valid:
            screenshot.failure_reason = Utils.get_as_string(
                value=screenshot.failure_reason, default=''
            ) + str(error_message)
            self._logger.info(f'{screenshot.failure_reason}')
            validation_response = False
        if validation_response:
            session = self.session_db.get_from_db(
                idea_session_owner=screenshot.idea_session_owner
                or context.get_username(),
                idea_session_id=screenshot.idea_session_id,
            )
            refusal = (
                owner_project_refusal(self.context, session)
                if session is not None
                else None
            )
            if refusal is not None:
                # returned in failed[] with this desktop's idea_session_id and this
                # failure_reason; dcv_session_id is not set on a refused screenshot
                screenshot.failure_reason = refusal
                validation_response = False
        return screenshot, validation_response

    def _validate_resume_session_request(
        self, session: VirtualDesktopSession, context: ApiInvocationContext
    ) -> tuple[VirtualDesktopSession, bool]:
        session, is_valid = self.validate_resume_session_request(session)
        if not is_valid:
            return session, is_valid

        message, is_valid = self._validate_owner(session.owner, context)
        if not is_valid:
            session.failure_reason = message

        return session, is_valid

    def create_session(self, context: ApiInvocationContext):
        """
        Creates a single session.
        The username is retrieved from the token to ensure that the session created is in the user's name only.
        """
        self._logger.debug(
            f'received create session request from user: {context.get_username()}'
        )
        # the per-user limit counts stored rows, and the row is written only after the host
        # launches, so the count and the launch run under one lock per user
        # the launch stays inside: the row the count reads is written only after it
        username = context.get_username()
        with _CREATE_LOCKS_GUARD:
            entry = _CREATE_LOCKS.setdefault(username, [threading.Lock(), 0])
            entry[1] += 1
        try:
            if not entry[0].acquire(timeout=CREATE_LOCK_WAIT_SECONDS):
                self._create_busy(context)
                return
            try:
                self._create_under_distributed_lock(context, username)
            finally:
                entry[0].release()
        finally:
            with _CREATE_LOCKS_GUARD:
                entry[1] -= 1
                if entry[1] == 0:
                    del _CREATE_LOCKS[username]

    def _create_under_distributed_lock(self, context: ApiInvocationContext, username):
        lock = self.context.distributed_lock()
        lock_key = f'create-session-{username}'
        try:
            lock.acquire(key=lock_key)
        except DynamoDBLockError as e:
            if e.code != DynamoDBLockError.ACQUIRE_TIMEOUT:
                raise
            self._create_busy(context)
            return
        try:
            self._create_session(context)
        finally:
            lock.release(key=lock_key)

    def _create_busy(self, context: ApiInvocationContext):
        self._logger.info(
            f'create session for {context.get_username()}: another create is in progress'
        )
        context.fail(
            error_code=errorcodes.CREATE_SESSION_FAILED,
            message=CREATE_BUSY_MESSAGE,
            payload=CreateSessionResponse(
                session=context.get_request_payload_as(CreateSessionRequest).session
            ),
        )

    def _create_session(self, context: ApiInvocationContext):
        session = context.get_request_payload_as(CreateSessionRequest).session
        session, is_valid = self._validate_create_session_request(session, context)
        if not is_valid:
            self._logger.error(session.failure_reason)
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message=session.failure_reason,
                payload=CreateSessionResponse(session=session),
            )
            return

        session = self.complete_create_session_request(session, context)
        session.is_launched_by_admin = False
        session = self.session_utils.create_session(session)

        if Utils.is_empty(session.failure_reason):
            self._logger.debug(
                f'session request created for user: {context.get_username()} with session name: {session.name} and idea_session_id: {session.idea_session_id}:{session.name}'
            )
            context.success(CreateSessionResponse(session=session))
        else:
            context.fail(
                message=session.failure_reason,
                error_code=errorcodes.CREATE_SESSION_FAILED,
                payload=CreateSessionResponse(session=session),
            )

    def get_session_connection_info(self, context: ApiInvocationContext):
        self._logger.info(
            f'received get session connection info request from user: {context.get_username()}'
        )
        connection_info_request = context.get_request_payload_as(
            GetSessionConnectionInfoRequest
        ).connection_info
        connection_info_request, is_valid_idea_session = (
            self._validate_get_connection_info_request(connection_info_request, context)
        )
        if not is_valid_idea_session:
            context.fail(
                message=connection_info_request.failure_reason,
                error_code=errorcodes.INVALID_PARAMS,
                payload=GetSessionConnectionInfoResponse(
                    connection_info=connection_info_request
                ),
            )
            return

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

    def get_session_screenshots(self, context: ApiInvocationContext):
        screenshots = context.get_request_payload_as(
            GetSessionScreenshotRequest
        ).screenshots
        valid_screenshots = []
        fail_list = []

        for screenshot in screenshots:
            screenshot, is_valid = self._validate_get_session_screenshot_request(
                screenshot, context
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

    def get_session_info(self, context: ApiInvocationContext):
        session = context.get_request_payload_as(GetSessionInfoRequest).session
        session, is_valid = self._validate_get_session_info_request(session, context)
        if not is_valid:
            self._logger.error(session.failure_reason)
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message=session.failure_reason,
                payload=CreateSessionResponse(session=session),
            )
            return

        session = self.complete_get_session_info_request(session, context)
        # The stored row proves ownership; the index carries the enriched shape
        # (server.private_ip etc.) that callers read. A stored failure_reason
        # is a diagnostic on an owned desktop, not a failed request.
        indexed = None
        if (
            self.get_session_if_owner(context.get_username(), session.idea_session_id)
            is None
        ):
            session.failure_reason = 'The desktop could not be found.'
        else:
            indexed = self.session_db.get_from_index(
                idea_session_id=session.idea_session_id
            )
            if indexed is None:
                session.failure_reason = f'invalid session.idea_session_id: {session.idea_session_id} for {session.name} owned by user: {session.owner}'

        if indexed is not None:
            context.success(GetSessionInfoResponse(session=indexed))
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

            session, is_valid = self._validate_delete_session_request(session, context)
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
            session, is_valid = self._validate_reboot_session_request(session, context)
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
            session, is_valid = self._validate_stop_session_request(session, context)
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
            session, is_valid = self._validate_resume_session_request(session, context)
            if not is_valid:
                failed_sessions.append(session)
                continue

            self.complete_resume_session_request(session, context)
            sessions_to_resume.append(session)

        success_list, fail_list = self._resume_sessions(sessions_to_resume)
        fail_list.extend(failed_sessions)
        context.success(ResumeSessionsResponse(success=success_list, failed=fail_list))

    def update_session(self, context: ApiInvocationContext):
        session = context.get_request_payload_as(UpdateSessionRequest).session
        session, is_valid = self._validate_update_session_request(session, context)
        if not is_valid:
            self._logger.error(session.failure_reason)
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message=session.failure_reason,
                payload=CreateSessionResponse(session=session),
            )
            return

        self.complete_update_session_request(session, context)
        session = self._update_session(session)
        if Utils.is_not_empty(session.failure_reason):
            self._logger.error(session.failure_reason)
            context.fail(
                message=session.failure_reason,
                error_code=errorcodes.UPDATE_SESSION_FAILED,
                payload=UpdateSessionResponse(session=session),
            )
        else:
            context.success(UpdateSessionResponse(session=session))

    def list_sessions(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListSessionsRequest)

        result = self.session_db.list_all_for_user(request, context.get_username())
        context.success(result)

    def list_software_stacks(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListSoftwareStackRequest)
        project_id = request.project_id
        if Utils.is_empty(project_id):
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message='missing project_id',
                payload=request,
            )
            return

        # a person sees the stacks of their own projects. administrators and managers list every
        # stack through the admin API already, and the admin create form lists here for the
        # person it creates a desktop for.
        if not (
            context.is_administrator()
            or context.is_manager()
            or any(
                project.project_id == project_id
                for project in self.context.projects_client.get_user_projects(
                    username=context.get_username()
                )
            )
        ):
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message="You aren't a member of this project.",
                payload=request,
            )
            return

        project_filter_found = False
        if Utils.is_not_empty(request.filters):
            for listing_filter in request.filters:
                if listing_filter.key == SOFTWARE_STACK_DB_FILTER_PROJECT_ID_KEY:
                    listing_filter.value = project_id
                    break

        if not project_filter_found:
            request.add_filter(
                SocaFilter(
                    key=SOFTWARE_STACK_DB_FILTER_PROJECT_ID_KEY, value=project_id
                )
            )

        result = self._list_software_stacks(request)
        # what a person may pick for a new desktop: their existing desktops never read this
        # ponytail: removed from the page read, so a page may come back short; the form reads one
        # page large enough for every stack. move this into the query if that ever changes.
        try:
            offered = StackExpiry(self.context).offered_for_new_desktops(
                result.listing or []
            )
            if result.paginator is not None and result.paginator.total is not None:
                result.paginator.total -= len(result.listing or []) - len(offered)
            result.listing = offered
        except Exception as e:
            self._logger.warning(f'could not check stack expiry: {type(e).__name__}')
        context.success(result)

    def list_shared_permissions(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListPermissionsRequest)
        response = self._list_shared_permissions(
            username=context.get_username(), request=request
        )
        context.success(response)

    def list_session_permissions(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(ListPermissionsRequest)
        idea_session_id = request.idea_session_id
        if Utils.is_empty(idea_session_id):
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message='missing idea_session_id',
                payload=request,
            )
            return

        session = self.get_session_if_owner(
            username=context.get_username(), idea_session_id=idea_session_id
        )
        if Utils.is_empty(session):
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                message=f'Only session owner can request to list_session_permissions for session {idea_session_id}',
                payload=request,
            )
            return

        response = self._list_session_permissions(
            idea_session_id=idea_session_id, request=request
        )
        context.success(response)

    def update_session_permission(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(UpdateSessionPermissionRequest)
        # a request may send any one of the three lists
        request.create = request.create or []
        request.update = request.update or []
        request.delete = request.delete or []
        message, is_valid = self._validate_update_session_permission_request(
            request, context
        )
        if not is_valid:
            context.fail(
                error_code=errorcodes.INVALID_PARAMS,
                payload=UpdateSessionPermissionResponse(
                    permissions=[] + request.create + request.update + request.delete
                ),
                message=message,
            )
        else:
            is_valid, request = self.validate_update_session_permission_request(request)

        if not is_valid:
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

    def invoke(self, context: ApiInvocationContext):
        if not context.is_authorized_user():
            raise exceptions.unauthorized_access()

        namespace = context.namespace
        if namespace in self.namespace_handler_map:
            self.namespace_handler_map[namespace](context)
