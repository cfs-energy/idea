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
    BatchCreateSessionResponse,
    DeleteSessionRequest,
    DeleteSessionResponse,
    Project,
)
from ideadatamodel import constants
from ideasdk.utils import Utils

from ideavirtualdesktopcontroller.cli import build_cli_context

import click
import csv
from typing import List
from rich.table import Table
from rich.console import Console


@click.command(
    context_settings=constants.CLICK_SETTINGS,
    short_help='Re Index all user-sessions to Open Search',
)
@click.option(
    '--reset', is_flag=True, help='Clear the OpenSearch index before reindexing'
)
@click.argument('tokens', nargs=-1)
def reindex_user_sessions(reset, tokens, **kwargs):
    context = build_cli_context(unix_socket_timeout=360000)

    if reset:
        # Clear the OpenSearch index before reindexing
        index_name = clear_user_sessions_index(context)
        click.echo(f'OpenSearch index {index_name} has been cleared.')

    request = ReIndexUserSessionsRequest()
    response = context.unix_socket_client.invoke_alt(
        namespace='VirtualDesktopAdmin.ReIndexUserSessions',
        payload=request,
        result_as=ReIndexUserSessionsResponse,
    )
    # TODO: PrettyPrint response.
    print(response)


def clear_user_sessions_index(context):
    """
    Clear the OpenSearch index for user sessions without deleting the index.
    """
    from ideasdk.aws.opensearch.aws_opensearch_client import AwsOpenSearchClient

    # Get the index name from the same pattern used in the application
    sessions_alias = context.config().get_string(
        'virtual-desktop-controller.opensearch.dcv_session.alias', required=True
    )

    # Create OpenSearch client
    os_client = AwsOpenSearchClient(context)

    # Get the actual index name that maps to the alias
    try:
        aliases_response = os_client.os_client.indices.get_alias(name=sessions_alias)

        # If we have the alias, clear all associated indices
        if aliases_response:
            for index_name in aliases_response:
                # Delete by query to remove all documents but keep the index
                os_client.os_client.delete_by_query(
                    index=index_name, body={'query': {'match_all': {}}}, refresh=True
                )
            return sessions_alias
    except Exception as e:
        # If the alias doesn't exist or other error, try with the versioned index pattern
        context.logger('reindex-user-sessions').warning(
            f'Error clearing index with alias {sessions_alias}: {str(e)}'
        )

    # Try with the specific versioned index pattern as fallback
    try:
        # This pattern follows the same one used in virtual_desktop_session_utils.py
        # We're looking for all indices that match the pattern
        indices = os_client.os_client.indices.get(f'{sessions_alias}-*')

        for index_name in indices:
            os_client.os_client.delete_by_query(
                index=index_name, body={'query': {'match_all': {}}}, refresh=True
            )
        return f'{sessions_alias}-*'
    except Exception as e:
        context.logger('reindex-user-sessions').error(
            f'Failed to clear user session indices: {str(e)}'
        )
        return None


