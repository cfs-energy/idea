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
import re
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

    def invoke(self):
        payload = Utils.get_value_as_dict('payload', self.request, default={})
        instance_id = Utils.get_value_as_string('instance_id', payload)
        computer_name = Utils.get_value_as_string(
            'computer_name', payload
        ) or Utils.get_value_as_string('hostname', payload)
        terminated_instance_id = None
        if (
            not computer_name
            and self.sender_id
            and ':' not in self.sender_id
            and instance_id
            and re.fullmatch(r'i-(?:[0-9a-f]{8}|[0-9a-f]{17})', instance_id)
        ):
            # ASG events have no role session or computer name. EC2 must independently
            # confirm a retired ECS host; the message alone never authorizes deletion.
            terminated_instance_id = instance_id
            computer_name = (
                instance_id  # Replaced by the verified host's generated name below.
            )
        if not computer_name:
            raise exceptions.soca_exception(
                error_code=errorcodes.AD_AUTOMATION_PRESET_COMPUTER_FAILED,
                message='DeleteComputer requires a target computer_name or hostname; use an AD sweep for unnamed legacy requests',
            )

        try:
            # Verify the requester independently of the computer being retired.
            helper = PresetComputeHelper(
                context=self.context,
                ldap_client=self.ldap_client,
                ad_automation_dao=self.ad_automation_dao,
                sender_id=self.sender_id,
                request={
                    'payload': {**payload, 'nonce': f'delete-{Utils.current_time_ms()}'}
                },
                target_computer=computer_name,
                terminated_instance_id=terminated_instance_id,
            )
        except exceptions.SocaException:
            raise
        except Exception as error:
            raise exceptions.soca_exception(
                error_code=errorcodes.AD_AUTOMATION_DELETE_COMPUTER_FAILED,
                message=f'Unable to verify DeleteComputer sender: {error}',
            ) from error

        if terminated_instance_id:
            computer_name = helper.generate_hostname(terminated_instance_id)

        try:
            computers = self.ldap_client.search_s(
                base=helper.ou,
                filterstr=helper.get_ldap_computer_filterstr(computer_name),
                attrlist=['dn'],
            )
            # Referrals are not computer entries. Never fall back to a domain-wide delete.
            computers = [(dn, attrs) for dn, attrs in computers if dn is not None]
            if not computers:
                self.logger.info(
                    f'Refusing AD delete for {computer_name}: no matching computer in cluster OU {helper.ou} (already absent or outside this cluster)'
                )
            elif len(computers) != 1:
                self.logger.error(
                    f'Refusing AD delete for {computer_name}: ambiguous target in cluster OU {helper.ou}'
                )
                return
            else:
                try:
                    self.ldap_client.delete_s(computers[0][0])
                except ldap.NO_SUCH_OBJECT:
                    pass  # A concurrent cleanup already removed this exact DN.
                self.logger.info(
                    f'Deleted computer {computer_name} from cluster OU {helper.ou}'
                )

            if instance_id:
                generated_target = (
                    helper.generate_hostname(instance_id).casefold()
                    == computer_name.casefold()
                )
                for entry in self.ad_automation_dao.list_entries_by_instance_id(
                    instance_id
                ):
                    if not generated_target and (
                        entry.get('status') != 'success'
                        or (entry.get('hostname') or '').casefold()
                        != computer_name.casefold()
                    ):
                        continue
                    nonce = Utils.get_value_as_string('nonce', entry)
                    if nonce:
                        self.ad_automation_dao.delete_entry(instance_id, nonce)
        except Exception as error:
            # A verified request with an AD/DAO failure must remain on the queue.
            raise exceptions.soca_exception(
                error_code=errorcodes.AD_AUTOMATION_DELETE_COMPUTER_FAILED,
                message=f'Failed to delete computer {computer_name} from Active Directory: {error}',
            ) from error
