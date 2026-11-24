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
from ideasdk.client import NotificationsAsyncClient
from ideasdk.utils import Utils
from ideadatamodel import SocaJob, Notification

from ideascheduler.app.app_protocols import JobNotificationsProtocol


class JobNotifications(JobNotificationsProtocol):
    def __init__(
        self, context: SocaContext, notifications_client: NotificationsAsyncClient
    ):
        self.context = context
        self.notifications_client = notifications_client

    @staticmethod
    def _params(job: SocaJob) -> dict:
        # the message is one SQS body. An array's record lists every subjob, and a wide
        # array's list alone is larger than a message may be, so it stays on the record.
        return Utils.to_dict(job.model_copy(update={'subjobs': None}))

    def job_started(self, job: SocaJob):
        if job.notifications is None:
            return
        if not Utils.is_true(job.notifications.started, False):
            return

        template_name = job.notifications.job_started_email_template
        if Utils.is_empty(template_name):
            template_name = self.context.config().get_string(
                'scheduler.notifications.job_started.email_template', required=True
            )

        if Utils.is_empty(template_name):
            return

        self.notifications_client.send_notification(
            notification=Notification(
                username=job.owner,
                template_name=template_name,
                params={'job': self._params(job)},
            )
        )

    def job_completed(self, job: SocaJob):
        if job.notifications is None:
            return
        if not Utils.is_true(job.notifications.completed, False):
            return

        template_name = job.notifications.job_completed_email_template
        if Utils.is_empty(template_name):
            template_name = self.context.config().get_string(
                'scheduler.notifications.job_completed.email_template', required=True
            )

        if Utils.is_empty(template_name):
            return

        self.notifications_client.send_notification(
            notification=Notification(
                username=job.owner,
                template_name=template_name,
                params={'job': self._params(job)},
            )
        )
