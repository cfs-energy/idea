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

import ideascheduler
from ideadatamodel.scheduler import SocaJob, SocaComputeNode, SocaComputeNodeState, SocaQueue, SocaJobState

from ideascheduler.app.scheduler.openpbs import OpenPBSScheduler
from ideascheduler.app.app_protocols import SocaSchedulerProtocol

from typing import List, Optional, Dict, Any, Generator
import arrow


class SocaScheduler(SocaSchedulerProtocol):
    """
    SocaScheduler wrapper class over underlying scheduler implementation

    Additional instrumentation, metrics and caching will be implemented here and underlying implementations
    do not need to worry about these aspects.

    Scheduler implementation classes must conform to SocaSchedulerProtocol.
    See: ideasdk.scheduler.openpbs.OpenPBS for more details on scheduler implementation.

    Developer Notes:
        > Currently, we support only one scheduler, which is OpenPBS. We will add support for additional
            schedulers, and the hardcoding for OpenPBS will be removed then.
        > Scheduler selection must be based upon config: soca.scheduler.provider
    """

    def __init__(self, context: ideascheduler.AppContext):
        self._context = context
        self._logger = context.logger()
        self._scheduler = OpenPBSScheduler(context=context)

    def is_ready(self) -> bool:
        return self._scheduler.is_ready()

    def list_nodes(self) -> List[SocaComputeNode]:
        return self._scheduler.list_nodes()

    def create_node(self, node: SocaComputeNode) -> bool:
        return self._scheduler.create_node(node=node)

    def get_node(self, host: str, **kwargs) -> Optional[SocaComputeNode]:
        return self._scheduler.get_node(host=host, **kwargs)

    def delete_node(self, host: str) -> bool:
        return self._scheduler.delete_node(host=host)

    def set_node_state(self, host: str, state: SocaComputeNodeState):
        return self._scheduler.set_node_state(host=host, state=state)

    def set_node_attributes(self, host: str, attributes: Dict[str, Any]) -> bool:
        return self._scheduler.set_node_attributes(host=host, attributes=attributes)

    def create_queue(self, queue_name: str):
        self._scheduler.create_queue(queue_name=queue_name)

    def set_queue_attributes(self, queue_name: str, attributes: Dict[str, Any]):
        self._scheduler.set_queue_attributes(queue_name=queue_name, attributes=attributes)

    def list_queues(self) -> List[SocaQueue]:
        return self._scheduler.list_queues()

    def delete_queue(self, queue_name: str):
        self._scheduler.delete_queue(queue_name=queue_name)

    def get_queue(self, queue: str) -> Optional[SocaQueue]:
        return self._scheduler.get_queue(queue=queue)

    def is_queue_enabled(self, queue: str):
        return self._scheduler.is_queue_enabled(queue=queue)

    def list_jobs(self, queue: Optional[str] = None, job_ids: Optional[List[str]] = None,
                  owners: Optional[List[str]] = None, job_state: Optional[SocaJobState] = None,
                  queued_after: Optional[arrow.Arrow] = None,
                  max_jobs: int = -1,
                  stack_id: str = None) -> List[SocaJob]:
        return self._scheduler.list_jobs(queue=queue,
                                         job_ids=job_ids,
                                         owners=owners,
                                         job_state=job_state,
                                         queued_after=queued_after,
                                         max_jobs=max_jobs,
                                         stack_id=stack_id)

    def job_iterator(self, queue: Optional[str] = None, job_ids: Optional[List[str]] = None,
                     owners: Optional[List[str]] = None, job_state: Optional[SocaJobState] = None,
                     queued_after: Optional[arrow.Arrow] = None,
                     max_jobs: int = -1,
                     stack_id: str = None) -> Generator[SocaJob, None, None]:
        return self._scheduler.job_iterator(queue=queue,
                                            job_ids=job_ids,
                                            owners=owners,
                                            job_state=job_state,
                                            queued_after=queued_after,
                                            max_jobs=max_jobs,
                                            stack_id=stack_id)

    def get_job(self, job_id: str) -> Optional[SocaJob]:
        return self._scheduler.get_job(job_id=job_id)

    def delete_job(self, job_id: str):
        self._scheduler.delete_job(job_id)

    def is_job_active(self, job_id: str) -> bool:
        return self._scheduler.is_job_active(job_id=job_id)

    def get_finished_job(self, job_id: str) -> Optional[SocaJob]:
        return self._scheduler.get_finished_job(job_id=job_id)

    def is_job_queued_or_running(self, job_id: str) -> bool:
        return self._scheduler.is_job_queued_or_running(job_id=job_id)

    def set_job_attributes(self, job_id: str, attributes: Dict[str, Any]) -> bool:
        return self._scheduler.set_job_attributes(job_id=job_id, attributes=attributes)

    def provision_job(self, job: SocaJob,
                      stack_id: str) -> int:
        return self._scheduler.provision_job(
            job=job,
            stack_id=stack_id
        )

    def reset_job(self, job_id: str) -> bool:
        return self._scheduler.reset_job(job_id=job_id)
