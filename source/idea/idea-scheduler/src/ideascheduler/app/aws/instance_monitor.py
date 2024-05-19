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

from ideasdk.protocols import SocaContextProtocol
from ideasdk.logging import ConsoleLogger
from ideasdk.service import SocaService
from ideasdk.pubsub import SocaPubSub
from ideadatamodel import constants, EC2InstanceMonitorEvent
from ideasdk.utils import Utils

from ideascheduler.app.aws import InstanceCache

from typing import Optional
from threading import Thread, Event

console = ConsoleLogger('instance_monitor')

INSTANCE_MONITOR_INTERVAL_SECS = 30
PAGE_SIZE = 20


class InstanceMonitor(SocaService):
    def __init__(self, context: SocaContextProtocol, instance_cache: InstanceCache):
        super().__init__(context)
        self._logger = context.logger('instance-monitor')
        self._context = context
        self._instance_cache = instance_cache
        self._instance_monitor_thread: Optional[Thread] = None
        self._topic: Optional[SocaPubSub] = None
        self._exit: Optional[Event] = None
        self._is_running = False

    def _initialize(self):
        self._instance_monitor_thread = Thread(
            name='instance-monitor',
            target=self._monitor_instances
        )
        self._topic = SocaPubSub(constants.TOPIC_EC2_INSTANCE_MONITOR_EVENTS)
        self._exit = Event()

    def _monitor_instances(self):
        while not self._exit.is_set():
            session_key = Utils.uuid()
            try:
                self._instance_cache.sync_begin(session_key=session_key)
                # get all instances for the cluster - scheduler, compute, dcv, and anything else ...
                self._context.aws_util().ec2_describe_instances(
                    page_size=PAGE_SIZE,
                    paging_callback=lambda instances: self._instance_cache.sync(
                        instances=instances
                    )
                )
                self._instance_cache.sync_commit(session_key=session_key)
                self._topic.publish(
                    sender='instance-monitor',
                    message=EC2InstanceMonitorEvent(
                        type=constants.EC2_INSTANCE_MONITOR_EVENT_CACHE_REFRESH
                    )
                )
            except Exception as e:
                self._logger.exception(f'ec2 instance monitor iteration failed', exc_info=e)
                self._instance_cache.sync_abort(session_key=session_key)
            finally:
                self._exit.wait(INSTANCE_MONITOR_INTERVAL_SECS)

    def start(self):
        if self._is_running:
            return
        self._initialize()
        self._is_running = True
        self._instance_monitor_thread.start()

    def stop(self):
        if not self._is_running:
            return
        self._exit.set()
        self._is_running = False
        self._instance_monitor_thread.join()
