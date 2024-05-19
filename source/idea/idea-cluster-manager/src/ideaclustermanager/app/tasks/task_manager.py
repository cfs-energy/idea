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
import logging

from ideasdk.context import SocaContext
from ideasdk.service import SocaService
from ideasdk.utils import Utils
from ideadatamodel import (
    constants
)
from ideaclustermanager.app.tasks.base_task import BaseTask

from typing import Dict, List
from ideasdk.thread_pool import ThreadPoolExecutor, ThreadPoolMaxCapacity
import threading
import ldap
import time

DEFAULT_MIN_WORKERS = 1 # The default setting if the configuration cannot be found
DEFAULT_MAX_WORKERS = 5 # The default setting if the configuration cannot be found
MAX_WORKERS = 10        # The absolute maximum

# WaitTime
#  https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-short-and-long-polling.html
DEFAULT_WAIT_TIME = 20 # WaitTime for SQS polling (short or long polling)
MAX_WAIT_TIME = 20     # The absolute maximum

# MaxMessages from SQS in one API call
# NOTE - Due to message processing time and dependency - the suggested value is 1
DEFAULT_MAX_MESSAGES = 1 # WaitTime for SQS polling (short or long polling)
MAX_MESSAGES = 10     # The absolute maximum

# Max Visibility from SQS in one API call
DEFAULT_VISIBILITY = constants.SQS_VISIBILITY_TASKS
MAX_VISIBILITY = (12 * 60 * 60) # The absolute maximum (12 hours, SQS limit)

