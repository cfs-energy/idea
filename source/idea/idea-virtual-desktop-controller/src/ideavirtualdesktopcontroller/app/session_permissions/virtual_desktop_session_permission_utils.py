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
import logging
from typing import List, Optional, Union

import ideavirtualdesktopcontroller
from ideadatamodel import errorcodes, exceptions
from ideadatamodel import (
    GetGroupRequest,
    ListUsersInGroupRequest,
    SocaPaginator,
    VirtualDesktopSessionPermissionActorType,
)
from ideadatamodel import (
    VirtualDesktopSessionPermission,
    VirtualDesktopSession,
    UpdateSessionPermissionRequest,
    UpdateSessionPermissionResponse,
)
from ideasdk.analytics.analytics_service import (
    AnalyticsEntry,
    EntryAction,
    EntryContent,
)
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.events.events_utils import EventsUtils
from ideavirtualdesktopcontroller.app.permission_profiles.virtual_desktop_permission_profile_db import (
    VirtualDesktopPermissionProfileDB,
)
from ideavirtualdesktopcontroller.app.session_permissions.constants import (
    GROUP_ACTOR_PREFIX,
    MAX_GROUP_SHARE_MEMBERS,
    bare_actor_name,
    ACTOR_NAME_PATTERN,
)
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_db import (
    VirtualDesktopSessionPermissionDB,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
)


GROUP_UNREADABLE_MESSAGE = (
    'Your group membership could not be checked right now, so the shared desktop cannot '
    'be opened. Try again in a minute.'
)


def group_state(context, group_name: str) -> str:
    """
    'enabled', 'disabled', 'missing' (the accounts service says there is no such group), or
    'unreadable' (any other failure: transport, authorization, a server fault), which is
    logged and never taken for a missing group
    """
    try:
        group = context.accounts_client.get_group(
            GetGroupRequest(group_name=group_name)
        ).group
    except exceptions.SocaException as e:
        if e.error_code == errorcodes.AUTH_GROUP_NOT_FOUND:
            return 'missing'
        logging.getLogger(__name__).error(
            f'group {group_name} could not be read from the accounts service: {e}'
        )
        return 'unreadable'
    except Exception as e:
        logging.getLogger(__name__).error(
            f'group {group_name} could not be read from the accounts service: {e}'
        )
        return 'unreadable'
    if group is None:
        return 'missing'
    return 'disabled' if group.enabled is False else 'enabled'


def group_enabled(context, group_name: str) -> bool:
    """a group the accounts service does not confirm as enabled admits nobody"""
    return group_state(context, group_name) == 'enabled'


def group_members(context, group_name: str, limit: int) -> Optional[List[str]]:
    """
    up to limit user names in the group, read from the accounts service now, or None when
    it cannot be read
    """
    members: List[str] = []
    cursor = None
    try:
        while len(members) < limit:
            result = context.accounts_client.list_users_in_group(
                ListUsersInGroupRequest(
                    group_names=[group_name],
                    paginator=SocaPaginator(page_size=limit, cursor=cursor),
                )
            )
            members.extend(
                user.username for user in (result.listing or []) if user.username
            )
            cursor = result.paginator.cursor if result.paginator else None
            if not cursor:
                break
    except Exception:
        return None
    return members[:limit]


