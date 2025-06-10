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
    ReIndexSoftwareStacksRequest,
    ReIndexSoftwareStacksResponse,
    VirtualDesktopBaseOS,
    VirtualDesktopArchitecture,
    VirtualDesktopSoftwareStack,
    SocaMemory,
    SocaMemoryUnit,
    VirtualDesktopGPU,
    VirtualDesktopTenancy,
)
from ideadatamodel import constants
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.cli import build_cli_context
from ideavirtualdesktopcontroller.app.software_stacks import (
    constants as software_stacks_constants,
)
from ideasdk.aws.opensearch.aws_opensearch_client import AwsOpenSearchClient

import click
import yaml
import os


# Helper functions for CLI operations
def get_software_stack(table, stack_id, base_os, logger):
    """
    Check if a software stack exists in DynamoDB
    """
    try:
        logger.debug(
            f'Checking for software stack {stack_id} with base_os {base_os} in DynamoDB'
        )
        result = table.get_item(
            Key={
                software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY: base_os,
                software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY: stack_id,
            }
        )
        stack_entry = result.get('Item')
        return stack_entry
    except Exception as e:
        logger.error(f'Failed to get software stack {stack_id}: {str(e)}')
        return None


def create_software_stack(table, stack, logger):
    """
    Create a new software stack in DynamoDB
    """
    try:
        # Ensure we're using the enum value for base_os, not the string representation of the enum
        base_os = (
            stack.base_os.value if hasattr(stack.base_os, 'value') else stack.base_os
        )

        # Convert project objects to project IDs (will be empty in CLI mode)
        project_ids = []
        if stack.projects:
            for project in stack.projects:
                project_ids.append(project.project_id)

        # Create the database entry
        db_entry = {
            software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY: base_os,
            software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY: stack.stack_id,
            software_stacks_constants.SOFTWARE_STACK_DB_NAME_KEY: stack.name,
            software_stacks_constants.SOFTWARE_STACK_DB_DESCRIPTION_KEY: stack.description,
            software_stacks_constants.SOFTWARE_STACK_DB_CREATED_ON_KEY: Utils.current_time_ms(),
            software_stacks_constants.SOFTWARE_STACK_DB_UPDATED_ON_KEY: Utils.current_time_ms(),
            software_stacks_constants.SOFTWARE_STACK_DB_AMI_ID_KEY: stack.ami_id,
            software_stacks_constants.SOFTWARE_STACK_DB_ENABLED_KEY: stack.enabled,
            software_stacks_constants.SOFTWARE_STACK_DB_MIN_STORAGE_VALUE_KEY: str(
                stack.min_storage.value
            ),
            software_stacks_constants.SOFTWARE_STACK_DB_MIN_STORAGE_UNIT_KEY: stack.min_storage.unit,
            software_stacks_constants.SOFTWARE_STACK_DB_MIN_RAM_VALUE_KEY: str(
                stack.min_ram.value
            ),
            software_stacks_constants.SOFTWARE_STACK_DB_MIN_RAM_UNIT_KEY: stack.min_ram.unit,
            software_stacks_constants.SOFTWARE_STACK_DB_ARCHITECTURE_KEY: stack.architecture,
            software_stacks_constants.SOFTWARE_STACK_DB_GPU_KEY: stack.gpu,
            software_stacks_constants.SOFTWARE_STACK_DB_POOL_ENABLED_KEY: stack.pool_enabled,
            software_stacks_constants.SOFTWARE_STACK_DB_POOL_ASG_KEY: stack.pool_asg_name,
            software_stacks_constants.SOFTWARE_STACK_DB_LAUNCH_TENANCY_KEY: stack.launch_tenancy,
            software_stacks_constants.SOFTWARE_STACK_DB_PROJECTS_KEY: project_ids,
        }

        logger.debug(f'Creating software stack in DynamoDB: {stack.stack_id}')

        # Save to DynamoDB
        table.put_item(Item=db_entry)
        return db_entry
    except Exception as e:
        # Check if it's a permissions error
        error_str = str(e)
        if 'AccessDeniedException' in error_str or 'not authorized' in error_str:
            logger.error(
                f'Permission denied creating software stack {stack.stack_id}: {error_str}'
            )
            raise Exception(
                f"Permission denied. Your role doesn't have dynamodb:PutItem permission on this table: {error_str}"
            )
        else:
            logger.error(
                f'Failed to create software stack {stack.stack_id}: {error_str}'
            )
            raise e


@click.command(
    context_settings=constants.CLICK_SETTINGS,
    short_help='Re Index all software stacks to Open Search',
)
@click.option(
    '--reset', is_flag=True, help='Clear the OpenSearch index before reindexing'
)
@click.argument('tokens', nargs=-1)
def reindex_software_stacks(reset, tokens, **kwargs):
    context = build_cli_context()
    logger = context.logger('reindex-software-stacks')

    try:
        cluster_name = context.config().get_string(
            'cluster.cluster_name', required=True
        )
        logger.info(f'Reindexing software stacks for cluster: {cluster_name}')

        if reset:
            # Clear the OpenSearch index before reindexing
            index_name = clear_software_stacks_index(context)
            if index_name:
                click.echo(f'OpenSearch index {index_name} has been cleared.')
            else:
                click.echo('Failed to clear OpenSearch index. Check logs for details.')

        request = ReIndexSoftwareStacksRequest()
        _ = context.unix_socket_client.invoke_alt(
            namespace='VirtualDesktopAdmin.ReIndexSoftwareStacks',
            payload=request,
            result_as=ReIndexSoftwareStacksResponse,
        )

    except Exception as e:
        error_message = f'Failed to reindex software stacks: {str(e)}'
        logger.error(error_message)
        click.echo(f'Error: {error_message}')
        click.echo(
            "Make sure you're running this command on the virtual desktop controller node with proper permissions."
        )


