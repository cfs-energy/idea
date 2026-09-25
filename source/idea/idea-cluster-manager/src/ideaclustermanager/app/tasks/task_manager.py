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
import os
import faulthandler
from datetime import datetime, timezone

from botocore.exceptions import ClientError
from concurrent.futures import ThreadPoolExecutor

from ideasdk.context import SocaContext
from ideasdk.service import SocaService
from ideasdk.utils import Utils
from ideadatamodel import constants
from ideaclustermanager.app.tasks.base_task import BaseTask

from typing import Dict, List
import threading
import ldap
import time

DEFAULT_MAX_WORKERS = 5  # The default setting if the configuration cannot be found
MAX_WORKERS = 10  # The absolute maximum

# WaitTime
#  https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-short-and-long-polling.html
DEFAULT_WAIT_TIME = 20  # WaitTime for SQS polling (short or long polling)
MAX_WAIT_TIME = 20  # The absolute maximum

# Max Visibility from SQS in one API call
DEFAULT_VISIBILITY = constants.SQS_VISIBILITY_TASKS
MAX_VISIBILITY = 12 * 60 * 60  # The absolute maximum (12 hours, SQS limit)


class TaskManager(SocaService):
    def __init__(self, context: SocaContext, tasks: List[BaseTask]):
        super().__init__(context)

        self.context = context
        self.logger = context.logger('task-manager')

        self.tasks: Dict[str, BaseTask] = {}
        for task in tasks:
            self.tasks[task.get_name()] = task

        self.exit = threading.Event()
        self._debug = self.context.config().get_bool(
            'cluster-manager.task_manager.debug', default=False
        )

        self.task_monitor_thread = threading.Thread(
            target=self.task_queue_listener, name='task-monitor'
        )
        self._wait_time = self.context.config().get_int(
            'cluster-manager.task_manager.sqs_wait_time', default=DEFAULT_WAIT_TIME
        )

        if self._wait_time < 0:
            self.logger.warning(
                f'SQS WaitTime (cluster-manager.task_manager.sqs_wait_time) cannot be negative. Setting to {DEFAULT_WAIT_TIME}'
            )
            self._wait_time = DEFAULT_WAIT_TIME

        if self._wait_time > MAX_WAIT_TIME:
            self.logger.warning(
                f'SQS WaitTime (cluster-manager.task_manager.sqs_wait_time) cannot be greater than {MAX_WAIT_TIME}. Setting to {MAX_WAIT_TIME}'
            )
            self._wait_time = MAX_WAIT_TIME

        self._max_workers = self.context.config().get_int(
            'cluster-manager.task_manager.max_workers', default=DEFAULT_MAX_WORKERS
        )

        self._polling_visibility_timeout = self.context.config().get_int(
            'cluster-manager.task_manager.polling_visibility_timeout',
            default=DEFAULT_VISIBILITY,
        )

        if self._max_workers <= 0:
            self.logger.warning(
                f'Maximum task workers cannot be 0 or negative. Setting maximum workers to {DEFAULT_MAX_WORKERS}'
            )
            self._max_workers = DEFAULT_MAX_WORKERS

        if self._polling_visibility_timeout <= 0:
            self.logger.warning(
                f'Polling visibility cannot be 0 or negative. Setting maximum messages to {DEFAULT_VISIBILITY}'
            )
            self._polling_visibility_timeout = DEFAULT_VISIBILITY

        if self._max_workers > MAX_WORKERS:
            # If we are set for debug mode - allow exceeding the suggested limits
            if self._debug:
                self.logger.info(
                    f'Allowing maximum workers of {self._max_workers} to exceed {MAX_WORKERS} due to debug mode being set'
                )
            else:
                self.logger.warning(
                    f'Maximum task workers exceeds suggested maximum of {MAX_WORKERS}. Setting maximum workers to {MAX_WORKERS}. Set debug mode (cluster-manager.task_manager.debug) to remove this safeguard'
                )
                self._max_workers = MAX_WORKERS

        if self._polling_visibility_timeout > MAX_VISIBILITY:
            self.logger.warning(
                f'Polling Visibility cannot be > {MAX_VISIBILITY}. Setting visibility to {MAX_VISIBILITY}'
            )
            self._polling_visibility_timeout = MAX_VISIBILITY

        self._task_timeout = max(
            1,
            min(
                self.context.config().get_int(
                    'cluster-manager.task_manager.task_timeout_seconds', default=120
                ),
                MAX_VISIBILITY - 30,
            ),
        )
        # Stop the process before SQS can redeliver a task still making side effects.
        self._polling_visibility_timeout = max(
            self._polling_visibility_timeout, self._task_timeout + 30
        )
        self._slots = threading.BoundedSemaphore(self._max_workers)
        self._full_since = None
        self._full_warned = False
        self.task_executors = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix='task-executor'
        )

    def _task_expired(self, task, payload, receive_count):
        # Python cannot safely cancel a running thread. The supervisor must replace the
        # process; leaving the receipt unacknowledged preserves FIFO retry ordering.
        try:
            os.write(
                2, b'Task deadline exceeded; terminating worker process for restart\n'
            )
            faulthandler.dump_traceback(all_threads=True)
            if (
                task is not None
                and receive_count >= constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS
            ):
                # Bound the write even if credentials or the database client hang.
                writer = threading.Thread(
                    target=self._update_task_failure,
                    args=(task, payload, TimeoutError('Task deadline exceeded')),
                    daemon=True,
                )
                writer.start()
                writer.join(timeout=2)
        finally:
            os._exit(1)

    def _execute_with_deadline(self, message):
        try:
            body = Utils.from_json(message.get('Body', ''))
            task = self.tasks.get(body.get('name'))
            payload = body.get('payload')
            receive_count = int(
                message.get('Attributes', {}).get('ApproximateReceiveCount', '1')
            )
        except (ValueError, TypeError, AttributeError):
            task, payload, receive_count = None, None, 1
        timer = threading.Timer(
            self._task_timeout, self._task_expired, args=(task, payload, receive_count)
        )
        timer.daemon = True
        timer.start()
        try:
            self.execute_task(message)
        finally:
            timer.cancel()
            self._slots.release()

    def _reserve_slot(self):
        if self._slots.acquire(blocking=False):
            self._full_since = None
            self._full_warned = False
            return True
        now = time.monotonic()
        if self._full_since is None:
            self._full_since = now
        elif not self._full_warned and now - self._full_since > self._task_timeout:
            self.logger.warning(
                'Task executor pool has been full longer than the task timeout'
            )
            self._full_warned = True
        return False

    def _discard_deleted_user(self, task_name, payload, receipt_handle):
        if task_name not in {
            'accounts.sync-user',
            'accounts.sync-password',
            'accounts.create-home-directory',
            'accounts.group-membership-updated',
        }:
            return False
        if not isinstance(payload, dict):
            return False
        username = payload.get('username')
        if (
            not username
            or self.context.accounts.user_dao.get_user(username) is not None
        ):
            return False
        # LDAP absence alone can mean replication lag; only the authoritative
        # account record establishes that retrying this work is obsolete.
        self.logger.warning(f'failed task for deleted user; discarding: {task_name}')
        self.context.aws().sqs().delete_message(
            QueueUrl=self.get_task_queue_url(), ReceiptHandle=receipt_handle
        )
        return True

    def get_task_queue_url(self) -> str:
        return self.context.config().get_string(
            'cluster-manager.task_queue_url', required=True
        )

    def _update_task_failure(self, task, payload, error=None):
        try:
            entity = task.entity_ref(payload)
            if entity is None:
                return
            kind, entity_id = entity
            if kind == 'user':
                table = self.context.accounts.user_dao.table
                key = 'username'
            elif kind == 'group':
                table = self.context.accounts.group_dao.table
                key = 'group_name'
            elif kind == 'project':
                table = self.context.projects.projects_dao.table
                key = 'project_id'
            else:
                return

            names = {'#key': key, '#failure': 'last_task_failure'}
            if error is not None:
                table.update_item(
                    Key={key: entity_id},
                    UpdateExpression='SET #failure = :failure',
                    ConditionExpression='attribute_exists(#key)',
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues={
                        ':failure': {
                            'task': task.get_name(),
                            'message': str(error)[:200],
                            'at': datetime.now(timezone.utc).isoformat(),
                        }
                    },
                )
            else:
                # Only the task that failed can clear its marker.
                table.update_item(
                    Key={key: entity_id},
                    UpdateExpression='REMOVE #failure',
                    ConditionExpression='attribute_exists(#key) AND #failure.#task = :task',
                    ExpressionAttributeNames={**names, '#task': 'task'},
                    ExpressionAttributeValues={':task': task.get_name()},
                )
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                self.logger.exception('failed to update task failure marker')
        except Exception:
            self.logger.exception('failed to update task failure marker')

    def execute_task(self, sqs_message: Dict):
        task_name = None
        task_payload = None
        receipt_handle = None
        task = None
        task_succeeded = False
        try:
            _task_start = Utils.current_time_ms()
            message_body = Utils.get_value_as_string('Body', sqs_message)
            receipt_handle = Utils.get_value_as_string('ReceiptHandle', sqs_message)
            task_message = Utils.from_json(message_body)

            task_name = task_message.get('name', None)
            task_payload = task_message.get('payload', None)

            if Utils.is_any_empty(task_name, task_payload):
                self.logger.error(
                    f'Invalid task - Body: {message_body} Handle: {receipt_handle} - {Utils.to_json(task_message)}'
                )
                return

            self.logger.info(
                f'executing task: {task_name} ({receipt_handle}), payload: {Utils.to_json(task_payload)}'
            )

            if task_name not in self.tasks:
                self.logger.warning(f'no task registered for task name: {task_name}')
                return

            task = self.tasks[task_name]

            if self._discard_deleted_user(task_name, task_payload, receipt_handle):
                return
            task.invoke(task_payload)
            task_succeeded = True
            self._update_task_failure(task, task_payload)
            _task_end = Utils.current_time_ms()
            _task_duration = int(_task_end - _task_start)
            self.logger.info(
                f'completed task: {task_name} ({receipt_handle}), duration: {_task_duration}ms'
            )

            self.logger.debug(f'Attempting to delete task handle ({receipt_handle})')
            self.context.aws().sqs().delete_message(
                QueueUrl=self.get_task_queue_url(), ReceiptHandle=receipt_handle
            )

        except ldap.ALREADY_EXISTS as e:
            self.logger.warning(
                f'failed to execute task due to LDAP exists error: {task_name} - {task_payload} - {e}'
            )
            if task is not None:
                self._update_task_failure(task, task_payload, e)
            # Still remove the task from the queue in this case
            self.context.aws().sqs().delete_message(
                QueueUrl=self.get_task_queue_url(), ReceiptHandle=receipt_handle
            )

        except Exception as e:
            if self._discard_deleted_user(task_name, task_payload, receipt_handle):
                return
            self.logger.exception(f'failed to execute task: {task_name} - {e}')
            receive_count = int(
                sqs_message.get('Attributes', {}).get('ApproximateReceiveCount', '1')
            )
            if (
                task is not None
                and not task_succeeded
                and receive_count >= constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS
            ):
                self._update_task_failure(task, task_payload, e)

    def task_queue_listener(self):
        while not self.exit.is_set():
            if not self._reserve_slot():
                # Do not receive and release work at capacity: each receive counts
                # towards the DLQ limit even though no task has attempted it.
                self.exit.wait(1)
                continue
            submitted = False
            try:
                result = (
                    self.context.aws()
                    .sqs()
                    .receive_message(
                        QueueUrl=self.get_task_queue_url(),
                        # A FIFO batch can contain multiple messages in the same group.
                        # Receiving one prevents concurrent execution within that group.
                        MaxNumberOfMessages=1,
                        AttributeNames=['All'],
                        VisibilityTimeout=self._polling_visibility_timeout,
                        WaitTimeSeconds=self._wait_time,
                    )
                )
                messages = Utils.get_value_as_list('Messages', result, default=[])
                if messages:
                    self.task_executors.submit(self._execute_with_deadline, messages[0])
                    submitted = True
            except Exception as e:
                self.logger.exception(f'failed to poll queue: {e}')
                self.exit.wait(1)
            finally:
                if not submitted:
                    self._slots.release()

    def send(
        self,
        task_name: str,
        payload: Dict,
        message_group_id: str = None,
        message_dedupe_id: str = None,
    ):
        task_message = {'name': task_name, 'payload': payload}

        if Utils.is_empty(message_group_id):
            message_group_id = task_name
        if Utils.is_empty(message_dedupe_id):
            message_dedupe_id = Utils.sha256(Utils.to_json(task_message))

        self.logger.debug(
            f'send task: {task_name}, message group id: {message_group_id}, DedupeId: {message_dedupe_id}'
        )
        self.context.aws().sqs().send_message(
            QueueUrl=self.get_task_queue_url(),
            MessageBody=Utils.to_json(task_message),
            MessageDeduplicationId=message_dedupe_id,
            MessageGroupId=message_group_id,
        )

    def start(self):
        self.task_monitor_thread.start()

    def stop(self):
        self.exit.set()
        if self.task_monitor_thread.is_alive():
            self.task_monitor_thread.join()
        self.task_executors.shutdown(wait=True)
