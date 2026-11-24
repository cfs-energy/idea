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

from ideasdk.context import SocaContext
from ideasdk.service import SocaService
from ideasdk.utils import Utils
from ideadatamodel import (
    GetEmailTemplateRequest,
    Notification,
    constants,
    errorcodes,
    exceptions,
)

from ideaclustermanager.app.accounts.accounts_service import AccountsService
from ideaclustermanager.app.email_templates.email_templates_service import (
    EmailTemplatesService,
    render_email,
)

from typing import Dict, Optional
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re
import threading
import time

MAX_WORKERS = 1  # must be between 1 and 10
# scratch cleanup counts a notice as given only once SES has taken its digest
DELIVERY_RECEIPT_TEMPLATES = 'scheduler.scratch-cleanup-'
# the templates whose sender waits for a receipt before deleting anything, and the
# module table the receipt goes to: the cleanup goes ahead only after the warning went
RECEIPT_TABLES = {
    DELIVERY_RECEIPT_TEMPLATES: (constants.MODULE_SCHEDULER, 'scratch-cleanup'),
}
# the desktop controller's cleanup warning, under the template its settings name; the
# controller owns <cluster>.<its module id>.desktop-cleanup and reads pk 'delivered'
DESKTOP_CLEANUP_TABLE = (constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER, 'desktop-cleanup')
# a desktop's notice is looked up when its warning window ends, which an
# administrator may set past 30 days: its receipt is kept longer
DESKTOP_RECEIPT_SECONDS = 400 * 24 * 3600
DESKTOP_CLEANUP_TEMPLATE_KEY = (
    'dcv_session.notifications.cleanup_warning.email_template'
)
DESKTOP_CLEANUP_TEMPLATE_DEFAULT = 'virtual-desktop-controller.session-cleanup-warning'
DELIVERY_RECEIPT_SECONDS = 30 * 24 * 3600


