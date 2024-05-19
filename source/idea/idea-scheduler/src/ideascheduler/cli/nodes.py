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

from ideascheduler.cli import build_cli_context
from ideadatamodel import constants, ProvisionAlwaysOnNodesRequest, ProvisionAlwaysOnNodesResult, SocaJobParams
from ideasdk.utils import Utils

import click


@click.command(context_settings=constants.CLICK_SETTINGS, short_help='provision always on nodes')
@click.option('--nodes', help='The no. of nodes to be provisioned.')
@click.option('--owner', required=True, help='The owner of the provisioned capacity')
@click.option('--queue-profile', required=True, help='The name of the queue profile')
@click.option('--project', required=False, help='The name of project')
def provision_always_on_nodes(**kwargs):
    """
    provision always on nodes

    provisions the cloud formation stack for always on queue profile.
    the cloud formation stack, applicable instances and no. of nodes must be managed manually via AWS Console or AWS CLI commands
    """
    queue_profile_name = Utils.get_value_as_string('queue_profile', kwargs)
    context = build_cli_context()
    result = context.unix_socket_client.invoke_alt(
        namespace='SchedulerAdmin.ProvisionAlwaysOnNodes',
        payload=ProvisionAlwaysOnNodesRequest(
            project_name=Utils.get_value_as_string('project', kwargs),
            queue_profile_name=Utils.get_value_as_string('queue_profile', kwargs),
            owner=Utils.get_value_as_string('owner', kwargs),
            params=SocaJobParams(
                nodes=Utils.get_value_as_int('nodes', kwargs)
            )
        ),
        result_as=ProvisionAlwaysOnNodesResult
    )

    context.success(f'provisioning always on nodes for queue profile: {queue_profile_name}')
    context.info(f'+ Stack Name: {result.stack_name}')
    context.info(f'+ Stack Id: {result.stack_id}')
