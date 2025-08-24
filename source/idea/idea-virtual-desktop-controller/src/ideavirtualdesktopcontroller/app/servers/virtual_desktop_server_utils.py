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
from typing import List

from botocore.exceptions import ClientError

import ideavirtualdesktopcontroller
from ideadatamodel import VirtualDesktopServer, VirtualDesktopSession
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_db import (
    VirtualDesktopServerDB,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
)
from typing import Optional


class VirtualDesktopServerUtils:
    def __init__(
        self,
        context: ideavirtualdesktopcontroller.AppContext,
        db: VirtualDesktopServerDB,
    ):
        self.context = context
        self._logger = context.logger('virtual-desktop-server-utils')
        self.ec2_client = self.context.aws().ec2()
        self._server_db = db
        self._controller_utils = VirtualDesktopControllerUtils(self.context)

    def provision_host_for_session(
        self, session: VirtualDesktopSession
    ) -> Optional[VirtualDesktopServer]:
        self._logger.info(f'initiate_host_provisioning for {session.name}')

        # TODO - need a way to get information from provision_dcv_host_for_session to the end-user/caller

        host_provisioning_response = (
            self._controller_utils.provision_dcv_host_for_session(session)
        )

        instances = Utils.get_value_as_list(
            'Instances', host_provisioning_response, default=[]
        )

        if not instances:
            return None

        # We know that there is ONLY 1 instance
        session.server.instance_id = Utils.get_value_as_string(
            'InstanceId', instances[0], default=None
        )

        if session.server.instance_id is None:
            return None

        return self._server_db.create(
            server=session.server,
            idea_session_id=session.idea_session_id,
            idea_session_owner=session.owner,
        )

    def _stop_dcv_hosts(
        self, servers: List[VirtualDesktopServer], hibernate=False
    ) -> dict:
        if Utils.is_empty(servers):
            self._logger.debug('No servers provided to _stop_dcv_hosts...')
            return {}

        instance_ids = []
        for server in servers:
            instance_ids.append(server.instance_id)

        if hibernate:
            self._logger.debug(f'Hibernating {instance_ids}')
        else:
            self._logger.debug(f'Stopping {instance_ids}')

        response = self.ec2_client.stop_instances(
            InstanceIds=instance_ids, Hibernate=hibernate
        )
        return Utils.to_dict(response)

    def stop_or_hibernate_servers(
        self,
        servers_to_stop: List[VirtualDesktopServer] = None,
        servers_to_hibernate: List[VirtualDesktopServer] = None,
    ):
        if Utils.is_empty(servers_to_stop) and Utils.is_empty(servers_to_hibernate):
            self._logger.debug('No servers provided to stop or hibernate...')
            return {}

        if Utils.is_not_empty(servers_to_stop):
            response = self._stop_dcv_hosts(servers_to_stop)
            instances = Utils.get_value_as_list('StoppingInstances', response, [])
            for instance in instances:
                instance_id = Utils.get_value_as_string('InstanceId', instance, None)
                server = self._server_db.get(instance_id=instance_id)
                server.state = 'STOPPED'
                self._server_db.update(server)

        if Utils.is_not_empty(servers_to_hibernate):
            response = self._stop_dcv_hosts(servers_to_hibernate, hibernate=True)
            instances = Utils.get_value_as_list('StoppingInstances', response, [])
            for instance in instances:
                instance_id = Utils.get_value_as_string('InstanceId', instance, None)
                server = self._server_db.get(instance_id=instance_id)
                server.state = 'HIBERNATED'
                self._server_db.update(server)

    def start_dcv_hosts(self, servers: List[VirtualDesktopServer]) -> dict:
        instance_ids = []
        for server in servers:
            instance_ids.append(server.instance_id)
        try:
            response = self.ec2_client.start_instances(InstanceIds=instance_ids)
        except ClientError as e:
            self._logger.error(e)
            return {'ERROR': str(e)}
        return Utils.to_dict(response)

    def reboot_dcv_hosts(self, servers: List[VirtualDesktopServer]) -> dict:
        if Utils.is_empty(servers):
            return {}

        instance_ids = []
        for server in servers:
            instance_ids.append(server.instance_id)

        response = self.ec2_client.reboot_instances(InstanceIds=instance_ids)
        return Utils.to_dict(response)

    def _terminate_dcv_hosts(self, servers: List[VirtualDesktopServer], force: bool = False) -> dict:
        instance_ids = []
        for server in servers:
            instance_ids.append(server.instance_id)

        kwargs = {
            'InstanceIds': instance_ids
        }
        if force:
            kwargs['Force'] = force
            kwargs['SkipOsShutdown'] = force  # Use skip OS shutdown when force is enabled
            
        response = self.ec2_client.terminate_instances(**kwargs)
        return Utils.to_dict(response)

    def terminate_dcv_hosts(self, servers: List[VirtualDesktopServer], force: bool = False) -> dict:
        if Utils.is_empty(servers):
            return {}

        terminate_response = self._terminate_dcv_hosts(servers, force=force)
        instances = Utils.get_value_as_list(
            'TerminatingInstances', terminate_response, []
        )
        for instance in instances:
            instance_id = Utils.get_value_as_string('InstanceId', instance, None)
            computer_name = None

            # Get the server details to extract the computer name
            server = self._server_db.get(instance_id=instance_id)
            if server is not None:
                self._logger.info(
                    f'Terminating server: {server} with instance_id: {instance_id}'
                )

                # First try to get hostname from server object
                if hasattr(server, 'hostname') and server.hostname:
                    computer_name = server.hostname
                    self._logger.info(f'Using server hostname: {computer_name}')

                # For Windows instances, we need to generate the hostname using the same algorithm as PresetComputeHelper
                # This is necessary because Windows instances might not have the hostname directly available
                else:
                    # Get cluster config values for hostname generation
                    try:
                        aws_region = self.context.config().get_string(
                            'cluster.aws.region', required=True
                        )
                        aws_account = self.context.config().get_string(
                            'cluster.aws.account_id', required=True
                        )
                        cluster_name = self.context.config().get_string(
                            'cluster.cluster_name', required=True
                        )

                        hostname_data = (
                            f'{aws_region}|{aws_account}|{cluster_name}|{instance_id}'
                        )
                        hostname_prefix = self.context.config().get_string(
                            'directoryservice.ad_automation.hostname_prefix',
                            default='IDEA-',
                        )

                        # Calculate available characters (max length of AD computer name is 15)
                        avail_chars = 15 - len(hostname_prefix)
                        if avail_chars < 4:
                            self._logger.warning(
                                f'Hostname prefix too long: {hostname_prefix}, using default IDEA-'
                            )
                            hostname_prefix = 'IDEA-'
                            avail_chars = 10  # 15 - 5

                        # Take the last n-chars from the resulting shake256 bucket of 256
                        shake_value = Utils.shake_256(hostname_data, 256)[
                            (avail_chars * -1) :
                        ]
                        computer_name = f'{hostname_prefix}{shake_value}'.upper()

                        self._logger.info(
                            f'Generated hostname for Windows instance {instance_id}: {computer_name}'
                        )
                    except Exception as e:
                        self._logger.error(
                            f'Failed to generate hostname for Windows instance: {str(e)}'
                        )

            # Only send AD automation delete messages if using Active Directory
            is_active_directory = False
            try:
                is_active_directory = self._controller_utils.is_active_directory()
                self._logger.info(f'Active Directory enabled: {is_active_directory}')
            except Exception as e:
                self._logger.error(f'Error checking Active Directory status: {str(e)}')

            # Send AD automation event to delete the computer object
            if computer_name is not None and is_active_directory:
                try:
                    ad_automation_queue_url = self.context.config().get_string(
                        'directoryservice.ad_automation.sqs_queue_url', required=True
                    )
                    self._logger.info(
                        f'Sending AD automation delete event for computer: {computer_name}, instance: {instance_id} to queue: {ad_automation_queue_url}'
                    )

                    # Create the AD automation request
                    request = {
                        'header': {'namespace': 'ADAutomation.DeleteComputer'},
                        'payload': {
                            'computer_name': computer_name,
                            'instance_id': instance_id,
                        },
                    }

                    # Send the message to SQS - always using FIFO parameters since AD automation queue is always FIFO
                    message_deduplication_id = (
                        f'{instance_id}-{Utils.current_time_ms()}'
                    )
                    self._logger.info(
                        f'Using message deduplication ID: {message_deduplication_id}'
                    )

                    sqs_response = (
                        self.context.aws()
                        .sqs()
                        .send_message(
                            QueueUrl=ad_automation_queue_url,
                            MessageBody=Utils.to_json(request),
                            MessageGroupId='ADAutomation.DeleteComputer',
                            MessageDeduplicationId=message_deduplication_id,
                        )
                    )

                    self._logger.info(f'SQS response: {Utils.to_json(sqs_response)}')
                    self._logger.info(
                        f'Successfully sent AD delete request for {computer_name}'
                    )
                except Exception as e:
                    self._logger.error(
                        f'Failed to send AD automation delete event: {str(e)}',
                        exc_info=True,
                    )
            else:
                if computer_name is None:
                    self._logger.warning(
                        f'Computer name could not be determined for instance {instance_id}. AD cleanup will not occur.'
                    )
                if not is_active_directory:
                    self._logger.info(
                        f'Active Directory is not enabled. Skipping AD cleanup for instance {instance_id}.'
                    )

            self._server_db.delete(VirtualDesktopServer(instance_id=instance_id))
        return terminate_response