@click.command(
    context_settings=constants.CLICK_SETTINGS,
    short_help='Merge software stacks from a YAML file without overwriting existing stacks',
)
@click.option(
    '--file',
    default='/opt/idea/app/virtual-desktop-controller/resources/base-software-stack-config.yaml',
    help='Path to the YAML file containing software stack definitions (default: /opt/idea/app/virtual-desktop-controller/resources/base-software-stack-config.yaml)',
)
@click.option(
    '--dry-run',
    is_flag=True,
    help='Show what would be imported without actually importing',
)
def merge_software_stacks(file, dry_run, **kwargs):
    """
    Merge software stacks from a YAML file into the database.
    Only adds software stacks that don't already exist (based on stack_id and base_os).

    This command is useful when upgrading IDEA to a new version that contains new software stacks.
    It will only add stacks that don't exist yet in your deployment, preserving any customizations
    you've made to existing stacks.

    The YAML file must have the same structure as the base-software-stack-config.yaml file.

    Note: This command requires access to the DynamoDB table:
    [cluster-name].vdc.controller.software-stacks

    Required permissions:
      - dynamodb:GetItem
      - dynamodb:PutItem
      - dynamodb:Scan

    Note: When running in CLI mode, software stacks are added with an empty projects list.
    The web UI will handle project associations when the stacks are viewed.

    Examples:
        # Perform a dry run to see what would be imported (using default YAML path)
        ideactl merge-software-stacks --dry-run

        # Import the stacks from the default path
        ideactl merge-software-stacks

        # Import from a specific file
        ideactl merge-software-stacks --file /path/to/new-stacks.yml
    """
    # Build CLI context
    try:
        context = build_cli_context()
        logger = context.logger('merge-software-stacks')
        logger.info(
            f'CLI context initialized. Starting merge_software_stacks operation with file: {file}, dry_run: {dry_run}'
        )

        # Log some environment info for debugging
        cluster_name = context.config().get_string(
            'cluster.cluster_name', required=True
        )
        aws_region = context.config().get_string('cluster.aws.region', required=True)
        logger.info(
            f'Environment: Cluster name = {cluster_name}, AWS region = {aws_region}'
        )
    except Exception as e:
        click.echo(f'Error initializing CLI context: {str(e)}')
        click.echo(
            "Make sure you're running this command on a virtual desktop controller node."
        )
        return

    # First, validate the file exists
    if not os.path.exists(file):
        click.echo(f'Error: File {file} does not exist.')
        click.echo(
            "If you're using the default path, make sure you have the necessary permissions."
        )
        click.echo('You may need to run this command with sudo for the default path.')
        return

    # Check if the file is readable
    if not os.access(file, os.R_OK):
        click.echo(f'Error: Cannot read file {file}. Check permissions.')
        click.echo('You may need to run this command with sudo.')
        return

    # Load the YAML file
    try:
        with open(file, 'r') as f:
            stack_configs = yaml.safe_load(f)
    except Exception as e:
        click.echo(f'Error loading YAML file: {str(e)}')
        return

    if Utils.is_empty(stack_configs):
        click.echo(f'The YAML file {file} is empty or invalid.')
        return

    # Initialize database connection
    try:
        # Get the DynamoDB table
        cluster_name = context.config().get_string(
            'cluster.cluster_name', required=True
        )
        module_id = 'vdc'  # Using the correct module ID format for your environment
        table_name = f'{cluster_name}.{module_id}.controller.software-stacks'

        logger.info(f'Using DynamoDB table: {table_name}')

        ddb_client = context.aws().dynamodb_table()
        table = ddb_client.Table(table_name)

        # Verify table access by attempting a scan with limit 0
        try:
            logger.info('Testing DynamoDB table access...')
            table.scan(Limit=1)
            logger.info('Successfully accessed DynamoDB table')
        except Exception as e:
            error_message = (
                f'Permission denied accessing DynamoDB table {table_name}: {str(e)}'
            )
            logger.error(error_message)
            click.echo(f'Error: {error_message}')
            click.echo(
                '\nThis command requires DynamoDB access. Please ensure your role has the following permissions:'
            )
            click.echo('  - dynamodb:GetItem')
            click.echo('  - dynamodb:PutItem')
            click.echo('  - dynamodb:Scan')
            click.echo('on the table: ' + table_name)
            return
    except Exception as e:
        error_message = f'Failed to initialize DB connection: {str(e)}'
        logger.error(f'Error: {str(e)}')
        click.echo(f'Error: {error_message}')
        click.echo(
            'This command must be run on the virtual desktop controller instance. Make sure you have the necessary permissions.'
        )
        return

    # Get the default project
    try:
        # In CLI context, we don't have projects_client, so we'll just use an empty projects list
        # The web UI will handle project associations when needed
        logger.info('Using empty projects list for CLI mode')
    except Exception as e:
        error_message = f'Failed to initialize: {str(e)}'
        logger.error(error_message)
        click.echo(f'Error: {error_message}')
        return

    # Track statistics for reporting
    stats = {'total': 0, 'new': 0, 'existing': 0, 'errors': 0}

    click.echo(
        f'{"DRY RUN: " if dry_run else ""}Starting software stack merge from {file}'
    )
    click.echo(
        'This will only add new software stacks and will not modify existing ones.'
    )

    # Process each base OS in the config
    for base_os in VirtualDesktopBaseOS:
        base_os_value = base_os.value
        os_config = stack_configs.get(base_os_value)

        if Utils.is_empty(os_config):
            logger.info(
                f'No configuration found for base_os: {base_os_value}. Skipping.'
            )
            continue

        # Process architectures
        for arch in VirtualDesktopArchitecture:
            arch_key = arch.replace('_', '-').lower()
            arch_config = os_config.get(arch_key)

            if Utils.is_empty(arch_config):
                logger.info(
                    f'No configuration found for architecture: {arch_key} within base_os: {base_os_value}. Skipping.'
                )
                continue

            # Get default values for this OS/architecture combination
            default_name = arch_config.get('default-name')
            default_description = arch_config.get('default-description')
            default_min_storage_value = arch_config.get('default-min-storage-value')
            default_min_storage_unit = arch_config.get('default-min-storage-unit')
            default_min_ram_value = arch_config.get('default-min-ram-value')
            default_min_ram_unit = arch_config.get('default-min-ram-unit')

            # Validate required defaults
            if (
                Utils.is_empty(default_name)
                or Utils.is_empty(default_description)
                or Utils.is_empty(default_min_storage_value)
                or Utils.is_empty(default_min_storage_unit)
                or Utils.is_empty(default_min_ram_value)
                or Utils.is_empty(default_min_ram_unit)
            ):
                error_message = f'Missing default values for OS: {base_os_value}, Arch: {arch_key}. Skipping.'
                logger.error(error_message)
                click.echo(f'Error: {error_message}')
                continue

            # Get configurations for the current AWS region
            aws_region = context.config().get_string(
                'cluster.aws.region', required=True
            )
            region_configs = arch_config.get(aws_region)

            if Utils.is_empty(region_configs):
                logger.info(
                    f'No configuration found for region: {aws_region} within base_os: {base_os_value}, arch: {arch_key}. Skipping.'
                )
                continue

            # Process each configuration for this region
            for region_config in region_configs:
                stats['total'] += 1

                # Validate required fields
                ami_id = region_config.get('ami-id')
                ss_id_suffix = region_config.get('ss-id-suffix')

                if Utils.is_empty(ami_id) or Utils.is_empty(ss_id_suffix):
                    error_message = f'Missing ami-id or ss-id-suffix for OS: {base_os_value}, Arch: {arch_key}, Region: {aws_region}.'
                    logger.error(error_message)
                    click.echo(f'Error: {error_message}')
                    stats['errors'] += 1
                    continue

                # Handle GPU manufacturer
                gpu_manufacturer = region_config.get('gpu-manufacturer', 'NO_GPU')
                if gpu_manufacturer not in {'AMD', 'NVIDIA', 'NO_GPU'}:
                    error_message = f'Invalid gpu-manufacturer {gpu_manufacturer} for OS: {base_os_value}, Arch: {arch_key}, Region: {aws_region}.'
                    logger.error(error_message)
                    click.echo(f'Error: {error_message}')
                    stats['errors'] += 1
                    continue

                # Get custom stack properties or fall back to defaults
                custom_stack_name = region_config.get('name', default_name)
                custom_stack_description = region_config.get(
                    'description', default_description
                )
                custom_stack_min_storage_value = region_config.get(
                    'min-storage-value', default_min_storage_value
                )
                custom_stack_min_storage_unit = region_config.get(
                    'min-storage-unit', default_min_storage_unit
                )
                custom_stack_min_ram_value = region_config.get(
                    'min-ram-value', default_min_ram_value
                )
                custom_stack_min_ram_unit = region_config.get(
                    'min-ram-unit', default_min_ram_unit
                )
                custom_stack_gpu_manufacturer = VirtualDesktopGPU(gpu_manufacturer)

                # Construct the software stack ID
                software_stack_id = f'{software_stacks_constants.BASE_STACK_PREFIX}-{base_os_value}-{arch_key}-{ss_id_suffix}'

                # Check if the software stack already exists
                existing_stack = get_software_stack(
                    table, software_stack_id, base_os_value, logger
                )

                if Utils.is_not_empty(existing_stack):
                    logger.info(
                        f'Software stack {software_stack_id} already exists. Skipping.'
                    )
                    click.echo(f'Skipping existing stack: {software_stack_id}')
                    stats['existing'] += 1
                    continue

                # Create the new software stack
                new_stack = VirtualDesktopSoftwareStack(
                    base_os=base_os,
                    stack_id=software_stack_id,
                    name=custom_stack_name,
                    description=custom_stack_description,
                    ami_id=ami_id,
                    enabled=True,
                    min_storage=SocaMemory(
                        value=custom_stack_min_storage_value,
                        unit=SocaMemoryUnit(custom_stack_min_storage_unit),
                    ),
                    min_ram=SocaMemory(
                        value=custom_stack_min_ram_value,
                        unit=SocaMemoryUnit(custom_stack_min_ram_unit),
                    ),
                    architecture=VirtualDesktopArchitecture(arch),
                    gpu=custom_stack_gpu_manufacturer,
                    projects=[],
                    pool_enabled=False,
                    pool_asg_name=None,
                    launch_tenancy=VirtualDesktopTenancy.DEFAULT,
                )

                if dry_run:
                    logger.info(
                        f'Would create software stack: {software_stack_id} (DRY RUN)'
                    )
                    click.echo(f'Would create: {software_stack_id} with AMI {ami_id}')
                    stats['new'] += 1
                else:
                    try:
                        # Create the stack in the database
                        create_software_stack(table, new_stack, logger)
                        logger.info(f'Created software stack: {software_stack_id}')
                        click.echo(f'Created: {software_stack_id}')
                        stats['new'] += 1
                    except Exception as e:
                        error_message = f'Failed to create software stack {software_stack_id}: {str(e)}'
                        logger.error(error_message)
                        click.echo(f'Error: {error_message}')
                        stats['errors'] += 1

    # Print summary
    click.echo(f'\n{"DRY RUN: " if dry_run else ""}Software stack merge summary:')
    click.echo(f'Total stacks processed: {stats["total"]}')
    click.echo(f'New stacks {"to be " if dry_run else ""}added: {stats["new"]}')
    click.echo(f'Existing stacks skipped: {stats["existing"]}')
    click.echo(f'Errors encountered: {stats["errors"]}')

    # If there were additions, show a success message
    if not dry_run and stats['new'] > 0:
        click.echo('\n✓ Successfully added new software stacks to the database!')

    # If not a dry run, reindex to update OpenSearch
    if not dry_run and stats['new'] > 0:
        try:
            click.echo('\nReindexing software stacks to update OpenSearch...')

            # Clear the OpenSearch index first (like --reset option)
            click.echo('Clearing OpenSearch index...')
            index_name = clear_software_stacks_index(context)
            if index_name:
                click.echo(f'✓ OpenSearch index {index_name} has been cleared.')
            else:
                click.echo(
                    '⚠️  Warning: Failed to clear OpenSearch index, but continuing with reindex...'
                )

            # Now reindex
            request = ReIndexSoftwareStacksRequest()
            _ = context.unix_socket_client.invoke_alt(
                namespace='VirtualDesktopAdmin.ReIndexSoftwareStacks',
                payload=request,
                result_as=ReIndexSoftwareStacksResponse,
            )
            click.echo('✓ Reindexing complete.')
            click.echo(
                '\nThe updated software stacks are now available for virtual desktop sessions.'
            )
            logger.info(
                'Successfully cleared and reindexed software stacks after AMI updates'
            )
        except Exception as e:
            error_str = str(e)
            logger.error(f'Failed to reindex software stacks: {error_str}')

            # Even if reindexing fails, the stacks were added to DynamoDB
            click.echo(
                '\n⚠️  Software stacks were successfully added to DynamoDB, but reindexing failed.'
            )
            click.echo(
                'The additions are saved but may not be immediately visible in the web UI.'
            )
            click.echo('\nTo manually reindex, run:')
            click.echo('ideactl reindex-software-stacks --reset')
            click.echo(f'\nError details: {error_str}')
    elif dry_run and stats['new'] > 0:
        click.echo(
            '\nTo actually import these stacks, run the command without the --dry-run flag:'
        )
        click.echo('ideactl merge-software-stacks')
    elif stats['new'] == 0:
        click.echo(
            '\nNo new software stacks were found to import. Your deployment is already up to date.'
        )