class TaskManager(SocaService):

    def __init__(self, context: SocaContext, tasks: List[BaseTask]):
        super().__init__(context)

        self.context = context
        self.logger = context.logger('task-manager')

        self.tasks: Dict[str, BaseTask] = {}
        for task in tasks:
            self.tasks[task.get_name()] = task

        self.exit = threading.Event()
        self._debug = self.context.config().get_bool('cluster-manager.task_manager.debug', default=False)

        self.task_monitor_thread = threading.Thread(
            target=self.task_queue_listener,
            name='task-monitor'
        )
        self._wait_time = self.context.config().get_int('cluster-manager.task_manager.sqs_wait_time', default=DEFAULT_WAIT_TIME)

        if self._wait_time < 0:
            self.logger.warning(f"SQS WaitTime (cluster-manager.task_manager.sqs_wait_time) cannot be negative. Setting to {DEFAULT_WAIT_TIME}")
            self._wait_time = DEFAULT_WAIT_TIME

        if self._wait_time > MAX_WAIT_TIME:
            self.logger.warning(f"SQS WaitTime (cluster-manager.task_manager.sqs_wait_time) cannot be greater than {MAX_WAIT_TIME}. Setting to {MAX_WAIT_TIME}")
            self._wait_time = MAX_WAIT_TIME

        self._min_workers = self.context.config().get_int('cluster-manager.task_manager.min_workers', default=DEFAULT_MIN_WORKERS)
        self._max_workers = self.context.config().get_int('cluster-manager.task_manager.max_workers', default=DEFAULT_MAX_WORKERS)

        self._polling_max_messages = self.context.config().get_int('cluster-manager.task_manager.polling_max_messages', default=DEFAULT_MAX_MESSAGES)
        self._polling_visibility_timeout = self.context.config().get_int('cluster-manager.task_manager.polling_visibility_timeout', default=DEFAULT_VISIBILITY)

        if self._min_workers <= 0:
            self.logger.warning(f"Minimum task workers cannot be 0 or negative. Setting minimum workers to {DEFAULT_MIN_WORKERS}")
            self._min_workers = DEFAULT_MIN_WORKERS

        if self._max_workers <= 0:
            self.logger.warning(f"Maximum task workers cannot be 0 or negative. Setting maximum workers to {DEFAULT_MAX_WORKERS}")
            self._max_workers = DEFAULT_MAX_WORKERS

        if self._polling_max_messages <= 0:
            self.logger.warning(f"Maximum messages cannot be 0 or negative. Setting maximum messages to {DEFAULT_MAX_MESSAGES}")
            self._polling_max_messages = DEFAULT_MAX_MESSAGES

        if self._polling_visibility_timeout <= 0:
            self.logger.warning(f"Polling visibility cannot be 0 or negative. Setting maximum messages to {DEFAULT_VISIBILITY}")
            self._polling_visibility_timeout = DEFAULT_VISIBILITY

        if self._max_workers > MAX_WORKERS:
            # If we are set for debug mode - allow exceeding the suggested limits
            if self._debug:
                self.logger.info(f"Allowing maximum workers of {self._max_workers} to exceed {MAX_WORKERS} due to debug mode being set")
            else:
                self.logger.warning(f"Maximum task workers exceeds suggested maximum of {MAX_WORKERS}. Setting maximum workers to {MAX_WORKERS}. Set debug mode (cluster-manager.task_manager.debug) to remove this safeguard")
                _max_workers = MAX_WORKERS

        if self._min_workers > self._max_workers:
            self.logger.warning(f"Minimum task workers > Maximum workers ({self._min_workers}) < ({self._max_workers}). Setting minimum workers to maximum workers ({self._max_workers})")
            self._min_workers = self._max_workers

        if self._polling_max_messages > MAX_MESSAGES:
            self.logger.warning(f"Maximum messages cannot be > {MAX_MESSAGES}. Setting maximum messages to {MAX_MESSAGES}")
            self._polling_max_messages = MAX_MESSAGES

        if self._polling_visibility_timeout > MAX_VISIBILITY:
            self.logger.warning(f"Polling Visibility cannot be > {MAX_VISIBILITY}. Setting visibility to {MAX_VISIBILITY}")
            self._polling_max_visibility = MAX_VISIBILITY

        self.task_executors = ThreadPoolExecutor(
            context=context,
            min_workers=self._min_workers,
            max_workers=self._max_workers,
            thread_pool_name='task-executor-pool',
            debug=self._debug,
        )

    def get_task_queue_url(self) -> str:
        return self.context.config().get_string('cluster-manager.task_queue_url', required=True)

    def execute_task(self, sqs_message: Dict):
        task_name = None
        try:
            _task_start = Utils.current_time_ms()
            message_body = Utils.get_value_as_string('Body', sqs_message)
            receipt_handle = Utils.get_value_as_string('ReceiptHandle', sqs_message)
            task_message = Utils.from_json(message_body)

            task_name = task_message.get('name', None)
            task_payload = task_message.get('payload', None)

            if Utils.is_any_empty(task_name, task_payload):
                self.logger.error(f'Invalid task - Body: {message_body} Handle: {receipt_handle} - {Utils.to_json(task_message)}')
                return

            self.logger.info(f'executing task: {task_name} ({receipt_handle}), payload: {Utils.to_json(task_payload)}')

            if task_name not in self.tasks:
                self.logger.warning(f'no task registered for task name: {task_name}')
                return

            task = self.tasks[task_name]

            task.invoke(task_payload)
            _task_end = Utils.current_time_ms()
            _task_duration = int(_task_end - _task_start)
            self.logger.info(f'completed task: {task_name} ({receipt_handle}), duration: {_task_duration}ms')

            self.logger.debug(f'Attempting to delete task handle ({receipt_handle})')
            self.context.aws().sqs().delete_message(
                QueueUrl=self.get_task_queue_url(),
                ReceiptHandle=receipt_handle
            )

        except ldap.ALREADY_EXISTS as e:
            self.logger.warning(f'failed to execute task due to LDAP exists error: {task_name} - {task_payload} - {e}')
            # Still remove the task from the queue in this case
            self.context.aws().sqs().delete_message(
                QueueUrl=self.get_task_queue_url(),
                ReceiptHandle=receipt_handle
            )

        except Exception as e:
            self.logger.exception(f'failed to execute task: {task_name} - {e}')

    def task_queue_listener(self):

        while not self.exit.is_set():
            try:
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug(f"Polling SQS with max messages: {self._polling_max_messages}, visibility timeout: {self._polling_visibility_timeout}, wait time: {self._wait_time}")
                result = self.context.aws().sqs().receive_message(
                    QueueUrl=self.get_task_queue_url(),
                    MaxNumberOfMessages=self._polling_max_messages,
                    AttributeNames=['All'],
                    VisibilityTimeout=self._polling_visibility_timeout,
                    WaitTimeSeconds=self._wait_time
                )
                messages: List[Dict] = Utils.get_value_as_list('Messages', result, default=[])
                if len(messages) == 0:
                    continue
                self.logger.info(f'received {len(messages)} messages from SQS')
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug(f"messages: {messages}")

                last_message_index = 0
                try:
                    for i, message in enumerate(messages):
                        last_message_index = i
                        self.logger.debug(f"Sending {i} to task_executor {message}")
                        message.setdefault('VisibilityTimeout', constants.SQS_VISIBILITY_TIMEOUT_DEFAULT)
                        self.task_executors.submit(lambda message_: self.execute_task(message_), message)

                except ThreadPoolMaxCapacity:
                    self.logger.debug(f"ThreadPoolMaxCapacity encountered - releasing messages")
                    # change the visibility timeout so that message can be processed by another server in ASG.
                    messages_not_processed = []
                    for i in range(last_message_index, len(messages)):
                        message = messages[i]
                        receipt_handle = Utils.get_value_as_string('ReceiptHandle', message)
                        messages_not_processed.append({
                            'Id': Utils.uuid(),
                            'ReceiptHandle': receipt_handle,
                            'VisibilityTimeout': 0
                        })

                    # 10 is the max size of entries that can be posted in batch to change_message_visibility_batch
                    for _batch_num in range(0, len(messages_not_processed), 10):
                        batch = messages_not_processed[_batch_num:_batch_num + 10]
                        self.logger.debug(f"Updating visibility timeout to 0 for: {batch}")
                        try:
                            self.context.aws().sqs().change_message_visibility_batch(
                                QueueUrl=self.get_task_queue_url(),
                                Entries=batch
                            )
                        except Exception as e:
                            self.logger.error(f'failed to update visibility timeout: {e}')
                    # wait for few seconds to poll the SQS queue
                    self.logger.debug(f"Waiting for 10 seconds to poll the SQS queue after ThreadPoolMaxCapacity")
                    self.exit.wait(10)

            except Exception as e:
                self.logger.exception(f'failed to poll queue: {e}')
                time.sleep(1)

    def send(self, task_name: str, payload: Dict, message_group_id: str = None, message_dedupe_id: str = None):
        task_message = {
            'name': task_name,
            'payload': payload
        }

        if Utils.is_empty(message_group_id):
            message_group_id = task_name
        if Utils.is_empty(message_dedupe_id):
            message_dedupe_id = Utils.sha256(Utils.to_json(task_message))

        self.logger.debug(f'send task: {task_name}, message group id: {message_group_id}, DedupeId: {message_dedupe_id}')
        self.context.aws().sqs().send_message(
            QueueUrl=self.get_task_queue_url(),
            MessageBody=Utils.to_json(task_message),
            MessageDeduplicationId=message_dedupe_id,
            MessageGroupId=message_group_id
        )

    def start(self):
        self.task_monitor_thread.start()
        self.task_executors.start()

    def stop(self):
        self.exit.set()
        if self.task_monitor_thread.is_alive():
            self.task_monitor_thread.join()
        self.task_executors.stop(wait=True)
