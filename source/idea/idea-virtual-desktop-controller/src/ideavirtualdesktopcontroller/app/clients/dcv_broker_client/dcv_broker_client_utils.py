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
import re
import shlex
from threading import RLock
from typing import List, Set

import ideavirtualdesktopcontroller
from ideadatamodel.virtual_desktop import (
    VirtualDesktopSession,
    VirtualDesktopBaseOS,
    VirtualDesktopSessionState,
    VirtualDesktopServer,
    VirtualDesktopSessionType,
)
from ideadatamodel import exceptions
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.session_permissions.virtual_desktop_session_permission_utils import (
    VirtualDesktopSessionPermissionUtils,
)


BROKER_UNAVAILABLE_MESSAGE = 'The remote display service could not be reached to check this desktop for open connections. Try again in a few minutes.'

# the broker issues a UUID for a virtual session and `console` for a console session
DCV_SESSION_ID_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,128}')


class DCVBrokerClientUtils:
    def __init__(
        self,
        context: ideavirtualdesktopcontroller.AppContext,
        session_permission_utils: VirtualDesktopSessionPermissionUtils,
    ):
        self.context = context
        self._logger = context.logger('dcv-broker-client-utils')
        self._session_permission_utils = session_permission_utils
        self.BROKER_TABLE_NAMES = 'dcv-broker.table-names-list'
        self.broker_table_names_lock = RLock()

    def get_broker_dynamodb_table_names(self) -> List[str]:
        broker_table_names = (
            self.context.cache().long_term().get(self.BROKER_TABLE_NAMES)
        )
        if Utils.is_empty(broker_table_names):
            broker_table_names = []
            with self.broker_table_names_lock:
                last_evaluated_table_name = None
                while True:
                    if Utils.is_empty(last_evaluated_table_name):
                        response = self.context.aws().dynamodb().list_tables()
                    else:
                        response = (
                            self.context.aws()
                            .dynamodb()
                            .list_tables(
                                ExclusiveStartTableName=last_evaluated_table_name
                            )
                        )
                    table_names = Utils.get_value_as_list('TableNames', response, [])
                    for table_name in table_names:
                        if (
                            f'{self.context.cluster_name()}.{self.context.module_id()}.dcv-broker.'
                            in table_name
                        ):
                            broker_table_names.append(table_name)

                    last_evaluated_table_name = Utils.get_value_as_string(
                        'LastEvaluatedTableName', response, None
                    )
                    if Utils.is_empty(last_evaluated_table_name):
                        break

                self.context.cache().long_term().set(
                    self.BROKER_TABLE_NAMES, broker_table_names
                )
        return broker_table_names

    @staticmethod
    def get_server_object_from_success_result(server_json) -> VirtualDesktopServer:
        server = VirtualDesktopServer(
            server_id=Utils.get_value_as_string('id', server_json, None),
            private_ip=Utils.get_value_as_string('ip', server_json, None),
            private_dns_name=Utils.get_value_as_string('hostname', server_json, None),
            public_ip=None,
            public_dns_name=None,
            availability=Utils.get_value_as_string('availability', server_json, None),
            unavailability_reason=Utils.get_value_as_string(
                'unavailabilityreason', server_json, None
            ),
            console_session_count=Utils.get_value_as_int(
                'consolesessioncount', server_json, None
            ),
            virtual_session_count=Utils.get_value_as_int(
                'virtualsessioncount', server_json, None
            ),
        )

        tags = Utils.get_value_as_list('tags', server_json, [])
        for tag in tags:
            if Utils.get_value_as_string('key', tag, None) == 'dcv:os-family':
                server.os_family = Utils.get_value_as_string('value', tag, None)
            elif (
                Utils.get_value_as_string('key', tag, None)
                == 'dcv:max-concurrent-sessions-per-user'
            ):
                server.max_concurrent_sessions_per_user = Utils.get_value_as_string(
                    'value', tag, None
                )
            elif (
                Utils.get_value_as_string('key', tag, None)
                == 'dcv:max-virtual-sessions'
            ):
                server.max_virtual_sessions = Utils.get_value_as_string(
                    'value', tag, None
                )

        return server

    @staticmethod
    def get_session_object_from_success_result(session_json) -> VirtualDesktopSession:
        return VirtualDesktopSession(
            dcv_session_id=Utils.get_value_as_string('id', session_json, None),
            owner=Utils.get_value_as_string('owner', session_json, None),
            type=VirtualDesktopSessionType[
                Utils.get_value_as_string('type', session_json, 'VIRTUAL')
            ],
            name=Utils.get_value_as_string('name', session_json, None),
            state=VirtualDesktopSessionState[
                Utils.get_value_as_string('state', session_json, 'READY')
            ],
            server=DCVBrokerClientUtils.get_server_object_from_success_result(
                Utils.get_value_as_dict('server', session_json, {})
            ),
        )

    @staticmethod
    def get_session_object_from_error_result(session_json) -> VirtualDesktopSession:
        session_request_data = Utils.get_value_as_dict(
            'create_session_request_data', session_json, {}
        )

        return VirtualDesktopSession(
            dcv_session_id=Utils.get_value_as_string('id', session_request_data, None),
            owner=Utils.get_value_as_string('owner', session_request_data, None),
            type=VirtualDesktopSessionType[
                Utils.get_value_as_string('type', session_request_data, 'VIRTUAL')
            ],
            name=Utils.get_value_as_string('name', session_request_data, None),
            failure_reason=Utils.get_value_as_string(
                'failure_reason', session_json, None
            ),
        )

    @staticmethod
    def get_storage_root_for_base_os(base_os: VirtualDesktopBaseOS, owner) -> str:
        # Simplified Windows detection to match all Windows variations
        is_windows = 'windows' in str(base_os).lower()

        if not is_windows:
            return f'/data/home/{owner}/storage-root'

        return f'C:\\session-storage\\{owner}'

    @staticmethod
    def _powershell_literal(value) -> str:
        """a single-quoted PowerShell string: nothing inside it is expanded"""
        # PowerShell also ends a single-quoted string at the typographic single quotes
        return "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r'\1\1', str(value)) + "'"

    @staticmethod
    def runs_on_desktop_host(entry: dict, host_addresses: Set[str]) -> bool:
        """
        A broker session entry is on this desktop only when the address its server reports is
        one of the desktop instance's own private addresses: the placement tag is reported by
        the agent itself and does not identify the host.
        """
        return (entry.get('server') or {}).get('ip') in host_addresses

    def desktop_host_addresses(self, session: VirtualDesktopSession) -> Set[str]:
        """
        Every private IPv4 address on every network interface of the desktop's instance, read
        from EC2: the stored session row does not carry the address. An EC2 error is raised so
        the caller retries; an instance with no address matches no broker session.
        """
        instance_id = session.server.instance_id if session.server else None
        addresses = set()
        if Utils.is_not_empty(instance_id):
            response = (
                self.context.aws().ec2().describe_instances(InstanceIds=[instance_id])
            )
            for reservation in response.get('Reservations', []):
                for instance in reservation.get('Instances', []):
                    for interface in instance.get('NetworkInterfaces', []):
                        for address in interface.get('PrivateIpAddresses', []):
                            addresses.add(address.get('PrivateIpAddress'))
        addresses.discard(None)
        if not addresses:
            self._logger.error(
                f'no private address for instance {instance_id} of {session.idea_session_id}; '
                f'no broker session is accepted as running on it'
            )
        return addresses

    def get_commands_to_execute_for_resuming_session(
        self, session: VirtualDesktopSession
    ) -> List[str]:
        """
        The commands run as an administrator on the host to recreate the DCV session. Every
        value is passed as a quoted literal and the permissions file is written from base64, so
        nothing taken from the session is read by the shell.
        """
        # the id names the permissions folder written as root/SYSTEM
        if not DCV_SESSION_ID_PATTERN.fullmatch(session.dcv_session_id or ''):
            self._logger.error(
                f'not resuming {session.idea_session_id}: the stored DCV session id is not one the broker issues'
            )
            return []
        storage_root = DCVBrokerClientUtils.get_storage_root_for_base_os(
            session.software_stack.base_os, session.owner
        )
        # the DCV session label only; a stored name from before names were checked is
        # shown with its other characters replaced instead of being refused
        dcv_name = re.sub(r'[^A-Za-z0-9 ._-]', '_', session.name or '')[:64]
        session_type = session.type.lower()
        command_list = []
        permissions_content = (
            self._session_permission_utils.generate_permissions_for_session(
                session, for_broker=False
            )
        )
        permissions_base64 = (
            Utils.base64_encode(permissions_content)
            if Utils.is_not_empty(permissions_content)
            else None
        )

        # Simplified Windows detection to match all Windows variations
        is_windows = 'windows' in str(session.software_stack.base_os).lower()

        if is_windows:
            ps = DCVBrokerClientUtils._powershell_literal
            dcv_command = (
                f"& 'C:\\Program Files\\NICE\\DCV\\Server\\bin\\dcv.exe' create-session "
                f'--type {ps(session_type)} --name {ps(dcv_name)} --user {ps(session.owner)} '
                f'--owner {ps(session.owner)} --storage-root {ps(storage_root)} '
            )
            if permissions_base64 is not None:
                permissions_folder = (
                    f'C:\\Program Files\\NICE\\DCV\\{session.dcv_session_id}\\'
                )
                permissions_file_path = f'{permissions_folder}idea.perm'
                command_list.append('Start-Sleep -Seconds 10')
                command_list.append(
                    f'Remove-Item -LiteralPath {ps(permissions_folder)} -Recurse -Force'
                )
                command_list.append(
                    f'New-Item -Path {ps(permissions_folder)} -ItemType Directory'
                )
                command_list.append(
                    f'[System.IO.File]::WriteAllText({ps(permissions_file_path)}, '
                    f'[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String({ps(permissions_base64)})))'
                )
                dcv_command += f'--permissions-file {ps(permissions_file_path)} '
            dcv_command += ps(session.dcv_session_id)
        else:
            q = shlex.quote
            dcv_command = (
                f'dcv create-session --type {q(session_type)} --name {q(dcv_name)} '
                f'--user {q(session.owner)} --owner {q(session.owner)} --storage-root {q(storage_root)} '
            )

            if permissions_base64 is not None:
                permissions_folder = f'/etc/dcv/{session.dcv_session_id}/'
                permissions_file_path = f'{permissions_folder}idea.perm'
                command_list.append(f'mkdir -p {q(permissions_folder)}')
                command_list.append(
                    f'echo {q(permissions_base64)} | base64 -d > {q(permissions_file_path)}'
                )
                dcv_command += f'--permissions-file {q(permissions_file_path)} '
            dcv_command += q(session.dcv_session_id)

        command_list.append(dcv_command)
        if is_windows:
            # the command reports dcv.exe's result, not the last cmdlet's
            command_list.append('exit $LASTEXITCODE')
        return command_list

    def get_active_counts_for_sessions(
        self, sessions: List[VirtualDesktopSession]
    ) -> List[VirtualDesktopSession]:
        if Utils.is_empty(sessions):
            return []

        dcv_session_id_map = {}
        for session in sessions:
            session.connection_count = 0
            # no DCV session id means the broker never had a session, so nothing is connected
            if Utils.is_not_empty(session.dcv_session_id):
                dcv_session_id_map[session.dcv_session_id] = session
        if not dcv_session_id_map:
            return sessions

        try:
            response = self.context.dcv_broker_client.describe_sessions(
                list(dcv_session_id_map.values())
            )
        except Exception as e:
            self._logger.exception(f'could not read connection counts: {e}')
            raise exceptions.general_exception(BROKER_UNAVAILABLE_MESSAGE)

        session_response = Utils.get_value_as_dict('sessions', response, {})
        for dcv_session_id in session_response.keys():
            dcv_session_id_map[
                dcv_session_id
            ].connection_count = Utils.get_value_as_int(
                'num_of_connections', session_response.get(dcv_session_id), 0
            )
        return sessions