def clear_software_stacks_index(context):
    """
    Clear the OpenSearch index for software stacks without deleting the index.
    """
    try:
        # Get the index name from the same pattern used in the application
        software_stack_alias = context.config().get_string(
            'virtual-desktop-controller.opensearch.software_stack.alias', required=True
        )

        # Create OpenSearch client
        os_client = AwsOpenSearchClient(context)

        # Get the actual index name that maps to the alias
        try:
            aliases_response = os_client.os_client.indices.get_alias(
                name=software_stack_alias
            )

            # If we have the alias, clear all associated indices
            if aliases_response:
                for index_name in aliases_response:
                    # Delete by query to remove all documents but keep the index
                    os_client.os_client.delete_by_query(
                        index=index_name,
                        body={'query': {'match_all': {}}},
                        refresh=True,
                    )
                return software_stack_alias
        except Exception as e:
            # If the alias doesn't exist or other error, try with the versioned index pattern
            context.logger('reindex-software-stacks').warning(
                f'Error clearing index with alias {software_stack_alias}: {str(e)}'
            )

        # Try with the specific versioned index pattern as fallback
        try:
            # This pattern follows the same one used in virtual_desktop_software_stack_utils.py
            # We're looking for all indices that match the pattern
            indices = os_client.os_client.indices.get(f'{software_stack_alias}-*')

            for index_name in indices:
                os_client.os_client.delete_by_query(
                    index=index_name, body={'query': {'match_all': {}}}, refresh=True
                )
            return f'{software_stack_alias}-*'
        except Exception as e:
            context.logger('reindex-software-stacks').error(
                f'Failed to clear software stack indices: {str(e)}'
            )
            return None
    except Exception as e:
        context.logger('reindex-software-stacks').error(
            f'Failed to clear software stack indices: {str(e)}'
        )
        return None


