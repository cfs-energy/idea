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
from ideadatamodel import (
    exceptions,
    constants,
    errorcodes,
    SocaMemory,
    SocaAmount,
    SocaComputeNode,
    SocaComputeNodeState,
    OpenPBSInfo,
    SocaComputeNodeSharing,
    SocaComputeNodeResources,
    SocaQueue,
    SocaQueueStats,
    SocaJob,
    SocaScalingMode,
)
from ideasdk.utils import Utils
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSJob

from typing import Dict, Optional, List
import arrow
import logging


class OpenPBSConverter:
    """
    Mapping class to convert data formats to/from SOCA canonical data model and OpenPBS
    """

    def __init__(self, context: ideascheduler.AppContext, logger: logging.Logger):
        self._context = context
        self._logger = logger

    @staticmethod
    def to_soca_compute_node_state(
        state: Optional[str] = None,
    ) -> Optional[List[SocaComputeNodeState]]:
        if state is None:
            return None
        tokens = state.strip().lower().split(',')
        result = set()
        for _token in tokens:
            if _token == 'busy':
                result.add(SocaComputeNodeState.BUSY)
            elif _token == 'down':
                result.add(SocaComputeNodeState.DOWN)
            elif _token == 'free':
                result.add(SocaComputeNodeState.FREE)
            elif _token == 'offline':
                result.add(SocaComputeNodeState.OFFLINE)
            elif _token == 'job-busy':
                result.add(SocaComputeNodeState.JOB_BUSY)
            elif _token == 'job-exclusive':
                result.add(SocaComputeNodeState.JOB_EXCLUSIVE)
            elif _token == 'provisioning':
                result.add(SocaComputeNodeState.PROVISIONING)
            elif _token == 'resv-exclusive':
                result.add(SocaComputeNodeState.RESV_EXCLUSIVE)
            elif _token == 'stale':
                result.add(SocaComputeNodeState.STALE)
            elif _token == 'stale-unknown':
                result.add(SocaComputeNodeState.STALE_UNKNOWN)
            elif _token == 'unresolvable':
                result.add(SocaComputeNodeState.UNRESOLVABLE)
            elif _token == 'wait-provisioning':
                result.add(SocaComputeNodeState.WAIT_PROVISIONING)
            elif _token == 'initializing':
                result.add(SocaComputeNodeState.INITIALIZING)

        if len(result) == 0:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_ERROR,
                message=f'Unknown SocaComputeNode state: {state}',
            )

        return list(result)

    @staticmethod
    def from_soca_compute_node_state(state: SocaComputeNodeState) -> str:
        if state == SocaComputeNodeState.BUSY:
            return 'busy'
        elif state == SocaComputeNodeState.DOWN:
            return 'down'
        elif state == SocaComputeNodeState.FREE:
            return 'free'
        elif state == SocaComputeNodeState.OFFLINE:
            return 'offline'
        elif state == SocaComputeNodeState.JOB_BUSY:
            return 'job-busy'
        elif state == SocaComputeNodeState.JOB_EXCLUSIVE:
            return 'job-exclusive'
        elif state == SocaComputeNodeState.PROVISIONING:
            return 'provisioning'
        elif state == SocaComputeNodeState.RESV_EXCLUSIVE:
            return 'resv-exclusive'
        elif state == SocaComputeNodeState.STALE:
            return 'stale'
        elif state == SocaComputeNodeState.STALE_UNKNOWN:
            return 'stale-unknown'
        elif state == SocaComputeNodeState.UNRESOLVABLE:
            return 'unresolvable'
        elif state == SocaComputeNodeState.WAIT_PROVISIONING:
            return 'wait-provisioning'

        raise exceptions.SocaException(
            error_code=errorcodes.SCHEDULER_ERROR,
            message=f'Unknown node state: {state}',
        )

    @staticmethod
    def to_soca_compute_node_sharing(
        sharing: Optional[str] = None,
    ) -> Optional[SocaComputeNodeSharing]:
        if sharing is None:
            return None
        if sharing == 'default_excl':
            return SocaComputeNodeSharing.DEFAULT_EXCL
        elif sharing == 'default_exclhost':
            return SocaComputeNodeSharing.DEFAULT_EXCLHOST
        elif sharing == 'default_shared':
            return SocaComputeNodeSharing.DEFAULT_SHARED
        elif sharing == 'force_excl':
            return SocaComputeNodeSharing.FORCE_EXCL
        elif sharing == 'force_exclhost':
            return SocaComputeNodeSharing.FORCE_EXCLHOST
        elif sharing == 'ignore_excl':
            return SocaComputeNodeSharing.IGNORE_EXCL

        raise exceptions.SocaException(
            error_code=errorcodes.SCHEDULER_ERROR,
            message=f'Unknown SocaComputeNodeSharing value: {sharing}',
        )

    @staticmethod
    def to_soca_compute_node_resources(
        resources: Optional[Dict],
    ) -> Optional[SocaComputeNodeResources]:
        if resources is None:
            return None

        if len(resources.keys()) == 0:
            return None

        # todo - verify if OpenPBS implements KIB or KB
        #   the response says kb, but is it indeed kb?
        memory = SocaMemory.resolve(Utils.get_value_as_string('mem', resources))
        cpus = Utils.get_value_as_string('ncpus', resources)
        # todo - gpus, mpiprocs and any additional..

        return SocaComputeNodeResources(memory=memory, cpus=cpus)

    def to_soca_node(
        self, host: str, node: Optional[Dict] = None
    ) -> Optional[SocaComputeNode]:
        if node is None:
            return None

        error = Utils.get_value_as_string('Error', node)
        if error is not None:
            return None

        # states
        states = self.to_soca_compute_node_state(
            Utils.get_value_as_string('state', node)
        )

        pbs_jobs = Utils.get_value_as_list('jobs', node, [])
        jobs = []
        for pbs_job_id in pbs_jobs:
            jobs.append(pbs_job_id.split('.')[0])

        # scheduler_info
        scheduler_name = constants.SCHEDULER_OPENPBS
        scheduler_version = Utils.get_value_as_string('pbs_version', node)
        mom_port = Utils.get_value_as_int('Port', node)
        scheduler_info = OpenPBSInfo(
            name=scheduler_name,
            version=scheduler_version,
            mom_private_dns=host,
            mom_port=mom_port,
        )
        # last_used_time
        last_used_time = Utils.get_value_as_int('last_used_time', node)
        if last_used_time is not None:
            last_used_time = arrow.get(last_used_time).datetime

        # last_state_changed_time
        last_state_changed_time = Utils.get_value_as_int('last_state_change_time', node)
        if last_state_changed_time is not None:
            last_state_changed_time = arrow.get(last_state_changed_time).datetime

        # sharing
        sharing = self.to_soca_compute_node_sharing(
            Utils.get_value_as_string('sharing', node)
        )

        # queue
        queue = Utils.get_value_as_string('queue', node)

        pbs_resources_available = Utils.get_value_as_dict('resources_available', node)

        # launch_time (seconds, since EC2 track in seconds)
        launch_time = Utils.get_value_as_int('launch_time', pbs_resources_available)
        if launch_time:
            launch_time = arrow.get(launch_time).datetime

        # provisioning_time (millis)
        provisioning_time = Utils.get_value_as_int(
            'provisioning_time', pbs_resources_available
        )
        if provisioning_time:
            provisioning_time = arrow.get(provisioning_time).datetime

        compute_stack = Utils.get_value_as_string(
            'compute_node', pbs_resources_available
        )
        stack_id = Utils.get_value_as_string('stack_id', pbs_resources_available)

        cluster_name = Utils.get_value_as_string(
            'cluster_name', pbs_resources_available
        )
        cluster_version = Utils.get_value_as_string(
            'cluster_version', pbs_resources_available
        )

        instance_id = Utils.get_value_as_string('instance_id', pbs_resources_available)
        instance_type = Utils.get_value_as_string(
            'instance_type', pbs_resources_available
        )
        availability_zone = Utils.get_value_as_string(
            'availability_zone', pbs_resources_available
        )
        subnet_id = Utils.get_value_as_string('subnet_id', pbs_resources_available)
        instance_ami = Utils.get_value_as_string(
            'instance_ami', pbs_resources_available
        )
        instance_profile = Utils.get_value_as_string(
            'instance_profile', pbs_resources_available
        )
        lifecycle = Utils.get_value_as_string('lifecycle', pbs_resources_available)
        tenancy = Utils.get_value_as_string('tenancy', pbs_resources_available)
        spot_fleet_request = Utils.get_value_as_string(
            'spot_fleet_request', pbs_resources_available
        )
        auto_scaling_group = Utils.get_value_as_string(
            'auto_scaling_group', pbs_resources_available
        )

        job_id = Utils.get_value_as_string('job_id', pbs_resources_available)
        job_group = Utils.get_value_as_string('job_group', pbs_resources_available)

        queue_type = Utils.get_value_as_string('queue_type', pbs_resources_available)
        scaling_mode = Utils.get_value_as_string(
            'scaling_mode', pbs_resources_available
        )
        scaling_mode = SocaScalingMode.resolve(scaling_mode)
        keep_forever = Utils.get_value_as_bool('keep_forever', pbs_resources_available)
        terminate_when_idle = Utils.get_value_as_int(
            'terminate_when_idle', pbs_resources_available
        )
        base_os = Utils.get_value_as_string('base_os', pbs_resources_available)
        enable_placement_group = Utils.get_value_as_string(
            'placement_group', pbs_resources_available
        )
        enable_ht_support = Utils.get_value_as_string(
            'ht_support', pbs_resources_available
        )
        keep_ebs_volumes = Utils.get_value_as_bool('keep_ebs', pbs_resources_available)
        scratch_storage_iops = Utils.get_value_as_int(
            'scratch_iops', pbs_resources_available
        )
        enable_efa_support = Utils.get_value_as_bool(
            'efa_support', pbs_resources_available
        )
        force_reserved_instances = Utils.get_value_as_bool(
            'force_ri', pbs_resources_available
        )
        enable_system_metrics = Utils.get_value_as_bool(
            'system_metrics', pbs_resources_available
        )
        enable_anonymous_metrics = Utils.get_value_as_bool(
            'anonymous_metrics', pbs_resources_available
        )

        spot = False
        spot_price = Utils.get_value_as_string('spot_price', pbs_resources_available)
        if spot_price is not None:
            if spot_price == 'auto':
                spot = True
                spot_price = None
            elif Utils.is_float(spot_price):
                spot = True
                spot_price_amount = Utils.is_float(spot_price)
                spot_price = SocaAmount(amount=spot_price_amount)
            else:
                spot_price = None

        root_storage_size = Utils.get_value_as_string(
            'root_size', pbs_resources_available
        )
        if root_storage_size:
            root_storage_size = SocaMemory.resolve(root_storage_size)

        scratch_storage_size = Utils.get_value_as_string(
            'scratch_size', pbs_resources_available
        )
        if scratch_storage_size:
            scratch_storage_size = SocaMemory.resolve(scratch_storage_size)

        # todo - fsx lustre config

        # resources_available
        resources_available = self.to_soca_compute_node_resources(
            pbs_resources_available
        )

        # resources_assigned
        resources_assigned = self.to_soca_compute_node_resources(
            Utils.get_value_as_dict('resources_assigned', node)
        )

        return SocaComputeNode(
            host=host,
            states=states,
            queue=queue,
            sharing=sharing,
            resources_available=resources_available,
            resources_assigned=resources_assigned,
            scheduler_info=scheduler_info,
            launch_time=launch_time,
            provisioning_time=provisioning_time,
            last_used_time=last_used_time,
            last_state_changed_time=last_state_changed_time,
            compute_stack=compute_stack,
            instance_id=instance_id,
            instance_type=instance_type,
            availability_zone=availability_zone,
            subnet_id=subnet_id,
            cluster_name=cluster_name,
            cluster_version=cluster_version,
            queue_type=queue_type,
            stack_id=stack_id,
            scaling_mode=scaling_mode,
            job_id=job_id,
            job_group=job_group,
            instance_ami=instance_ami,
            instance_profile=instance_profile,
            lifecycle=lifecycle,
            tenancy=tenancy,
            spot_fleet_request=spot_fleet_request,
            auto_scaling_group=auto_scaling_group,
            keep_forever=keep_forever,
            terminate_when_idle=terminate_when_idle,
            spot=spot,
            spot_price=spot_price,
            base_os=base_os,
            enable_placement_group=enable_placement_group,
            enable_ht_support=enable_ht_support,
            keep_ebs_volumes=keep_ebs_volumes,
            root_storage_size=root_storage_size,
            scratch_storage_size=scratch_storage_size,
            scratch_storage_iops=scratch_storage_iops,
            enable_efa_support=enable_efa_support,
            force_reserved_instances=force_reserved_instances,
            enable_system_metrics=enable_system_metrics,
            enable_anonymous_metrics=enable_anonymous_metrics,
            jobs=jobs,
        )

    @staticmethod
    def to_soca_queue(
        queue_name: str, params: Optional[Dict] = None
    ) -> Optional[SocaQueue]:
        if params is None:
            return None

        total_jobs = Utils.get_value_as_int('total_jobs', params)
        state_count = Utils.get_value_as_string('state_count', params)
        state_count_tokens = state_count.split(' ')

        transit = 0
        queued = 0
        held = 0
        waiting = 0
        running = 0
        exiting = 0
        begun = 0
        for token in state_count_tokens:
            kv = token.split(':')
            key = kv[0]
            value = Utils.get_as_int(kv[1])
            if key == 'Transit':
                transit = value
            elif key == 'Queued':
                queued = value
            elif key == 'Held':
                held = value
            elif key == 'Waiting':
                waiting = value
            elif key == 'Running':
                running = value
            elif key == 'Exiting':
                exiting = value
            elif key == 'Begun':
                begun = value

        enabled = Utils.get_value_as_bool('enabled', params)
        started = Utils.get_value_as_bool('started', params)

        return SocaQueue(
            name=queue_name,
            enabled=enabled,
            started=started,
            total_jobs=total_jobs,
            stats=SocaQueueStats(
                transit=transit,
                queued=queued,
                held=held,
                waiting=waiting,
                running=running,
                exiting=exiting,
                begun=begun,
            ),
        )

    def to_soca_job(
        self, job_id: Optional[str], params: Optional[Dict]
    ) -> Optional[SocaJob]:
        if params is None:
            return None

        pbs_job = OpenPBSJob(id=job_id, **params)

        return pbs_job.as_soca_job(context=self._context)