@click.command(
    context_settings=constants.CLICK_SETTINGS,
    short_help='Create Multiple User sessions',
)
@click.option('--path-to-csv', help='path to the csv file')
@click.option('--force', is_flag=True, help='skips confirmation prompts')
@click.option(
    '--generate-template',
    is_flag=True,
    help='generates a csv template compatible with this command',
)
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
        raise NotImplementedError

    context = build_cli_context(unix_socket_timeout=360000)

    # TODO: check if valid path and throw error
    if not path_to_csv:
        raise ValueError('path_to_csv is required')

    with open(path_to_csv, 'r', encoding='utf-8-sig') as csv_file:
        csv_reader = csv.DictReader(csv_file)
        fieldnames = csv_reader.fieldnames
        # TODO check if all required field names are present

        # Basic validation of required field names
        required_fields = ['session_name', 'owner']
        if fieldnames:
            missing_fields = [
                field for field in required_fields if field not in fieldnames
            ]
            if missing_fields:
                raise ValueError(f'Missing required CSV columns: {missing_fields}')
        else:
            raise ValueError('CSV file appears to be empty or malformed')

        session_requests: List[List[VirtualDesktopSession]] = []
        sessions: List[VirtualDesktopSession] = []

        for row in csv_reader:
            _name = Utils.get_as_string(row.get('session_name', None))
            _owner = Utils.get_as_string(row.get('owner', None))
            _base_os = Utils.get_as_string(
                row.get('base_os', None), default='amazonlinux2023'
            )
            _software_stack_id = Utils.get_as_string(
                row.get('software_stack_id', None),
                default='ss-base-amazonlinux2023-x86-64-base',
            )
            _instance_type = Utils.get_as_string(
                row.get('instance_type', None), default='t3.large'
            )
            _storage_size_gb = Utils.get_as_float(
                row.get('storage_size_gb', None), default=30.0
            )
            _project_id = Utils.get_as_string(
                row.get('project_id', None), default='default'
            )
            _session_type = Utils.get_as_string(
                row.get('session_type', None), default='console'
            )
            _hibernation_enabled = Utils.get_as_bool(
                row.get('hibernation_enabled', None), default=False
            )

            if Utils.is_any_empty(
                _name,
                _owner,
                _base_os,
                _software_stack_id,
                _instance_type,
                _storage_size_gb,
                _project_id,
                _session_type,
            ):
                # Skip this entry
                print(f'Skipping entry: {row}')
                continue

            sessions.append(
                VirtualDesktopSession(
                    name=_name,
                    owner=_owner,
                    software_stack=VirtualDesktopSoftwareStack(
                        base_os=VirtualDesktopBaseOS(_base_os),
                        stack_id=_software_stack_id,
                    ),
                    server=VirtualDesktopServer(
                        instance_type=_instance_type,
                        root_volume_size=SocaMemory(
                            unit=SocaMemoryUnit.GB, value=_storage_size_gb
                        ),
                    ),
                    project=Project(project_id=_project_id),
                    type=VirtualDesktopSessionType(_session_type),
                    hibernation_enabled=_hibernation_enabled,
                )
            )
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
                payload=BatchCreateSessionRequest(sessions=sessions),
                result_as=BatchCreateSessionResponse,
            )
            success_list.extend(response.success)
            fail_list.extend(response.failed)

        table = Table()
        table.add_column('Session Name', justify='left', no_wrap=False)
        table.add_column('IDEA Session ID', justify='left', no_wrap=False)
        table.add_column('Status', justify='left', no_wrap=False)
        table.add_column('Failure Reason', justify='left', no_wrap=False)

        if Utils.is_not_empty(success_list):
            for session in success_list:
                table.add_row(
                    session.name,
                    session.idea_session_id,
                    session.state,
                    'N/A',
                    style='green',
                )

        if Utils.is_not_empty(fail_list):
            for session in fail_list:
                table.add_row(
                    session.name, 'N/A', 'FAILED', session.failure_reason, style='red'
                )

        context.print(table)