def get_opensearch_client(context):
    """
    Get the OpenSearch client from the context.
    This function may need to be adjusted based on the actual implementation.
    """
    # This is a placeholder. The actual implementation will depend on how
    # OpenSearch is configured in the application.
    # Options might include:
    # 1. Using a client already available in the context
    # 2. Creating a new client using configuration from the context
    # 3. Importing and using an existing client from elsewhere in the codebase

    # For example:
    # from some.module import opensearch_client
    # return opensearch_client

    # Or:
    # return OpenSearch(
    #     hosts=[{'host': context.config.opensearch_host, 'port': context.config.opensearch_port}],
    #     http_auth=(context.config.opensearch_username, context.config.opensearch_password),
    #     use_ssl=True,
    #     verify_certs=False
    # )

    # For now, we'll check if the context has an opensearch_client attribute
    if hasattr(context, 'opensearch_client'):
        return context.opensearch_client

    # If not, we might need to look elsewhere or create one
    # This is just a placeholder and should be replaced with actual implementation
    raise NotImplementedError('OpenSearch client retrieval not implemented')


def get_software_stacks_index_name(context):
    """
    Get the name of the OpenSearch index for software stacks.
    This function may need to be adjusted based on the actual implementation.
    """
    # This is a placeholder. The actual implementation will depend on how
    # the index name is configured in the application.

    # For example:
    # return context.config.opensearch_software_stacks_index

    # For now, we'll check if the context has configuration for this
    if hasattr(context, 'config') and hasattr(
        context.config, 'opensearch_software_stacks_index'
    ):
        return context.config.opensearch_software_stacks_index

    # If not, we might need to use a default or look elsewhere
    # This is just a placeholder and should be replaced with actual implementation
    return 'software_stacks'


