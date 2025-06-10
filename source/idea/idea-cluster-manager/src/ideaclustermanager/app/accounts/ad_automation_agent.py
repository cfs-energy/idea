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


from ideasdk.service import SocaService
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
from ideaclustermanager.app.accounts.helpers.delete_computer_helper import (
    DeleteComputerHelper,
)

from typing import Dict
from threading import Thread, Event
import arrow
import ldap  # noqa
import time
import random

DEFAULT_MAX_MESSAGES = 1
DEFAULT_WAIT_INTERVAL_SECONDS = 20

AD_RESET_PASSWORD_LOCK_KEY = 'activedirectory.reset-password'


class ADAutomationAgent(SocaService):
    """
    IDEA - Active Directory Automation Agent

    * Manages password rotation of AD Admin credentials
    * Manages automation for creating preset-computers using adcli for cluster nodes

    Developer Note:
    * Expect admins to heavily customize this implementation
    """

    def __init__(self, context: SocaContext, ldap_client: ActiveDirectoryClient):
        super().__init__(context)
        self.context = context
        self.logger = context.logger('ad-automation-agent')
        self.ldap_client = ldap_client

        self.ad_automation_sqs_queue_url = self.context.config().get_string(
            'directoryservice.ad_automation.sqs_queue_url', required=True
        )

        self.ad_automation_dao = ADAutomationDAO(context=self.context)
        self.ad_automation_dao.initialize()

        self._stop_event = Event()
        self._automation_thread = Thread(
            name='ad-automation-thread', target=self.automation_loop
        )

    def is_password_expired(self) -> bool:
        root_username = self.ldap_client.ldap_root_username
        user = self.ldap_client.get_user(root_username, trace=False)
        if user is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message='ActiveDirectory admin user not found',
            )

        password_last_set = Utils.get_value_as_int('password_last_set', user, 0)
        password_max_age = Utils.get_value_as_int('password_max_age', user)

        password_expiry_date = None
        if password_last_set != 0:
            password_last_set_date = arrow.get(password_last_set)
            password_expiry_date = password_last_set_date.shift(days=password_max_age)

        if password_expiry_date is not None:
            now = arrow.utcnow()

            # check for expiration 2 days before expiration
            now_minus_2days = now.shift(days=-2)

            if now_minus_2days < password_expiry_date:
                return False

        return True

    def check_and_reset_admin_password(self):
        """
        check and reset password for AD admin user
        """

        if not self.is_password_expired():
            return

        try:
            # sleep for a random interval to prevent race condition
            time.sleep(random.randint(1, 30))

            self.logger.info('acquiring lock to reset ds credentials ...')
            self.context.distributed_lock().acquire(key=AD_RESET_PASSWORD_LOCK_KEY)

            self.logger.info('reset ds credentials lock acquired.')

            # check again, where other node may have acquired the lock and already performed the update.
            if not self.is_password_expired():
                self.logger.info(
                    'ds credentials already reset. re-sync ds credentials from secrets manager ...'
                )
                self.ldap_client.refresh_root_username_password()
                return

            username = self.ldap_client.ldap_root_username
            new_password = Utils.generate_password(16, 2, 2, 2, 2)

            # change password in Managed AD (calls ds.reset_password)
            success = False
            current_retry = 0
            max_retries = 4
            backoff_in_seconds = 5
            while not success and current_retry < max_retries:
                try:
                    self.ldap_client.change_password(username, new_password)

                    # wait for password change to sync across domain controllers
                    time.sleep(30)

                    success = True
                except exceptions.SocaException as e:
                    if e.error_code == errorcodes.AUTH_USER_NOT_FOUND:
                        self.logger.info(
                            f'reset password for {username}: waiting for user to be synced with AD domain controllers ...'
                        )
                        interval = Utils.get_retry_backoff_interval(
                            current_retry, max_retries, backoff_in_seconds
                        )
                        current_retry += 1
                        time.sleep(interval)
                    else:
                        raise e

            # update root password secret
            self.ldap_client.update_root_password(new_password)
            self.logger.info('ds credentials updated.')

        finally:
            self.context.distributed_lock().release(key=AD_RESET_PASSWORD_LOCK_KEY)
            self.logger.info('reset ds credentials lock released.')

    def automation_loop(self):
        while not self._stop_event.is_set():
            admin_user_ok = False

            try:
                enable_root_password_reset = self.context.config().get_bool(
                    'directoryservice.ad_automation.enable_root_password_reset',
                    default=False,
                )
                if enable_root_password_reset:
                    self.check_and_reset_admin_password()
                admin_user_ok = True

                visibility_timeout = self.context.config().get_int(
                    'directoryservice.ad_automation.sqs_visibility_timeout_seconds',
                    default=60,
                )
                result = (
                    self.context.aws()
                    .sqs()
                    .receive_message(
                        QueueUrl=self.ad_automation_sqs_queue_url,
                        MaxNumberOfMessages=DEFAULT_MAX_MESSAGES,
                        WaitTimeSeconds=DEFAULT_WAIT_INTERVAL_SECONDS,
                        AttributeNames=['SenderId'],
                        VisibilityTimeout=visibility_timeout,
                    )
                )

                sqs_messages = Utils.get_value_as_list('Messages', result, default=[])

                delete_messages = []

                def add_to_delete(sqs_message_: Dict):
                    delete_messages.append(
                        {
                            'Id': sqs_message_['MessageId'],
                            'ReceiptHandle': sqs_message_['ReceiptHandle'],
                        }
                    )

                for sqs_message in sqs_messages:
                    try:
                        message_body = Utils.get_value_as_string('Body', sqs_message)

                        request = Utils.from_json(message_body)
                        # todo - validate request thus far
                        if not request:
                            self.logger.exception(
                                f'Failed to process SQS message. Message Body: ({message_body}) Processing will be retried in {visibility_timeout} seconds ...'
                            )

                        header = Utils.get_value_as_dict('header', request)
                        namespace = Utils.get_value_as_string('namespace', header)

                        # todo - constants for the namespaces supported
                        ad_automation_namespaces = {
                            'ADAutomation.PresetComputer',
                            'ADAutomation.UpdateComputerDescription',
                            'ADAutomation.DeleteComputer',
                        }
                        if namespace not in ad_automation_namespaces:
                            self.logger.error(
                                f'Invalid request: namespace {namespace} not supported. Supported namespaces: {ad_automation_namespaces}'
                            )
                            add_to_delete(sqs_message)
                            continue

                        attributes = Utils.get_value_as_dict(
                            'Attributes', sqs_message, default={}
                        )
                        sender_id = Utils.get_value_as_string('SenderId', attributes)

                        self.logger.info(f'Processing AD automation event: {namespace}')
                        _ad_automation_start = Utils.current_time_ms()
                        try:
                            if namespace == 'ADAutomation.PresetComputer':
                                PresetComputeHelper(
                                    context=self.context,
                                    ldap_client=self.ldap_client,
                                    ad_automation_dao=self.ad_automation_dao,
                                    sender_id=sender_id,
                                    request=request,
                                ).invoke()
                            elif namespace == 'ADAutomation.DeleteComputer':
                                self.logger.debug(
                                    'Processing AD automation event: DeleteComputer'
                                )
                                DeleteComputerHelper(
                                    context=self.context,
                                    ldap_client=self.ldap_client,
                                    ad_automation_dao=self.ad_automation_dao,
                                    sender_id=sender_id,
                                    request=request,
                                ).invoke()
                            elif namespace == 'ADAutomation.UpdateComputerDescription':
                                self.logger.debug(
                                    'Processing AD automation event: UpdateComputerDescription'
                                )

                            # no exception, AD automation succeeded. delete from queue.
                            add_to_delete(sqs_message)

                        except exceptions.SocaException as e:
                            if (
                                e.error_code
                                == errorcodes.AD_AUTOMATION_PRESET_COMPUTER_FAILED
                            ):
                                self.logger.error(f'{e}')
                                add_to_delete(sqs_message)
                            elif (
                                e.error_code
                                == errorcodes.AD_AUTOMATION_PRESET_COMPUTER_RETRY
                            ):
                                # do nothing. request will be retried after visibility timeout interval.
                                self.logger.warning(
                                    f'{e} - request will be retried in {visibility_timeout} seconds'
                                )
                            else:
                                # retry on any unhandled exception.
                                raise e
                        _ad_automation_end = Utils.current_time_ms()
                        self.logger.debug(
                            f'AD Automation event for {namespace} completed - Duration {_ad_automation_end - _ad_automation_start}ms'
                        )

                    except Exception as e:
                        self.logger.exception(
                            f'failed to process sqs message: {e}. payload: {Utils.to_json(sqs_message)}. processing will be retried in {visibility_timeout} seconds ...'
                        )

                if len(delete_messages) > 0:
                    self.logger.info(
                        f'Attempting to delete {len(delete_messages)} AD automation entries from SQS'
                    )
                    delete_message_result = (
                        self.context.aws()
                        .sqs()
                        .delete_message_batch(
                            QueueUrl=self.ad_automation_sqs_queue_url,
                            Entries=delete_messages,
                        )
                    )
                    failed = Utils.get_value_as_list(
                        'Failed', delete_message_result, default=[]
                    )
                    if len(failed) > 0:
                        self.logger.error(
                            f'Failed to delete AD automation entries. This could result in an infinite loop. Consider increasing the directoryservice.ad_automation.sqs_visibility_timeout_seconds. failed messages: {failed}'
                        )

            except KeyboardInterrupt:
                pass
            except Exception as e:
                self.logger.exception(f'ad automation failure: {e}')
            finally:
                # wait only if admin user is not OK and keep retrying.
                # if admin user and/or credentials are ok, wait will be handled by sqs receive message long polling
                if not admin_user_ok:
                    self._stop_event.wait(DEFAULT_WAIT_INTERVAL_SECONDS)

    def start(self):
        self.logger.info('Starting AD Automation Agent sanity checks...')
        self._sanity_check()
        self.logger.info('starting ad automation agent ...')
        self._automation_thread.start()

    def _ddb_sanity_check(self):
        self.logger.info('Performing AD Automation table integrity scan ...')

        ddb_paginate = self.context.aws().dynamodb().get_paginator('scan')
        ddb_iter = ddb_paginate.paginate(
            TableName=self.ad_automation_dao.get_table_name()
        )
        _entrynum = 0
        _repaired_entries = 0
        _repair_failures = 0
        new_ttl = Utils.current_time()

        for _page in ddb_iter:
            for item in _page.get('Items', None):
                _instance_dict = item.get('instance_id', None)
                _ttl_dict = item.get('ttl', None)
                _nonce_dict = item.get('nonce', None)
                if _instance_dict and _ttl_dict and _nonce_dict:
                    _entrynum += 1
                    _instance_id = Utils.get_value_as_string('S', _instance_dict)
                    _ttl = Utils.get_value_as_int('N', _ttl_dict)
                    _nonce = Utils.get_value_as_string('S', _nonce_dict)
                    if _instance_id and _ttl:
                        _ttl_str = f'({Utils.unix_timestamp_string(_ttl)} - {Utils.humanize_unix_timestamp(_ttl)})'
                        self.logger.debug(
                            f'AD Automation table scan entry #{_entrynum} - Instance: {_instance_id}, TTL: {_ttl} {_ttl_str}'
                        )
                        if _ttl >= 2000000000:  # Tue May 17 22:33:20 CDT 2033
                            if self._update_ttl(
                                instance_id=_instance_id, nonce=_nonce, new_ttl=new_ttl
                            ):
                                self.logger.info(
                                    f'Automation DB entry TTL repaired: #{_entrynum} - Instance: {_instance_id}, TTL: {_ttl} -> {new_ttl}'
                                )
                                _repaired_entries += 1
                            else:
                                self.logger.error(
                                    f'Unable to repair TTL for entry #{_entrynum} - Instance: {_instance_id}, TTL: {_ttl}'
                                )
                                _repair_failures += 1

        # Summarize for the logs
        self.logger.info(
            f'AD Automation table scan summary: {_entrynum} entries scanned, {_repaired_entries} entries repaired, {_repair_failures} repair failures'
        )

    def _update_ttl(self, instance_id: str, nonce: str, new_ttl: int) -> bool:
        # Update the TTL in the dynamo DB table
        self.logger.debug(
            f'Updating DynamoDB TTL for instance_id/nonce: {instance_id}/{nonce} TO {new_ttl}'
        )
        try:
            result = (
                self.context.aws()
                .dynamodb()
                .update_item(
                    TableName=self.ad_automation_dao.get_table_name(),
                    Key={'instance_id': {'S': instance_id}, 'nonce': {'S': nonce}},
                    UpdateExpression='SET #ttl = :new_ttl',
                    ExpressionAttributeValues={':new_ttl': {'N': str(new_ttl)}},
                    ExpressionAttributeNames={'#ttl': 'ttl'},
                    ReturnValues='UPDATED_NEW',
                )
            )
            _res_status = result.get('ResponseMetadata', {}).get('HTTPStatusCode', None)
            if _res_status != 200:
                self.logger.warning(
                    f'TTL Update error {instance_id}/{nonce} - HTTPStatusCode({_res_status}) - {result}'
                )
                return False

            return True
        except Exception as e:
            self.logger.error(
                f'Failed to update TTL for instance_id/nonce: {instance_id}/{nonce} - {e}'
            )

    def _sanity_check(self):
        self.logger.info('Performing startup sanity check for AD Automation agent ...')
        self._ddb_sanity_check()

    def stop(self):
        self.logger.info('stopping ad automation agent ...')
        self._stop_event.set()
        self._automation_thread.join()