class NotificationsService(SocaService):
    def __init__(
        self,
        context: SocaContext,
        accounts: AccountsService,
        email_templates: EmailTemplatesService,
    ):
        super().__init__(context)

        self.context = context
        self.logger = context.logger('notifications')

        self.accounts = accounts
        self.email_templates = email_templates

        self.exit = threading.Event()
        self.notifications_queue_url = self.context.config().get_string(
            'cluster-manager.notifications_queue_url', required=True
        )

        self.notifications_monitor_thread = threading.Thread(
            target=self.notifications_queue_listener, name='notifications-monitor'
        )
        self.notifications_executors = ThreadPoolExecutor(
            max_workers=MAX_WORKERS, thread_name_prefix='notifications-executor'
        )

    def send_email(self, notification: Notification):
        """
        send email

        payload must contain below fields:

            username: str
            template_name: str - the name of the email template
            params: dict - containing all parameters required to render the template

        """
        try:
            ses_enabled = self.context.config().get_bool('cluster.ses.enabled', False)
            if not ses_enabled:
                self.logger.debug('ses is disabled. skip.')
                self._cannot_notify(notification, 'email-off')
                return

            sender_email = self.context.config().get_string(
                'cluster.ses.sender_email', required=True
            )
            max_sending_rate = self.context.config().get_int(
                'cluster.ses.max_sending_rate', 1
            )
            max_sending_rate = max(1, max_sending_rate)

            email_notifications_enabled = self.context.config().get_bool(
                'cluster-manager.notifications.email.enabled', False
            )
            if not email_notifications_enabled:
                self.logger.debug('email notifications are disabled. skip.')
                self._cannot_notify(notification, 'notifications-off')
                return

            ses_region = self.context.config().get_string(
                'cluster.ses.region', required=True
            )

            ses = self.context.aws().ses(region_name=ses_region)

            username = notification.username
            try:
                user = self.accounts.get_user(username=username)
            except exceptions.SocaException as error:
                if error.error_code != errorcodes.AUTH_USER_NOT_FOUND:
                    raise
                self.logger.info(f'user: {username} not found. skip email notification')
                self._cannot_notify(notification, 'owner-not-found')
                return
            if Utils.is_false(user.enabled):
                self.logger.info(
                    f'user: {username} has been disabled. skip email notification'
                )
                self._cannot_notify(notification, 'owner-disabled')
                return

            email = user.email
            if Utils.is_empty(email):
                self.logger.warning(
                    f'email address not found for user: {username}. skip email notification'
                )
                self._cannot_notify(notification, 'no-address')
                return

            template_name = notification.template_name
            get_template_result = self.email_templates.get_email_template(
                GetEmailTemplateRequest(name=template_name)
            )
            template = get_template_result.template
            subject_template = template.subject
            body_template = template.body

            params = Utils.get_as_dict(notification.params, {})

            # a template the sandbox refuses fails this one email, logged below
            subject, body = render_email(subject_template, body_template, params)

            ses.send_email(
                Source=sender_email,
                Destination={'ToAddresses': [email]},
                Message={
                    'Subject': {'Data': subject},
                    'Body': {'Html': {'Data': body}},
                },
            )
            self.record_delivery(template_name, params, outcome='sent')

            delay = float(1 / max_sending_rate)
            time.sleep(delay)

        except Exception as e:
            self.logger.exception(f'failed to send email notification: {e}')

    def _desktop_cleanup_template(self) -> str:
        try:
            config = self.context.config()
            module_id = config.get_module_id(
                constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER
            )
            return config.get_string(
                f'{module_id}.{DESKTOP_CLEANUP_TEMPLATE_KEY}',
                default=DESKTOP_CLEANUP_TEMPLATE_DEFAULT,
            )
        except Exception:
            # no desktop module: no desktop receipts to record
            return None

    def _cannot_notify(self, notification: Notification, reason: str):
        """
        a desktop cleanup warning that can never be sent for this reason is recorded
        as such, so the cleanup goes ahead once its window has passed; a failure that
        may pass (the mail service refusing or timing out) records nothing
        """
        template_name = notification.template_name
        if template_name != self._desktop_cleanup_template():
            return
        self.record_delivery(
            template_name,
            Utils.get_as_dict(notification.params, {}),
            outcome='cannot-notify',
            reason=reason,
        )

    def record_delivery(
        self,
        template_name: str,
        params: Dict,
        outcome: str = 'sent',
        reason: Optional[str] = None,
    ):
        receipt = params.get('delivery_receipt')
        # the desktop template by its exact name first, whatever prefix it was given
        if template_name == self._desktop_cleanup_template():
            table = DESKTOP_CLEANUP_TABLE
        else:
            table = next(
                (
                    target
                    for prefix, target in RECEIPT_TABLES.items()
                    if str(template_name).startswith(prefix)
                ),
                None,
            )
        if not (
            table is not None
            and isinstance(receipt, str)
            and re.fullmatch(r'[0-9a-f]{32}', receipt)
        ):
            return
        try:
            module, name = table
            module_id = self.context.config().get_module_id(module)
            self.context.aws().dynamodb_table().Table(
                f'{self.context.cluster_name()}.{module_id}.{name}'
            ).put_item(
                Item={
                    'pk': 'delivered',
                    'sk': receipt,
                    'at': datetime.now(timezone.utc).isoformat(),
                    # sent, or cannot-notify with the reason it never can be
                    'outcome': outcome,
                    **({'reason': reason} if reason else {}),
                    'expires_at': int(time.time())
                    + (
                        DESKTOP_RECEIPT_SECONDS
                        if table == DESKTOP_CLEANUP_TABLE
                        else DELIVERY_RECEIPT_SECONDS
                    ),
                }
            )
        except Exception as e:
            # the email went; the cleanup treats it as not sent and waits
            self.logger.exception(f'could not record delivery of {template_name}: {e}')

    def execute_notifications(self, sqs_message: Dict):
        notifications_name = None
        try:
            message_body = Utils.get_value_as_string('Body', sqs_message)
            receipt_handle = Utils.get_value_as_string('ReceiptHandle', sqs_message)
            payload = Utils.from_json(message_body)
            self.send_email(Notification(**payload))
            self.context.aws().sqs().delete_message(
                QueueUrl=self.notifications_queue_url, ReceiptHandle=receipt_handle
            )
        except Exception as e:
            self.logger.exception(
                f'failed to execute notifications: {notifications_name} - {e}'
            )

    def notifications_queue_listener(self):
        while not self.exit.is_set():
            try:
                result = (
                    self.context.aws()
                    .sqs()
                    .receive_message(
                        QueueUrl=self.notifications_queue_url,
                        MaxNumberOfMessages=MAX_WORKERS,
                        WaitTimeSeconds=20,
                    )
                )
                messages = Utils.get_value_as_list('Messages', result, [])
                if len(messages) == 0:
                    continue
                self.logger.info(f'received {len(messages)} messages')
                for message in messages:
                    self.notifications_executors.submit(
                        lambda message_: self.execute_notifications(message_), message
                    )

            except Exception as e:
                self.logger.exception(f'failed to poll queue: {e}')

    def start(self):
        self.notifications_monitor_thread.start()

    def stop(self):
        self.exit.set()
        if self.notifications_monitor_thread.is_alive():
            self.notifications_monitor_thread.join()
        self.notifications_executors.shutdown(wait=True)