# AMI search patterns for different OS/architecture combinations
AMI_PATTERNS = {
    'windows2019/x86-64/base': 'Windows_Server-2019-English-Full-Base-2025.*',
    'windows2022/x86-64/base': 'Windows_Server-2022-English-Full-Base-2025.*',
    'windows2025/x86-64/base': 'Windows_Server-2025-English-Full-Base-2025.*',
    'rhel8/arm64': 'RHEL-8.10.0_HVM-*-arm64-*',
    'rhel8/x86-64': 'RHEL-8.10.0_HVM-*-x86_64-*',
    'rhel9/arm64': 'RHEL-9.5.0_HVM-*-arm64-*',
    'rhel9/x86-64': 'RHEL-9.5.0_HVM-*-x86_64-*',
    'ubuntu2204/arm64': 'ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*',
    'ubuntu2204/x86-64': 'ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*',
    'ubuntu2404/arm64': 'ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*',
    'ubuntu2404/x86-64': 'ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*',
    'amazonlinux2/arm64': 'amzn2-ami-kernel-5.10-hvm-*-arm64-gp2',
    'amazonlinux2/x86-64': 'amzn2-ami-kernel-5.10-hvm-*-x86_64-gp2',
    'amazonlinux2023/arm64': 'al2023-ami-2023.7*-kernel-6.1-arm64',
    'amazonlinux2023/x86-64': 'al2023-ami-2023.7.*-kernel-6.1-x86_64',
    'rocky8/arm64': 'Rocky-8-EC2-Base-8.9-*.aarch64-*',
    'rocky8/x86-64': 'Rocky-8-EC2-Base-8.9-*.x86_64-*',
    'rocky9/arm64': 'Rocky-9-EC2-Base-9.5-*.aarch64-*',
    'rocky9/x86-64': 'Rocky-9-EC2-Base-9.5-*.x86_64-*',
}


def get_ami_pattern_for_stack(stack_id):
    """
    Extract the OS/architecture pattern from a software stack ID.

    Args:
        stack_id: Software stack ID like 'ss-base-amazonlinux2023-x86-64-dcv'

    Returns:
        Tuple of (pattern_key, ami_pattern, owners) or (None, None, None) if not found
    """
    # Remove the 'ss-base-' prefix
    if not stack_id.startswith('ss-base-'):
        return None, None, None

    # Extract the part after 'ss-base-'
    stack_suffix = stack_id[8:]  # Remove 'ss-base-'

    # Try to match patterns in order of specificity
    for pattern_key, ami_pattern in AMI_PATTERNS.items():
        os_arch = pattern_key.replace('/base', '').replace('/', '-')

        # Check if the stack suffix starts with this OS/arch pattern
        if stack_suffix.startswith(os_arch):
            # Use simplified owners list that covers most cases
            owners = ['amazon', 'aws-marketplace']
            return pattern_key, ami_pattern, owners

    return None, None, None


