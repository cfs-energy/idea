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

from ideadatamodel import (
    ReIndexUserSessionsRequest,
    ReIndexUserSessionsResponse,
    VirtualDesktopSession,
    VirtualDesktopSoftwareStack,
    VirtualDesktopBaseOS,
    VirtualDesktopServer,
    SocaMemory,
    SocaMemoryUnit,
    VirtualDesktopSessionType,
    BatchCreateSessionRequest,
    BatchCreateSessionResponse, Project
)
from ideadatamodel import constants
from ideasdk.utils import Utils

from ideavirtualdesktopcontroller.cli import build_cli_context

import click
import csv
from typing import List
from rich.table import Table


@click.command(context_settings=constants.CLICK_SETTINGS, short_help='Re Index all user-sessions to Open Search')
@click.argument('tokens', nargs=-1)
def reindex_user_sessions(tokens, **kwargs):
    context = build_cli_context(unix_socket_timeout=360000)

    request = ReIndexUserSessionsRequest()
    response = context.unix_socket_client.invoke_alt(
        namespace='VirtualDesktopAdmin.ReIndexUserSessions',
        payload=request,
        result_as=ReIndexUserSessionsResponse
    )
    # TODO: PrettyPrint response.
    # TODO: handle flag --destroy-and-recreate-index
    print(response)


@click.command(context_settings=constants.CLICK_SETTINGS, short_help='Create Multiple User sessions')
@click.option('--path-to-csv', help='path to the csv file')
@click.option('--force', is_flag=True, help='skips confirmation prompts')
@click.option('--generate-template', is_flag=True, help='generates a csv template compatible with this command')
def batch_create_sessions(path_to_csv: str, force: bool, generate_template: bool):
    """
    @param path_to_csv:
    @param force:
    @param generate_template:
    @return:

    Creates user sessions
    """

    if generate_template:
        #  TODO: generate template
        return

    context = build_cli_context(unix_socket_timeout=360000)

    # TODO: check if valid path and throw error

    with open(path_to_csv, 'r', encoding='utf-8-sig') as csv_file:
        csv_reader = csv.DictReader(csv_file)
        fieldnames = csv_reader.fieldnames
        # TODO check if all required field names are present

        session_requests: List[List[VirtualDesktopSession]] = []
        sessions: List[VirtualDesktopSession] = []
        for row in csv_reader:
            sessions.append(VirtualDesktopSession(
                name=Utils.get_as_string(row['session_name']),
                owner=Utils.get_as_string(row['owner']),
                software_stack=VirtualDesktopSoftwareStack(
                    base_os=VirtualDesktopBaseOS(Utils.get_as_string(row['base_os'])),
                    stack_id=Utils.get_as_string(str(row['software_stack_id']))
                ),
                server=VirtualDesktopServer(
                    instance_type=Utils.get_as_string(row['instance_type']),
                    root_volume_size=SocaMemory(
                        unit=SocaMemoryUnit.GB,
                        value=Utils.get_as_float(row['storage_size_gb'])
                    )
                ),
                project=Project(
                    project_id=Utils.get_as_string(row['project_id'])
                ),
                type=VirtualDesktopSessionType(Utils.get_as_string(row['session_type'])),
                hibernation_enabled=Utils.get_as_bool(row['hibernation_enabled']),
            ))
            # random batching with 10 sessions to not overload the API.
            if len(sessions) > 10:
                session_requests.append(sessions)
                sessions = []

        session_requests.append(sessions)
        success_list: List[VirtualDesktopSession] = []
        fail_list: List[VirtualDesktopSession] = []
        for sessions in session_requests:
            response = context.unix_socket_client.invoke_alt(
                namespace='VirtualDesktopAdmin.BatchCreateSessions',
                payload=BatchCreateSessionRequest(
                    sessions=sessions
                ),
                result_as=BatchCreateSessionResponse
            )
            success_list.extend(response.success)
            fail_list.extend(response.failed)

        table = Table()
        table.add_column("Session Name", justify="left", no_wrap=False)
        table.add_column("IDEA Session ID", justify="left", no_wrap=False)
        table.add_column("Status", justify="left", no_wrap=False)
        table.add_column("Failure Reason", justify="left", no_wrap=False)

        if Utils.is_not_empty(success_list):
            for session in success_list:
                table.add_row(session.name, session.idea_session_id, session.state, 'N/A', style='green')

        if Utils.is_not_empty(fail_list):
            for session in fail_list:
                table.add_row(session.name, 'N/A', 'FAILED', session.failure_reason, style='red')

        context.print(table)