@click.command(
    context_settings=constants.CLICK_SETTINGS,
    short_help='Clean up orphaned schedule records in DynamoDB',
)
@click.option(
    '--dry-run',
    is_flag=True,
    help='Show what would be deleted without actually deleting',
)
@click.option('--force', is_flag=True, help='Skip confirmation prompts')
def cleanup_orphaned_schedules(dry_run: bool, force: bool):
    """
    Clean up schedule records that don't have corresponding sessions in the session database.

    \b
    This tool identifies and removes orphaned schedule entries that may remain after
    improper session deletion by directly querying DynamoDB.

    \b
    The tool works by:
    1. Scanning all schedule records in the schedules DynamoDB table for each day of the week
    2. For each schedule, checking if the corresponding session exists in the sessions table
    3. Identifying schedules that reference non-existent sessions (orphaned schedules)
    4. Optionally deleting these orphaned schedule records from DynamoDB

    \b
    Usage Examples:

    \b
    # Dry run to see what would be deleted (recommended first step)
    ideactl cleanup-orphaned-schedules --dry-run

    \b
    # Interactive cleanup with confirmation prompt
    ideactl cleanup-orphaned-schedules

    \b
    # Force cleanup without confirmation (use with caution)
    ideactl cleanup-orphaned-schedules --force

    \b
    # Combine dry-run with force (useful for automation/scripts)
    ideactl cleanup-orphaned-schedules --dry-run --force

    \b
    Args:
        dry_run: If True, only shows what would be deleted without making changes
        force: If True, skips confirmation prompts

    \b
    DynamoDB Tables Accessed:
        - {cluster_name}.{module_id}.controller.schedules (read/delete)
        - {cluster_name}.{module_id}.controller.user-sessions (read only)

    \b
    Requirements:
        - AWS credentials with DynamoDB access
        - IDEA cluster environment variables set
        - Access to both schedules and sessions DynamoDB tables
    """

    import boto3
    from botocore.exceptions import ClientError
    from ideadatamodel import DayOfWeek
    from ideasdk.utils import EnvironmentUtils

    # Get AWS region from environment
    aws_region = EnvironmentUtils.aws_default_region(required=True)

    # Get DynamoDB client
    dynamodb = boto3.resource('dynamodb', region_name=aws_region)

    # Build table names
    cluster_name = EnvironmentUtils.idea_cluster_name(required=True)
    module_id = EnvironmentUtils.idea_module_id(required=True)

    schedules_table_name = f'{cluster_name}.{module_id}.controller.schedules'
    sessions_table_name = f'{cluster_name}.{module_id}.controller.user-sessions'

    click.echo('🔍 Scanning DynamoDB tables:')
    click.echo(f'   Schedules: {schedules_table_name}')
    click.echo(f'   Sessions: {sessions_table_name}')

    try:
        schedules_table = dynamodb.Table(schedules_table_name)
        sessions_table = dynamodb.Table(sessions_table_name)
    except Exception as e:
        click.echo(f'❌ Error accessing DynamoDB tables: {str(e)}')
        return

    orphaned_schedules = []
    total_schedules = 0

    # Scan schedules for each day of the week
    for day in DayOfWeek:
        try:
            # Query schedules for this day
            response = schedules_table.query(
                KeyConditionExpression='day_of_week = :dow',
                ExpressionAttributeValues={':dow': day.value},
            )

            schedules_for_day = response.get('Items', [])
            day_count = len(schedules_for_day)
            total_schedules += day_count
            click.echo(
                f'🔍 Scanning schedules for {day.value}... found {day_count} schedules'
            )

            for schedule_item in schedules_for_day:
                idea_session_id = schedule_item.get('idea_session_id')
                idea_session_owner = schedule_item.get('idea_session_owner')
                schedule_id = schedule_item.get('schedule_id')

                if not idea_session_id or not idea_session_owner:
                    click.echo(
                        f'⚠️  Schedule {schedule_id} missing session info, skipping'
                    )
                    continue

                # Check if corresponding session exists
                try:
                    session_response = sessions_table.get_item(
                        Key={
                            'owner': idea_session_owner,
                            'idea_session_id': idea_session_id,
                        }
                    )

                    if 'Item' not in session_response:
                        # Session not found, this is an orphaned schedule
                        orphaned_schedules.append(
                            {
                                'day_of_week': day.value,
                                'schedule_id': schedule_id,
                                'idea_session_id': idea_session_id,
                                'idea_session_owner': idea_session_owner,
                                'schedule_type': schedule_item.get(
                                    'schedule_type', 'unknown'
                                ),
                                'raw_item': schedule_item,
                            }
                        )

                except ClientError as e:
                    if e.response['Error']['Code'] == 'ResourceNotFoundException':
                        # Session doesn't exist
                        orphaned_schedules.append(
                            {
                                'day_of_week': day.value,
                                'schedule_id': schedule_id,
                                'idea_session_id': idea_session_id,
                                'idea_session_owner': idea_session_owner,
                                'schedule_type': schedule_item.get(
                                    'schedule_type', 'unknown'
                                ),
                                'raw_item': schedule_item,
                            }
                        )
                    else:
                        click.echo(
                            f'❌ Error checking session {idea_session_id}: {str(e)}'
                        )

        except Exception as e:
            click.echo(f'❌ Error scanning schedules for {day.value}: {str(e)}')
            continue

    # Display results
    click.echo('\n📊 Scan Results:')
    click.echo(f'   Total schedules found: {total_schedules}')
    click.echo(f'   Orphaned schedules: {len(orphaned_schedules)}')

    if not orphaned_schedules:
        click.echo('✅ No orphaned schedules found!')
        return

    # Display orphaned schedules in a table
    table = Table()
    table.add_column('Day of Week', justify='left', no_wrap=False)
    table.add_column('Schedule ID', justify='left', no_wrap=False)
    table.add_column('Session ID', justify='left', no_wrap=False)
    table.add_column('Session Owner', justify='left', no_wrap=False)
    table.add_column('Schedule Type', justify='left', no_wrap=False)

    for schedule in orphaned_schedules:
        table.add_row(
            schedule['day_of_week'],
            schedule['schedule_id'],
            schedule['idea_session_id'] or 'N/A',
            schedule['idea_session_owner'] or 'N/A',
            schedule['schedule_type'],
            style='red',
        )

    click.echo('\n🗑️  Orphaned Schedules Found:')

    # Use Rich console to properly render the table
    console = Console()
    console.print(table)

    if dry_run:
        click.echo('\n🔍 Dry run mode - no changes made')
        return

    # Confirm deletion
    if not force:
        if not click.confirm(
            f'\n❗ Delete {len(orphaned_schedules)} orphaned schedule records?'
        ):
            click.echo('Cleanup cancelled')
            return

    # Delete orphaned schedules
    deleted_count = 0
    failed_deletes = []

    click.echo(f'\n🧹 Cleaning up {len(orphaned_schedules)} orphaned schedules...')

    for schedule in orphaned_schedules:
        try:
            schedules_table.delete_item(
                Key={
                    'day_of_week': schedule['day_of_week'],
                    'schedule_id': schedule['schedule_id'],
                }
            )
            deleted_count += 1
            click.echo(
                f'   ✅ Deleted schedule {schedule["schedule_id"]} ({schedule["day_of_week"]})'
            )
        except Exception as e:
            failed_deletes.append((schedule, str(e)))
            click.echo(
                f'   ❌ Failed to delete schedule {schedule["schedule_id"]}: {str(e)}'
            )

    # Summary
    click.echo('\n📋 Cleanup Summary:')
    click.echo(f'   Successfully deleted: {deleted_count}')
    click.echo(f'   Failed to delete: {len(failed_deletes)}')

    if failed_deletes:
        click.echo('\n❌ Failed Deletions:')
        for schedule, error in failed_deletes:
            click.echo(
                f'   {schedule["schedule_id"]} ({schedule["day_of_week"]}): {error}'
            )

    if deleted_count > 0:
        click.echo(
            f'\n✅ Successfully cleaned up {deleted_count} orphaned schedule records!'
        )