def find_latest_ami(ec2_client, pattern, owners, logger, ami_type=None):
    """
    Find the latest AMI matching the given pattern.

    Args:
        ec2_client: Boto3 EC2 client
        pattern: AMI name pattern to search for
        owners: List of AMI owner IDs (kept for backward compatibility, will be overridden for rocky8/9)
        logger: Logger instance
        ami_type: AMI type to determine the correct owner (optional)

    Returns:
        AMI ID of the latest matching AMI, or None if not found
    """
    try:
        # Use aws-marketplace only for rocky8 and rocky9
        if ami_type in ['rocky8', 'rocky9']:
            actual_owners = ['aws-marketplace']
        else:
            actual_owners = ['amazon']

        logger.debug(
            f'Searching for AMIs with pattern: {pattern}, owners: {actual_owners}'
        )

        response = ec2_client.describe_images(
            Filters=[
                {'Name': 'name', 'Values': [pattern]},
                {'Name': 'state', 'Values': ['available']},
                {'Name': 'architecture', 'Values': ['x86_64', 'arm64']},
            ],
            Owners=actual_owners,
        )

        images = response.get('Images', [])
        if not images:
            logger.warning(f'No AMIs found matching pattern: {pattern}')
            return None

        # Sort by creation date to get the latest
        images.sort(key=lambda x: x['CreationDate'], reverse=True)
        latest_ami = images[0]

        logger.info(
            f'Found latest AMI: {latest_ami["ImageId"]} ({latest_ami["Name"]}) created on {latest_ami["CreationDate"]}'
        )
        return latest_ami['ImageId']

    except Exception as e:
        logger.error(f'Error searching for AMI with pattern {pattern}: {str(e)}')
        return None


def update_software_stack_ami(table, stack_id, base_os, new_ami_id, logger):
    """
    Update the AMI ID for a software stack in DynamoDB.

    Args:
        table: DynamoDB table resource
        stack_id: Software stack ID
        base_os: Base OS value
        new_ami_id: New AMI ID to set
        logger: Logger instance

    Returns:
        True if successful, False otherwise
    """
    try:
        logger.debug(f'Updating software stack {stack_id} with new AMI: {new_ami_id}')

        table.update_item(
            Key={
                software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY: base_os,
                software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY: stack_id,
            },
            UpdateExpression='SET #ami_id = :new_ami_id, #updated_on = :updated_on',
            ExpressionAttributeNames={
                '#ami_id': software_stacks_constants.SOFTWARE_STACK_DB_AMI_ID_KEY,
                '#updated_on': software_stacks_constants.SOFTWARE_STACK_DB_UPDATED_ON_KEY,
            },
            ExpressionAttributeValues={
                ':new_ami_id': new_ami_id,
                ':updated_on': Utils.current_time_ms(),
            },
        )

        logger.info(
            f'Successfully updated software stack {stack_id} with AMI {new_ami_id}'
        )
        return True

    except Exception as e:
        logger.error(f'Failed to update software stack {stack_id}: {str(e)}')
        return False


