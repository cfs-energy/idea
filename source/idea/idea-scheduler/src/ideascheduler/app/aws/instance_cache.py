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

from ideadatamodel import EC2Instance, constants
from ideasdk.utils import Utils

import ideascheduler
from ideascheduler.app.app_protocols import InstanceCacheProtocol

from typing import List, Optional, Set
from threading import Event
from cacheout import Cache
import logging

INSTANCE_CACHE_MAX_SIZE = 10000
INSTANCE_CACHE_TTL_SECS = 24 * 60 * 60


class InMemoryInstanceDB:
    def __init__(self, context: ideascheduler.AppContext):
        self._context = context
        self._logger = context.logger()

        self._instances = Cache(
            maxsize=INSTANCE_CACHE_MAX_SIZE, ttl=INSTANCE_CACHE_TTL_SECS
        )

    def get(self, instance_id: str) -> Optional[EC2Instance]:
        return self._instances.get(key=instance_id)

    def get_many(self, instance_ids: List[str]) -> List[EC2Instance]:
        result = self._instances.get_many(iteratee=instance_ids)
        return list(result.values())

    def add(self, instance: EC2Instance):
        self.add_many(instances=[instance])

    def add_many(self, instances: List[EC2Instance]):
        items = {}
        for instance in instances:
            items[instance.instance_id] = instance
        self._instances.set_many(items=items)

    def delete(self, instance_id: str):
        self.delete_many(instance_ids=[instance_id])

    def delete_many(self, instance_ids: List[str]):
        self._instances.delete_many(instance_ids)

    @staticmethod
    def _apply_filters(instances: List[EC2Instance], **kwargs) -> List[EC2Instance]:
        cluster_name = kwargs.get('cluster_name', None)

        node_type = kwargs.get('node_type', None)

        instance_types = kwargs.get('instance_types', None)
        if instance_types is not None:
            if len(instance_types) > 0:
                instance_types = set(instance_types)
            else:
                instance_types = None

        # list of values from: [pending, running, shutting-down, terminated, stopping, stopped]
        states = kwargs.get('states', None)
        if states is not None:
            if len(states) > 0:
                states = set(states)
            else:
                states = None

        # one of: [spot, on-demand]
        capacity_type = kwargs.get('capacity_type', None)

        # one of: [default, dedicated]
        tenancy = kwargs.get('tenancy', None)

        job_id = kwargs.get('job_id', None)

        job_group = kwargs.get('job_group', None)

        job_project = kwargs.get('job_project', None)

        job_queue = kwargs.get('job_queue', None)

        job_queue_type = kwargs.get('job_queue_type', None)

        module_id = kwargs.get('module_id', None)

        result = []
        for instance in instances:
            if cluster_name is not None:
                if instance.soca_cluster_name is None:
                    continue
                if instance.soca_cluster_name != cluster_name:
                    continue

            if module_id is not None:
                if instance.idea_module_id is None:
                    continue
                if instance.idea_module_id != module_id:
                    continue

            if node_type is not None:
                if instance.soca_node_type is None:
                    continue
                if instance.soca_node_type != node_type:
                    continue

            if instance_types is not None:
                if instance.instance_type not in instance_types:
                    continue

            if states is not None:
                if instance.state not in states:
                    continue

            if capacity_type is not None:
                if capacity_type == 'spot':
                    if instance.spot_instance_request_id is None:
                        continue
                if capacity_type == 'on-demand':
                    if instance.spot_instance_request_id is not None:
                        continue

            if tenancy is not None:
                if instance.placement_tenancy != tenancy:
                    continue

            if job_id is not None:
                if instance.soca_job_id is None:
                    continue
                if instance.soca_job_id != job_id:
                    continue

            if job_group is not None:
                if instance.soca_job_group is None:
                    continue
                if instance.soca_job_group != job_group:
                    continue

            if job_project is not None:
                if instance.soca_job_project is None:
                    continue
                if instance.soca_job_project != job_project:
                    continue

            if job_queue is not None:
                if instance.soca_job_queue is None:
                    continue
                if instance.soca_job_queue != job_queue:
                    continue

            if job_queue_type is not None:
                if instance.soca_queue_type is None:
                    continue
                if instance.soca_queue_type != job_queue_type:
                    continue

            result.append(instance)
        return result

    def query(self, **kwargs) -> List[EC2Instance]:
        result = list(self._instances.values())
        return self._apply_filters(result, **kwargs)

    def keys(self) -> Set[str]:
        return set(self._instances.keys())


