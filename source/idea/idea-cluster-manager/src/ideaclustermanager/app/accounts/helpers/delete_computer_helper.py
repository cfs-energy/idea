from ideasdk.context import SocaContext
from ideasdk.utils import Utils
from ideadatamodel import exceptions, errorcodes
from ideaclustermanager.app.accounts.ldapclient.active_directory_client import (
    ActiveDirectoryClient,
)
from ideaclustermanager.app.accounts.db.ad_automation_dao import ADAutomationDAO
from ideaclustermanager.app.accounts.helpers.preset_computer_helper import (
    PresetComputeHelper,
)

import ldap  # noqa
from typing import Dict


class DeleteComputerHelper:
    """
    Helper class to delete computer objects from Active Directory
    """

    def __init__(
        self,
        context: SocaContext,
        ldap_client: ActiveDirectoryClient,
        ad_automation_dao: ADAutomationDAO,
        sender_id: str,
        request: Dict,
    ):
        self.context = context
        self.logger = context.logger('delete-computer-helper')
        self.ldap_client = ldap_client
        self.ad_automation_dao = ad_automation_dao
        self.sender_id = sender_id
        self.request = request

    def generate_hostname_from_instance_id(self, instance_id: str) -> str:
        """
        Generate a hostname using the same algorithm as PresetComputeHelper
        """
        aws_region = self.context.config().get_string(
            'cluster.aws.region', required=True
        )
        aws_account = self.context.config().get_string(
            'cluster.aws.account_id', required=True
        )
        cluster_name = self.context.config().get_string(
            'cluster.cluster_name', required=True
        )

        hostname_data = f'{aws_region}|{aws_account}|{cluster_name}|{instance_id}'
        hostname_prefix = self.context.config().get_string(
            'directoryservice.ad_automation.hostname_prefix', default='IDEA-'
        )

        # Calculate available characters (max length of AD computer name is 15)
        avail_chars = 15 - len(hostname_prefix)
        if avail_chars < 4:
            self.logger.warning(
                f'Hostname prefix too long: {hostname_prefix}, using default IDEA-'
            )
            hostname_prefix = 'IDEA-'
            avail_chars = 10  # 15 - 5

        # Take the last n-chars from the resulting shake256 bucket of 256
        shake_value = Utils.shake_256(hostname_data, 256)[(avail_chars * -1) :]
        hostname = f'{hostname_prefix}{shake_value}'.upper()

        self.logger.info(f'Generated hostname for instance {instance_id}: {hostname}')
        return hostname

    def invoke(self):
        """
        Delete a computer object from Active Directory
        """
        try:
            payload = Utils.get_value_as_dict('payload', self.request)
            instance_id = Utils.get_value_as_string('instance_id', payload)

            # Get the provided computer_name if available (might be included in the request)
            provided_computer_name = Utils.get_value_as_string('computer_name', payload)

            # Validate required parameters
            if not instance_id:
                raise exceptions.soca_exception(
                    error_code=errorcodes.AD_AUTOMATION_DELETE_COMPUTER_FAILED,
                    message='Instance ID is required for deletion',
                )

            # Always generate the computer name using our algorithm
            computer_name = self.generate_hostname_from_instance_id(instance_id)
            self.logger.info(f'Generated computer name for deletion: {computer_name}')

            # If a computer name was provided in the request, log it for debugging
            if provided_computer_name and provided_computer_name != computer_name:
                self.logger.warning(
                    f'Provided computer name {provided_computer_name} differs from generated name {computer_name}. Using generated name.'
                )

            self.logger.info(f'Deleting computer {computer_name} from Active Directory')

            # Create a minimal request with just the hostname to satisfy the PresetComputeHelper initialization
            minimal_request = {
                'payload': {
                    'hostname': computer_name,
                    'nonce': f'delete-{Utils.current_time_ms()}',
                }
            }

            preset_helper = PresetComputeHelper(
                context=self.context,
                ldap_client=self.ldap_client,
                ad_automation_dao=self.ad_automation_dao,
                sender_id=self.sender_id,
                request=minimal_request,
            )

            # Get a domain controller and delete the computer account
            domain_controller_ip = preset_helper.get_any_domain_controller_ip()
            self.logger.info(
                f'Using domain controller {domain_controller_ip} to delete computer account {computer_name}'
            )

            # First check if the computer exists in AD
            try:
                exists = preset_helper.is_existing_computer_account()
                if not exists:
                    self.logger.info(
                        f'Computer account {computer_name} does not exist in AD. Nothing to delete.'
                    )
                else:
                    self.logger.info(
                        f'Computer account {computer_name} exists in AD. Proceeding with deletion.'
                    )
                    try:
                        preset_helper.delete_computer(
                            domain_controller_ip=domain_controller_ip
                        )
                        self.logger.info(
                            f'Successfully deleted computer account {computer_name}'
                        )
                    except exceptions.SocaException as e:
                        # Check if the error is because the computer doesn't exist
                        if 'No computer account' in e.message and 'exists' in e.message:
                            self.logger.info(
                                f'Computer account {computer_name} not found in AD. Treating as already deleted.'
                            )
                        else:
                            # Re-raise other errors
                            raise e
            except Exception as e:
                self.logger.warning(
                    f'Error checking if computer account exists: {str(e)}. Will attempt to delete anyway.'
                )
                try:
                    preset_helper.delete_computer(
                        domain_controller_ip=domain_controller_ip
                    )
                    self.logger.info(
                        f'Successfully deleted computer account {computer_name}'
                    )
                except exceptions.SocaException as e:
                    # Check if the error is because the computer doesn't exist
                    if 'No computer account' in e.message and 'exists' in e.message:
                        self.logger.info(
                            f'Computer account {computer_name} not found in AD. Treating as already deleted.'
                        )
                    else:
                        # Re-raise other errors
                        raise e

            # Clean up any entries in the automation table
            if instance_id:
                self.logger.info(
                    f'Cleaning up AD automation entries for instance: {instance_id}'
                )
                automation_entries = self.ad_automation_dao.list_entries_by_instance_id(
                    instance_id
                )
                if not automation_entries:
                    self.logger.info(
                        f'No automation entries found for instance {instance_id}'
                    )
                for entry in automation_entries:
                    nonce = Utils.get_value_as_string('nonce', entry)
                    if nonce:
                        self.logger.info(
                            f'Deleting automation entry for instance {instance_id} with nonce {nonce}'
                        )
                        self.ad_automation_dao.delete_entry(instance_id, nonce)

            self.logger.info(
                f'Computer {computer_name} successfully deleted from Active Directory'
            )

        except ldap.NO_SUCH_OBJECT:
            # The computer object doesn't exist in AD, which is fine as our goal is to make sure it's not there
            self.logger.info(
                f'Computer object {computer_name} not found in AD. Nothing to delete.'
            )
        except Exception as e:
            self.logger.error(
                f'Failed to delete computer from Active Directory: {e}', exc_info=True
            )
            raise exceptions.soca_exception(
                error_code=errorcodes.AD_AUTOMATION_DELETE_COMPUTER_FAILED,
                message=f'Failed to delete computer from Active Directory: {e}',
            )