@click.command(
    context_settings=constants.CLICK_SETTINGS,
    short_help='Terminate virtual desktop sessions',
)
@click.option(
    '--session-id',
    help='Terminate a specific session by IDEA session ID',
)
@click.option(
    '--username',
    help='Terminate all sessions for a specific user',
)
@click.option(
    '--created-before',
    help='Terminate all sessions created before this date (YYYY-MM-DD format)',
)
@click.option(
    '--dry-run',
    is_flag=True,
    help='Show what sessions would be terminated without actually terminating them',
)
@click.option(
    '--force',
    is_flag=True,
    help='Skip confirmation prompts and force termination',
)
def terminate_sessions(
    session_id: str, username: str, created_before: str, dry_run: bool, force: bool
):
    """
    Terminate virtual desktop sessions by session ID, username, or creation date.

    \b
    This tool allows you to terminate sessions using different criteria:
    1. Terminate a specific session by providing its IDEA session ID
    2. Terminate all sessions for a specific user
    3. Terminate all sessions created before a specific date
    4. Terminate sessions for a specific user created before a specific date (combining filters)

    \b
    Usage Examples:

    \b
    # Terminate a specific session (dry run first)
    ideactl terminate-sessions --session-id dcv-session-12345 --dry-run
    ideactl terminate-sessions --session-id dcv-session-12345

    \b
    # Terminate all sessions for a user
    ideactl terminate-sessions --username john.doe --dry-run
    ideactl terminate-sessions --username john.doe --force

    \b
    # Terminate all sessions created before a date
    ideactl terminate-sessions --created-before 2024-01-15 --dry-run
    ideactl terminate-sessions --created-before 2024-01-15 --force

    \b
    # Terminate sessions for a specific user created before a date (combined filters)
    ideactl terminate-sessions --username john.doe --created-before 2024-01-15 --dry-run
    ideactl terminate-sessions --username john.doe --created-before 2024-01-15 --force

    \b
    Args:
        session_id: Specific IDEA session ID to terminate (cannot be combined with other filters)
        username: Username to terminate sessions for (can be combined with created_before)
        created_before: Date in YYYY-MM-DD format to terminate sessions created before (can be combined with username)
        dry_run: If True, only shows what would be terminated without making changes
        force: If True, skips confirmation prompts

    \b
    Requirements:
        - AWS credentials with DynamoDB access
        - IDEA cluster environment variables set
        - Access to sessions DynamoDB table
    """

    import boto3
    from ideasdk.utils import EnvironmentUtils
    from datetime import datetime

    # Validate input - session-id must be used alone, but username and created-before can be combined
    if not any([session_id, username, created_before]):
        click.echo(
            '❌ Error: You must specify at least one of --session-id, --username, or --created-before'
        )
        return

    if session_id and (username or created_before):
        click.echo('❌ Error: --session-id cannot be combined with other filters')
        return

    # Validate date format if provided
    cutoff_date = None
    if created_before:
        try:
            cutoff_date = datetime.strptime(created_before, '%Y-%m-%d')
            click.echo(
                f'🗓️  Will terminate sessions created before: {cutoff_date.strftime("%Y-%m-%d")}'
            )
        except ValueError:
            click.echo('❌ Error: Date must be in YYYY-MM-DD format (e.g., 2024-01-15)')
            return

    # Get AWS region from environment
    aws_region = EnvironmentUtils.aws_default_region(required=True)

    # Get DynamoDB client
    dynamodb = boto3.resource('dynamodb', region_name=aws_region)

    # Build table names
    cluster_name = EnvironmentUtils.idea_cluster_name(required=True)
    module_id = EnvironmentUtils.idea_module_id(required=True)

    sessions_table_name = f'{cluster_name}.{module_id}.controller.user-sessions'

    click.echo('🔍 Scanning for sessions to terminate...')
    click.echo(f'   Sessions table: {sessions_table_name}')

    try:
        sessions_table = dynamodb.Table(sessions_table_name)
    except Exception as e:
        click.echo(f'❌ Error accessing DynamoDB table: {str(e)}')
        return

    sessions_to_terminate = []

    if session_id:
        # Find session by ID - need to scan since session ID is not the hash key
        click.echo(f'🔍 Looking for session with ID: {session_id}')
        try:
            response = sessions_table.scan(
                FilterExpression='idea_session_id = :sid',
                ExpressionAttributeValues={':sid': session_id},
            )
            items = response.get('Items', [])
            if items:
                sessions_to_terminate.extend(items)
                click.echo(f'✅ Found session: {session_id}')
            else:
                click.echo(f'❌ Session not found: {session_id}')
                return
        except Exception as e:
            click.echo(f'❌ Error searching for session {session_id}: {str(e)}')
            return

    else:
        # Handle username and/or created_before filters
        if username and created_before:
            # Combined filters: query by username first, then filter by date
            click.echo(
                f'🔍 Looking for sessions for user "{username}" created before {created_before}...'
            )
            try:
                response = sessions_table.query(
                    KeyConditionExpression='#owner_attr = :owner',
                    ExpressionAttributeNames={'#owner_attr': 'owner'},
                    ExpressionAttributeValues={':owner': username},
                )
                user_sessions = response.get('Items', [])
                click.echo(
                    f'✅ Found {len(user_sessions)} total sessions for user: {username}'
                )

                # Filter by creation date
                for item in user_sessions:
                    created_on_value = item.get('created_on')
                    if created_on_value:
                        try:
                            # Handle both string (ISO format) and numeric (epoch timestamp) formats
                            if isinstance(created_on_value, str):
                                # Parse the ISO datetime string
                                created_on = datetime.fromisoformat(
                                    created_on_value.replace('Z', '+00:00')
                                )
                            else:
                                # Handle epoch timestamp (Decimal from DynamoDB)
                                # Convert to float and handle both seconds and milliseconds
                                timestamp_value = float(created_on_value)
                                # If the timestamp is too large, it's likely in milliseconds
                                if (
                                    timestamp_value > 1e10
                                ):  # Greater than year 2001 in seconds
                                    timestamp_value = timestamp_value / 1000.0
                                created_on = datetime.fromtimestamp(timestamp_value)

                            if created_on.date() < cutoff_date.date():
                                sessions_to_terminate.append(item)
                        except (ValueError, TypeError, OSError):
                            # Skip sessions with invalid date formats
                            continue

                click.echo(
                    f'✅ Found {len(sessions_to_terminate)} sessions for user "{username}" created before {created_before}'
                )

            except Exception as e:
                click.echo(
                    f'❌ Error searching for sessions for user {username}: {str(e)}'
                )
                return

        elif username:
            # Query sessions by username only (owner is the hash key)
            click.echo(f'🔍 Looking for all sessions for user: {username}')
            try:
                response = sessions_table.query(
                    KeyConditionExpression='#owner_attr = :owner',
                    ExpressionAttributeNames={'#owner_attr': 'owner'},
                    ExpressionAttributeValues={':owner': username},
                )
                sessions_to_terminate.extend(response.get('Items', []))
                click.echo(
                    f'✅ Found {len(sessions_to_terminate)} sessions for user: {username}'
                )
            except Exception as e:
                click.echo(
                    f'❌ Error searching for sessions for user {username}: {str(e)}'
                )
                return

        elif created_before:
            # Scan all sessions and filter by creation date only
            click.echo(
                f'🔍 Scanning all sessions for those created before {created_before}...'
            )
            try:
                # Use scan with pagination to handle large tables
                paginator = sessions_table.meta.client.get_paginator('scan')
                page_iterator = paginator.paginate(TableName=sessions_table_name)

                for page in page_iterator:
                    for item in page.get('Items', []):
                        created_on_value = item.get('created_on')
                        if created_on_value:
                            try:
                                # Handle both string (ISO format) and numeric (epoch timestamp) formats
                                if isinstance(created_on_value, str):
                                    # Parse the ISO datetime string
                                    created_on = datetime.fromisoformat(
                                        created_on_value.replace('Z', '+00:00')
                                    )
                                else:
                                    # Handle epoch timestamp (Decimal from DynamoDB)
                                    # Convert to float and handle both seconds and milliseconds
                                    timestamp_value = float(created_on_value)
                                    # If the timestamp is too large, it's likely in milliseconds
                                    if (
                                        timestamp_value > 1e10
                                    ):  # Greater than year 2001 in seconds
                                        timestamp_value = timestamp_value / 1000.0
                                    created_on = datetime.fromtimestamp(timestamp_value)

                                if created_on.date() < cutoff_date.date():
                                    sessions_to_terminate.append(item)
                            except (ValueError, TypeError, OSError):
                                # Skip sessions with invalid date formats
                                continue

                click.echo(
                    f'✅ Found {len(sessions_to_terminate)} sessions created before {created_before}'
                )
            except Exception as e:
                click.echo(f'❌ Error scanning sessions by date: {str(e)}')
                return

    if not sessions_to_terminate:
        click.echo('✅ No sessions found matching the criteria!')
        return

    # Filter out sessions that are already terminated or in terminating states
    terminable_sessions = []
    for session_item in sessions_to_terminate:
        session_state = session_item.get('state')
        session_id_item = session_item.get('idea_session_id')
        session_owner = session_item.get('owner')

        if session_state in ['DELETED', 'DELETING']:
            click.echo(
                f'⚠️  Session {session_id_item} for {session_owner} is already {session_state}, skipping'
            )
            continue

        terminable_sessions.append(session_item)

    if not terminable_sessions:
        click.echo(
            '✅ No terminable sessions found (all are already deleted/deleting)!'
        )
        return

    # Display sessions in a table
    table = Table()
    table.add_column('Session ID', justify='left', no_wrap=False)
    table.add_column('Owner', justify='left', no_wrap=False)
    table.add_column('Name', justify='left', no_wrap=False)
    table.add_column('State', justify='left', no_wrap=False)
    table.add_column('Created On', justify='left', no_wrap=False)
    table.add_column('Instance Type', justify='left', no_wrap=False)

    for session_item in terminable_sessions:
        created_on_value = session_item.get('created_on', 'N/A')
        if created_on_value and created_on_value != 'N/A':
            try:
                # Handle both string (ISO format) and numeric (epoch timestamp) formats
                if isinstance(created_on_value, str):
                    created_on = datetime.fromisoformat(
                        created_on_value.replace('Z', '+00:00')
                    )
                else:
                    # Handle epoch timestamp (Decimal from DynamoDB)
                    # Convert to float and handle both seconds and milliseconds
                    timestamp_value = float(created_on_value)
                    # If the timestamp is too large, it's likely in milliseconds
                    if timestamp_value > 1e10:  # Greater than year 2001 in seconds
                        timestamp_value = timestamp_value / 1000.0
                    created_on = datetime.fromtimestamp(timestamp_value)
                created_on_display = created_on.strftime('%Y-%m-%d %H:%M')
            except (ValueError, TypeError, OSError):
                created_on_display = str(created_on_value)
        else:
            created_on_display = 'N/A'

        server_info = session_item.get('server', {})
        instance_type = (
            server_info.get('instance_type', 'N/A')
            if isinstance(server_info, dict)
            else 'N/A'
        )

        # Color code by state
        state = session_item.get('state', 'UNKNOWN')
        if state in ['READY', 'STOPPED']:
            style = 'green'
        elif state in ['ERROR']:
            style = 'red'
        elif state in ['PROVISIONING', 'CREATING', 'INITIALIZING', 'RESUMING']:
            style = 'yellow'
        else:
            style = 'white'

        table.add_row(
            session_item.get('idea_session_id', 'N/A'),
            session_item.get('owner', 'N/A'),
            session_item.get('name', 'N/A'),
            state,
            created_on_display,
            instance_type,
            style=style,
        )

    click.echo(f'\n🗑️  Sessions to Terminate ({len(terminable_sessions)} sessions):')

    # Use Rich console to properly render the table
    console = Console()
    console.print(table)

    if dry_run:
        click.echo('\n🔍 Dry run mode - no sessions will be terminated')
        return

    # Confirm termination
    if not force:
        if not click.confirm(
            f'\n❗ Are you sure you want to TERMINATE {len(terminable_sessions)} sessions? This action cannot be undone!'
        ):
            click.echo('Termination cancelled')
            return

        # Terminate sessions using the proper API instead of direct DynamoDB updates
    click.echo(
        f'\n🔥 Terminating {len(terminable_sessions)} sessions using VirtualDesktopAdmin.DeleteSessions API...'
    )

    # Convert DynamoDB items to VirtualDesktopSession objects for the API
    sessions_for_api = []
    for session_item in terminable_sessions:
        session = VirtualDesktopSession(
            idea_session_id=session_item.get('idea_session_id'),
            owner=session_item.get('owner'),
        )
        sessions_for_api.append(session)

    try:
        # Get the CLI context to make API calls
        context = build_cli_context(unix_socket_timeout=360000)

        # Call the proper DeleteSessions API
        response = context.unix_socket_client.invoke_alt(
            namespace='VirtualDesktopAdmin.DeleteSessions',
            payload=DeleteSessionRequest(sessions=sessions_for_api),
            result_as=DeleteSessionResponse,
        )

        # Process the API response
        successful_sessions = response.success or []
        failed_sessions = response.failed or []

        # Summary
        click.echo('\n📋 Termination Summary:')
        click.echo(f'   Successfully initiated termination: {len(successful_sessions)}')
        click.echo(f'   Failed to terminate: {len(failed_sessions)}')

        # Display successful terminations
        if successful_sessions:
            click.echo('\n✅ Successfully Terminated Sessions:')
            for session in successful_sessions:
                click.echo(
                    f'   ✅ Session {session.idea_session_id} (owner: {session.owner})'
                )

        # Display failed terminations
        if failed_sessions:
            click.echo('\n❌ Failed Terminations:')
            for session in failed_sessions:
                failure_reason = session.failure_reason or 'Unknown error'
                click.echo(
                    f'   ❌ Session {session.idea_session_id} (owner: {session.owner}): {failure_reason}'
                )

        if successful_sessions:
            click.echo(
                f'\n✅ Successfully initiated termination for {len(successful_sessions)} sessions!'
            )
            click.echo(
                '   Note: Sessions are being terminated asynchronously. Check their status after a few minutes.'
            )

    except Exception as e:
        click.echo(f'\n❌ Error calling DeleteSessions API: {str(e)}')
        click.echo(
            '   This could be due to authentication issues or service unavailability.'
        )
        return