class VirtualDesktopSessionPermissionUtils:
    # the file is written from base64 on the host, so these are the real line endings
    WINDOWS_POWERSHELL_NEW_LINE = '\r\n'
    LINUX_CMD_NEW_LINE = '\n'

    def __init__(
        self,
        context: ideavirtualdesktopcontroller.AppContext,
        db: VirtualDesktopSessionPermissionDB,
        permission_profile_db: VirtualDesktopPermissionProfileDB,
    ):
        self.context = context
        self._logger = self.context.logger('virtual-desktop-session-permission-utils')
        self._session_permission_db = db
        self._permission_profile_db = permission_profile_db
        self.events_utils: EventsUtils = EventsUtils(context=self.context)
        self.controller_utils: VirtualDesktopControllerUtils = (
            VirtualDesktopControllerUtils(self.context)
        )

    @staticmethod
    def _generate_entry_id(session_permission: VirtualDesktopSessionPermission) -> str:
        return f'permission-{session_permission.idea_session_id}-{session_permission.actor_name}'

    def index_session_permission_to_opensearch(
        self, session_permission: VirtualDesktopSessionPermission
    ):
        index_name = f'{self.context.config().get_string("virtual-desktop-controller.opensearch.session_permission.alias", required=True)}-{self.context.session_permission_template_version}'
        self.context.analytics_service().post_entry(
            AnalyticsEntry(
                entry_id=self._generate_entry_id(session_permission),
                entry_action=EntryAction.CREATE_ENTRY,
                entry_content=EntryContent(
                    index_id=index_name,
                    entry_record=self._session_permission_db.convert_session_permission_object_to_db_dict(
                        session_permission
                    ),
                ),
            )
        )

    def delete_session_entry_from_opensearch(
        self, session_permission: VirtualDesktopSessionPermission
    ):
        index_name = f'{self.context.config().get_string("virtual-desktop-controller.opensearch.session_permission.alias", required=True)}-{self.context.session_permission_template_version}'
        self.context.analytics_service().post_entry(
            AnalyticsEntry(
                entry_id=self._generate_entry_id(session_permission),
                entry_action=EntryAction.DELETE_ENTRY,
                entry_content=EntryContent(index_id=index_name),
            )
        )

    def update_session_entry_to_opensearch(
        self, session_permission: VirtualDesktopSessionPermission
    ):
        index_name = f'{self.context.config().get_string("virtual-desktop-controller.opensearch.session_permission.alias", required=True)}-{self.context.session_permission_template_version}'
        self.context.analytics_service().post_entry(
            AnalyticsEntry(
                entry_id=self._generate_entry_id(session_permission),
                entry_action=EntryAction.UPDATE_ENTRY,
                entry_content=EntryContent(
                    index_id=index_name,
                    entry_record=self._session_permission_db.convert_session_permission_object_to_db_dict(
                        session_permission
                    ),
                ),
            )
        )

    def _retrieve_permission_profile_values(
        self, profile_id
    ) -> tuple[List[str], List[str]]:
        allow_permissions: List[str] = []
        deny_permissions: List[str] = []

        permission_profile = self._permission_profile_db.get(profile_id=profile_id)

        builtin_permission = permission_profile.get_permission('builtin')
        if Utils.is_not_empty(builtin_permission) and builtin_permission.enabled:
            allow_permissions.append('builtin')
        else:
            for permission in self._permission_profile_db.permission_types:
                permission_entry = permission_profile.get_permission(permission.key)
                self._logger.debug(f'Found permission entry: {permission_entry}')

                if permission_entry.enabled:
                    self._logger.debug(
                        f'Applying allow permission for {permission_entry.key}'
                    )
                    allow_permissions.append(permission.key.replace('_', '-'))
                else:
                    if permission_entry.key.lower() in {'builtin'}:
                        self._logger.debug("Skipping deny permission for 'builtin'")
                        continue
                    else:
                        self._logger.debug(
                            f'Applying deny permission for {permission_entry.key}'
                        )
                        deny_permissions.append(permission.key.replace('_', '-'))

        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                f'Final permissions returning: Allow: {allow_permissions}   Deny: {deny_permissions}'
            )

        return allow_permissions, deny_permissions

    def _share_actors(self, session_permission) -> List[str]:
        """the user names a share admits: its user, or its group's current members"""
        if (
            session_permission.actor_type
            != VirtualDesktopSessionPermissionActorType.GROUP
        ):
            return [session_permission.actor_name]
        group = bare_actor_name(session_permission.actor_name)
        # a disabled group grants nothing, as for project access
        if not group_enabled(self.context, group):
            return []
        members = group_members(self.context, group, MAX_GROUP_SHARE_MEMBERS)
        if members is None:
            self._logger.error(
                f'the members of group {group} could not be read; its share on '
                f'{session_permission.idea_session_id} admits nobody until the next refresh'
            )
            return []
        return [m for m in members if ACTOR_NAME_PATTERN.fullmatch(m)]

    def generate_permissions_for_session(
        self, session: VirtualDesktopSession, for_broker: bool
    ) -> Union[str, None]:
        # Simplified Windows detection to match all Windows variations
        is_windows = 'windows' in str(session.software_stack.base_os).lower()

        admin_username = (
            self.context.config().get_string(
                'cluster.administrator_username', required=True
            )
            if not is_windows
            else 'Administrator'
        )

        if for_broker or not is_windows:
            new_line_char = self.LINUX_CMD_NEW_LINE
        else:
            new_line_char = self.WINDOWS_POWERSHELL_NEW_LINE

        self._logger.debug(
            f'Generating Permissions for session - Admin: {admin_username}  BaseOS: {session.software_stack.base_os}'
        )
        permission = f'[groups]{new_line_char}'
        if is_windows:
            permission += f'group:ideaadmin=user:{admin_username}{new_line_char}'
        else:
            permission += f'group:ideaadmin=user:{admin_username}, osgroup:{self.controller_utils.get_virtual_desktop_admin_group()}{new_line_char}'
        permission += f'[aliases]{new_line_char}'

        permission_profiles = set()
        profile_cache = {}

        admin_profile = self.context.config().get_string(
            'virtual-desktop-controller.dcv_session.default_profiles.admin',
            required=True,
        )
        owner_profile = self.context.config().get_string(
            'virtual-desktop-controller.dcv_session.default_profiles.owner',
            required=True,
        )
        permission_profiles.add(admin_profile)
        permission_profiles.add(owner_profile)

        session_permissions = []
        for session_permission in self._session_permission_db.get_for_session(
            session.idea_session_id
        ):
            # a group row from before group shares were keyed names nobody: it is skipped
            is_group = (
                session_permission.actor_type
                == VirtualDesktopSessionPermissionActorType.GROUP
            )
            if is_group != (session_permission.actor_name or '').startswith(
                GROUP_ACTOR_PREFIX
            ):
                continue
            # an entry stored before share names were checked would break the file's lines
            if not ACTOR_NAME_PATTERN.fullmatch(
                bare_actor_name(session_permission.actor_name) or ''
            ):
                self._logger.warning(
                    f'skipping a share entry on {session.idea_session_id} whose name is not a plain user or group name'
                )
                continue
            if VirtualDesktopSessionPermissionDB.is_expired(session_permission):
                continue
            session_permissions.append(session_permission)
        for session_permission in session_permissions:
            permission_profiles.add(session_permission.permission_profile.profile_id)

        for profile in permission_profiles:
            self._logger.debug(f'Processing permissions profile {profile}')
            allow_permissions, deny_permissions = (
                self._retrieve_permission_profile_values(profile)
            )
            self._logger.debug(
                f'Retrieved profile permissions: Allow: {allow_permissions}   Deny: {deny_permissions}'
            )
            if Utils.is_not_empty(allow_permissions):
                permission += (
                    f'{profile}-allow={", ".join(allow_permissions)}{new_line_char}'
                )
            if Utils.is_not_empty(deny_permissions):
                permission += (
                    f'{profile}-deny={", ".join(deny_permissions)}{new_line_char}'
                )

            profile_cache[profile] = {
                'allow': allow_permissions,
                'deny': deny_permissions,
            }

        permission += f'[permissions]{new_line_char}'

        # Apply guest permissions. a group share is written as one line per member, read now:
        # a directory group name is not reliably resolved by the host's display server
        for session_permission in session_permissions:
            profile_id = session_permission.permission_profile.profile_id
            for actor in self._share_actors(session_permission):
                # the owner's own lines come from the owner profile below: a share, by group
                # or naming the owner, never adds a line that could restrict them
                if actor == session.owner:
                    continue
                if Utils.is_not_empty(profile_cache[profile_id]['allow']):
                    permission += f'{actor} allow {profile_id}-allow{new_line_char}'
                if Utils.is_not_empty(profile_cache[profile_id]['deny']):
                    permission += f'{actor} deny {profile_id}-deny{new_line_char}'

        # Apply admin permission
        if Utils.is_not_empty(profile_cache[admin_profile]['allow']):
            permission += f'group:ideaadmin allow {admin_profile}-allow{new_line_char}'
        if Utils.is_not_empty(profile_cache[admin_profile]['deny']):
            permission += f'group:ideaadmin deny {admin_profile}-deny{new_line_char}'

        # Apply owner permission
        if Utils.is_not_empty(profile_cache[owner_profile]['allow']):
            permission += f'%owner% allow {owner_profile}-allow{new_line_char}'
        if Utils.is_not_empty(profile_cache[owner_profile]['deny']):
            permission += f'%owner% deny {owner_profile}-deny{new_line_char}'

        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(f'Returning permissions content {permission}')
        return permission

    def update_permission_for_sessions(
        self, request: UpdateSessionPermissionRequest
    ) -> UpdateSessionPermissionResponse:
        response = []
        sessions_info = set()
        for session_permission in request.create:
            response.append(self._session_permission_db.create(session_permission))
            sessions_info.add(
                (
                    session_permission.idea_session_id,
                    session_permission.idea_session_owner,
                )
            )
        for session_permission in request.update:
            response.append(self._session_permission_db.update(session_permission))
            sessions_info.add(
                (
                    session_permission.idea_session_id,
                    session_permission.idea_session_owner,
                )
            )
        for session_permission in request.delete:
            self._session_permission_db.delete(session_permission)
            sessions_info.add(
                (
                    session_permission.idea_session_id,
                    session_permission.idea_session_owner,
                )
            )

        for session_info in sessions_info:
            self.events_utils.publish_enforce_session_permissions_event(
                idea_session_id=session_info[0],
                idea_session_owner=session_info[1],
            )

        return UpdateSessionPermissionResponse(permissions=response)

    def delete_permissions_for_session(self, session: VirtualDesktopSession):
        session_permissions = self._session_permission_db.get_for_session(
            session.idea_session_id
        )
        if Utils.is_empty(session_permissions):
            return
        for session_permission in session_permissions:
            self._session_permission_db.delete(session_permission)