class InstanceSyncSession:
    def __init__(
        self,
        context: ideascheduler.AppContext,
        logger: logging.Logger,
        db: InMemoryInstanceDB,
        session_key: str,
    ):
        self._context = context
        self._logger = logger
        self.session_key = session_key
        self.db = db

        self.existing_keys = self.db.keys()
        self.new_keys = set()

    def sync(self, instances: List[EC2Instance]):
        for instance in instances:
            self.new_keys.add(instance.instance_id)
        self.db.add_many(instances=instances)

    def commit(self):
        self._logger.debug(f'Instance Cache: Total Instances: {len(self.new_keys)}')
        to_delete = list(self.existing_keys - self.new_keys)
        self.db.delete_many(instance_ids=to_delete)


class InstanceCache(InstanceCacheProtocol):
    def __init__(self, context: ideascheduler.AppContext):
        self._context = context
        self._logger = context.logger()
        self._session: Optional[InstanceSyncSession] = None
        self._db = InMemoryInstanceDB(context=self._context)
        self._is_ready = Event()

        self._instance_being_provisioned = Cache()

        self._job_metrics = {}
        self._job_group_metrics = {}
        self._queue_profile_metrics = {}

    def list_instances(self, **kwargs) -> List[EC2Instance]:
        return self._db.query(**kwargs)

    def list_compute_instances(
        self, cluster_name: Optional[str] = None, **kwargs
    ) -> List[EC2Instance]:
        if Utils.is_empty(cluster_name):
            cluster_name = self._context.cluster_name()

        kwargs.setdefault('cluster_name', cluster_name)
        kwargs.setdefault('node_type', constants.NODE_TYPE_COMPUTE)
        kwargs.setdefault('module_id', self._context.module_id())
        return self._db.query(**kwargs)

    def list_dcv_instances(self, cluster_name: str, **kwargs) -> List[EC2Instance]:
        kwargs.setdefault('cluster_name', cluster_name)
        kwargs.setdefault('node_type', constants.NODE_TYPE_DCV_HOST)
        return self._db.query(**kwargs)

    def get_instance(self, instance_id: str) -> Optional[EC2Instance]:
        return self._db.get(instance_id=instance_id)

    def sync_begin(self, session_key: str):
        self._session = InstanceSyncSession(
            context=self._context,
            logger=self._logger,
            db=self._db,
            session_key=session_key,
        )

    def get_job_instance_count(self, job_id: str) -> int:
        return Utils.get_value_as_int(job_id, self._job_metrics, 0)

    def get_job_group_instance_count(self, job_group: str) -> int:
        return Utils.get_value_as_int(job_group, self._job_group_metrics, 0)

    def get_queue_profile_instance_count(self, queue_profile: str) -> int:
        return Utils.get_value_as_int(queue_profile, self._queue_profile_metrics, 0)

    def sync(self, instances: Optional[List[EC2Instance]]):
        self._session.sync(instances=instances)

    def sync_commit(self, session_key: str):
        self._session.commit()
        if not self._is_ready.is_set():
            self._is_ready.set()
            self._logger.info('ec2 instance cache ready.')

        compute_instances = self.list_compute_instances(states=['pending', 'running'])
        job_metrics = {}
        job_group_metrics = {}
        queue_profile_metrics = {}
        for instance in compute_instances:
            job_id = instance.soca_job_id
            job_instance_count = Utils.get_value_as_int('count', job_metrics, 0) + 1
            job_metrics[job_id] = job_instance_count

            job_group = instance.soca_job_group
            group_instance_count = (
                Utils.get_value_as_int('count', job_group_metrics, 0) + 1
            )
            job_group_metrics[job_group] = group_instance_count

            queue_profile = instance.soca_queue_type
            queue_profile_instance_count = (
                Utils.get_value_as_int('count', queue_profile_metrics, 0) + 1
            )
            queue_profile_metrics[queue_profile] = queue_profile_instance_count

        self._job_metrics = job_metrics
        self._job_group_metrics = job_group_metrics
        self._queue_profile_metrics = queue_profile_metrics

    def sync_abort(self, session_key: str):
        self._session = None
        self._is_ready.clear()
        self._context.metrics.instance_cache_sync_failed()

    def is_ready(self) -> bool:
        return self._is_ready.is_set()