@click.command(
    context_settings=constants.CLICK_SETTINGS,
    short_help='Update base software stacks with the latest AMI versions',
)
@click.option(
    '--dry-run',
    is_flag=True,
    help='Show what would be updated without actually updating',
)
@click.option(
    '--stack-id',
    help='Update only a specific stack ID (must start with ss-base-)',
)
@click.option(
    '--force',
    is_flag=True,
    help='Force update all stacks even if they already have the latest AMI',
)
def update_base_stacks(dry_run, stack_id, force, **kwargs):
    """
    Update base software stacks with the latest AMI versions.

    This command scans for software stacks that start with 'ss-base-' and
    updates them with the latest available AMI based on predefined patterns
    for different operating systems and architectures.

    \b
    The command will:
    1. Find all software stacks starting with 'ss-base-'
       (or a specific one if --stack-id is provided)
    2. For each stack, determine the appropriate AMI search pattern
    3. Search for the latest AMI matching that pattern
    4. Update the stack with the new AMI ID

    \b
    Note: This command requires the following permissions:
      - dynamodb:Scan (to find stacks)
      - dynamodb:UpdateItem (to update stacks)
      - ec2:DescribeImages (to find latest AMIs)

    \b
    Examples:
        # Perform a dry run to see what would be updated
        ideactl update-base-stacks --dry-run

        # Update all base stacks
        ideactl update-base-stacks

        # Force update all stacks even if they already have the latest AMI
        ideactl update-base-stacks --force

        # Update only a specific stack
        ideactl update-base-stacks --stack-id ss-base-amazonlinux2023-x86-64-dcv
    """
    # Build CLI context
    try:
        context = build_cli_context()
        logger = context.logger('update-base-stacks')
        logger.info(
            f'Starting update_base_stacks operation with dry_run: {dry_run}, stack_id: {stack_id}, force: {force}'
        )

        # Log environment info
        cluster_name = context.config().get_string(
            'cluster.cluster_name', required=True
        )
        aws_region = context.config().get_string('cluster.aws.region', required=True)
        logger.info(
            f'Environment: Cluster name = {cluster_name}, AWS region = {aws_region}'
        )
    except Exception as e:
        click.echo(f'Error initializing CLI context: {str(e)}')
        click.echo(
            "Make sure you're running this command on a virtual desktop controller node."
        )
        return

    # Initialize database connection
    try:
        cluster_name = context.config().get_string(
            'cluster.cluster_name', required=True
        )
        module_id = 'vdc'
        table_name = f'{cluster_name}.{module_id}.controller.software-stacks'

        logger.info(f'Using DynamoDB table: {table_name}')

        ddb_client = context.aws().dynamodb_table()
        table = ddb_client.Table(table_name)

        # Verify table access
        try:
            logger.info('Testing DynamoDB table access...')
            table.scan(Limit=1)
            logger.info('Successfully accessed DynamoDB table')
        except Exception as e:
            error_message = (
                f'Permission denied accessing DynamoDB table {table_name}: {str(e)}'
            )
            logger.error(error_message)
            click.echo(f'Error: {error_message}')
            click.echo(
                '\nThis command requires DynamoDB access. Please ensure your role has the following permissions:'
            )
            click.echo('  - dynamodb:Scan')
            click.echo('  - dynamodb:UpdateItem')
            click.echo('on the table: ' + table_name)
            return
    except Exception as e:
        error_message = f'Failed to initialize DB connection: {str(e)}'
        logger.error(error_message)
        click.echo(f'Error: {error_message}')
        return

    # Initialize EC2 client
    try:
        ec2_client = context.aws().ec2()
        logger.info('EC2 client initialized successfully')
    except Exception as e:
        error_message = f'Failed to initialize EC2 client: {str(e)}'
        logger.error(error_message)
        click.echo(f'Error: {error_message}')
        click.echo(
            'This command requires EC2 access. Please ensure your role has ec2:DescribeImages permission.'
        )
        return

    # Track statistics
    stats = {'total': 0, 'updated': 0, 'skipped': 0, 'errors': 0}

    click.echo(
        f'{"DRY RUN: " if dry_run else ""}Starting base software stack AMI updates'
    )

    try:
        # Scan for software stacks
        if stack_id:
            # Validate the stack ID
            if not stack_id.startswith('ss-base-'):
                click.echo(
                    f'Error: Stack ID must start with "ss-base-", got: {stack_id}'
                )
                return

            # Find the specific stack by scanning (since we need both hash and range key)
            logger.info(f'Searching for specific stack: {stack_id}')
            response = table.scan(
                FilterExpression='begins_with(#range_key, :stack_prefix) AND #range_key = :stack_id',
                ExpressionAttributeNames={
                    '#range_key': software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY,
                },
                ExpressionAttributeValues={
                    ':stack_prefix': 'ss-base-',
                    ':stack_id': stack_id,
                },
            )
            stacks = response.get('Items', [])
        else:
            # Scan for all stacks starting with 'ss-base-'
            logger.info('Scanning for all base software stacks...')
            response = table.scan(
                FilterExpression='begins_with(#range_key, :stack_prefix)',
                ExpressionAttributeNames={
                    '#range_key': software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY,
                },
                ExpressionAttributeValues={
                    ':stack_prefix': 'ss-base-',
                },
            )
            stacks = response.get('Items', [])

        if not stacks:
            if stack_id:
                click.echo(f'No software stack found with ID: {stack_id}')
            else:
                click.echo(
                    'No base software stacks found (stacks starting with "ss-base-")'
                )
            return

        logger.info(f'Found {len(stacks)} base software stack(s) to process')

        for stack in stacks:
            stats['total'] += 1

            current_stack_id = stack.get(
                software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY
            )
            current_base_os = stack.get(
                software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY
            )
            current_ami_id = stack.get(
                software_stacks_constants.SOFTWARE_STACK_DB_AMI_ID_KEY
            )
            stack_name = stack.get(
                software_stacks_constants.SOFTWARE_STACK_DB_NAME_KEY, 'Unknown'
            )

            logger.info(
                f'Processing stack: {current_stack_id} (current AMI: {current_ami_id})'
            )

            # Get the AMI pattern for this stack
            pattern_key, ami_pattern, owners = get_ami_pattern_for_stack(
                current_stack_id
            )

            if not pattern_key:
                logger.warning(f'No AMI pattern found for stack: {current_stack_id}')
                click.echo(
                    f'Skipping {current_stack_id}: No matching AMI pattern found'
                )
                stats['skipped'] += 1
                continue

            logger.info(f'Using AMI pattern: {ami_pattern} with owners: {owners}')

            # Extract AMI type from stack ID for owner determination
            # Stack ID pattern: ss-base-{ami_type}-{arch}-{suffix}
            ami_type = None
            if current_stack_id.startswith('ss-base-'):
                parts = current_stack_id[8:].split('-')  # Remove 'ss-base-' prefix
                if len(parts) >= 2:
                    # For patterns like 'amazonlinux2023-x86' or 'rocky8-arm64'
                    ami_type = parts[0]

            # Find the latest AMI
            latest_ami_id = find_latest_ami(
                ec2_client, ami_pattern, owners, logger, ami_type
            )

            if not latest_ami_id:
                logger.error(f'Could not find latest AMI for stack: {current_stack_id}')
                click.echo(f'Error: Could not find latest AMI for {current_stack_id}')
                stats['errors'] += 1
                continue

            # Check if update is needed (skip this check if --force is used)
            if not force and latest_ami_id == current_ami_id:
                logger.info(
                    f'Stack {current_stack_id} already has the latest AMI: {current_ami_id}'
                )
                click.echo(f'Up to date: {current_stack_id} ({stack_name})')
                stats['skipped'] += 1
                continue

            # Update needed (or forced)
            if dry_run:
                if force and latest_ami_id == current_ami_id:
                    logger.info(
                        f'Would force update {current_stack_id} with same AMI: {latest_ami_id} (DRY RUN)'
                    )
                    click.echo(
                        f'Would force update: {current_stack_id} ({stack_name}) - FORCED'
                    )
                    click.echo(f'  AMI: {latest_ami_id} (same)')
                else:
                    logger.info(
                        f'Would update {current_stack_id} from {current_ami_id} to {latest_ami_id} (DRY RUN)'
                    )
                    click.echo(f'Would update: {current_stack_id} ({stack_name})')
                    click.echo(f'  Current AMI: {current_ami_id}')
                    click.echo(f'  New AMI: {latest_ami_id}')
                stats['updated'] += 1
            else:
                # Perform the update
                success = update_software_stack_ami(
                    table, current_stack_id, current_base_os, latest_ami_id, logger
                )

                if success:
                    if force and latest_ami_id == current_ami_id:
                        click.echo(
                            f'Force updated: {current_stack_id} ({stack_name}) - FORCED'
                        )
                        click.echo(f'  AMI: {latest_ami_id} (refreshed)')
                    else:
                        click.echo(f'Updated: {current_stack_id} ({stack_name})')
                        click.echo(f'  Old AMI: {current_ami_id}')
                        click.echo(f'  New AMI: {latest_ami_id}')
                    stats['updated'] += 1
                else:
                    click.echo(f'Failed to update: {current_stack_id}')
                    stats['errors'] += 1

    except Exception as e:
        error_message = f'Error during stack processing: {str(e)}'
        logger.error(error_message)
        click.echo(f'Error: {error_message}')
        return

    # Print summary
    click.echo(f'\n{"DRY RUN: " if dry_run else ""}Base software stack update summary:')
    click.echo(f'Total stacks processed: {stats["total"]}')
    if force:
        click.echo(
            f'Stacks {"to be " if dry_run else ""}updated (including forced): {stats["updated"]}'
        )
    else:
        click.echo(f'Stacks {"to be " if dry_run else ""}updated: {stats["updated"]}')
    click.echo(f'Stacks already up to date: {stats["skipped"]}')
    click.echo(f'Errors encountered: {stats["errors"]}')

    # If there were updates, automatically reindex OpenSearch
    if not dry_run and stats['updated'] > 0:
        if force:
            click.echo(
                '\n✓ Successfully updated base software stacks (including forced updates)!'
            )
        else:
            click.echo('\n✓ Successfully updated base software stacks!')
        try:
            click.echo('\nReindexing software stacks to update OpenSearch...')

            # Clear the OpenSearch index first (like --reset option)
            click.echo('Clearing OpenSearch index...')
            index_name = clear_software_stacks_index(context)
            if index_name:
                click.echo(f'✓ OpenSearch index {index_name} has been cleared.')
            else:
                click.echo(
                    '⚠️  Warning: Failed to clear OpenSearch index, but continuing with reindex...'
                )

            # Now reindex
            request = ReIndexSoftwareStacksRequest()
            _ = context.unix_socket_client.invoke_alt(
                namespace='VirtualDesktopAdmin.ReIndexSoftwareStacks',
                payload=request,
                result_as=ReIndexSoftwareStacksResponse,
            )
            click.echo('✓ Reindexing complete.')
            click.echo(
                '\nThe updated software stacks are now available for virtual desktop sessions.'
            )
            logger.info(
                'Successfully cleared and reindexed software stacks after AMI updates'
            )
        except Exception as e:
            error_str = str(e)
            logger.error(f'Failed to reindex software stacks: {error_str}')

            # Even if reindexing fails, the stacks were updated in DynamoDB
            click.echo(
                '\n⚠️  Software stacks were successfully updated in DynamoDB, but reindexing failed.'
            )
            click.echo(
                'The updates are saved but may not be immediately visible in the web UI.'
            )
            click.echo('\nTo manually reindex, run:')
            click.echo('ideactl reindex-software-stacks --reset')
            click.echo(f'\nError details: {error_str}')
    elif dry_run and stats['updated'] > 0:
        click.echo(
            '\nTo actually perform these updates, run the command without --dry-run:'
        )
        click.echo('ideactl update-base-stacks')
    elif stats['updated'] == 0 and stats['errors'] == 0:
        click.echo('\nAll base software stacks are already up to date!')
