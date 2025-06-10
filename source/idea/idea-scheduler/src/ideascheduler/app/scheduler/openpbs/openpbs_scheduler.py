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

from ideadatamodel import exceptions, errorcodes
from ideadatamodel.scheduler import (
    SocaJob,
    SocaQueue,
    SocaComputeNodeState,
    SocaComputeNode,
    SocaJobState,
)
from ideasdk.shell import ShellInvoker
from ideasdk.utils import Utils
from ideascheduler.app.scheduler.openpbs import OpenPBSConverter, OpenPBSQStat
from ideascheduler.app.scheduler.openpbs import openpbs_constants

from ideascheduler.app.app_protocols import SocaSchedulerProtocol

from typing import Dict, Any, List, Optional, Generator
import re
import os
import arrow
import orjson


class OpenPBSScheduler(SocaSchedulerProtocol):
    """
    OpenPBS integration for SOCA Cluster
    """

    def __init__(self, context: ideascheduler.AppContext):
        self._context = context
        self._logger = context.logger('openpbs')
        self._shell = ShellInvoker(logger=self._logger)
        self._converter = OpenPBSConverter(context=self._context, logger=self._logger)

    def is_ready(self) -> bool:
        result = self._shell.invoke('systemctl status pbs', shell=True)
        return result.returncode == 0

    def list_nodes(self, host: Optional[str] = None, **kwargs) -> List[SocaComputeNode]:
        json_data = None
        try:
            if host is not None:
                cmd = f'{openpbs_constants.PBSNODES} -v -F json {host}'.split()
            else:
                cmd = f'{openpbs_constants.PBSNODES} -v -a -F json'.split()
            result = self._shell.invoke(cmd, skip_error_logging=True)

            debug = kwargs.get('debug', False)
            if debug:
                message = f'{result}{os.linesep}'
                if result.returncode == 0:
                    message += result.stdout
                else:
                    message += result.stderr
                self._logger.info(message)

            # returncode from pbsnodes can be misleading
            # It will return with a 0 even if nodes are not found
            # Need to parse the output when requesting a specific node/host
            if result.returncode != 0:
                if 'Server has no node list' in result.stderr:
                    return []

                raise exceptions.SocaException(
                    error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
                )

            json_data = result.stdout
            json_response = Utils.from_json(json_data)
            nodes = Utils.get_value_as_dict('nodes', json_response)
            if nodes is None or len(nodes.keys()) == 0:
                return []

            response = []
            for host, node in nodes.items():
                self._logger.debug(f'Converting node data for host: {host}')

                # Handle both PBS error formats for unknown nodes
                if isinstance(node, dict):
                    # Check for old format with "Error" key
                    if 'Error' in node and 'Unknown node' in node['Error']:
                        self._logger.debug(f'Node {host} not found (old format)')
                        continue
                    # Check for new format with hostname as key
                    if host in node and 'Unknown node' in node[host]:
                        self._logger.debug(f'Node {host} not found (new format)')
                        continue

                soca_node = self._converter.to_soca_node(host=host, node=node)
                if soca_node is None:
                    self._logger.warning(
                        f'Failed to convert node data for host: {host}'
                    )
                    continue
                response.append(soca_node)
            return response
        except orjson.JSONDecodeError as e:
            self._logger.error(
                f'failed to parse json data during pbs list nodes: {json_data}'
            )
            raise e

    def create_node(self, node: SocaComputeNode) -> bool:
        cmd = [
            openpbs_constants.QMGR,
            '-c',
            f'create node {node.host} queue={node.queue}',
        ]
        result = self._shell.invoke(cmd)
        if result.returncode != 0:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
            )

        launch_time = None
        if node.launch_time:
            launch_time = int(node.launch_time.timestamp())

        spot_price = None
        if node.spot:
            if node.spot_price:
                spot_price = node.spot_price.amount
            else:
                spot_price = 'auto'

        attributes = {
            'job_id': node.job_id,
            'job_group': node.job_group,
            'launch_time': launch_time,
            'instance_id': node.instance_id,
            'instance_type': node.instance_type,
            'availability_zone': node.availability_zone,
            'subnet_id': node.subnet_id,
            'cluster_name': node.cluster_name,
            'cluster_version': node.cluster_version,
            'queue_type': node.queue_type,
            'scaling_mode': node.scaling_mode,
            'instance_ami': node.instance_ami,
            'instance_profile': node.instance_profile,
            'lifecycle': node.lifecycle,
            'tenancy': node.tenancy,
            'spot_fleet_request': node.spot_fleet_request,
            'auto_scaling_group': node.auto_scaling_group,
            'keep_forever': node.keep_forever,
            'terminate_when_idle': node.terminate_when_idle,
            'spot_price': spot_price,
            'base_os': node.base_os,
            'placement_group': node.enable_placement_group,
            'ht_support': node.enable_ht_support,
            'keep_ebs': node.keep_ebs_volumes,
            'root_size': node.root_storage_size,
            'scratch_size': node.scratch_storage_size,
            'scratch_iops': node.scratch_storage_iops,
            'efa_support': node.enable_efa_support,
            'force_ri': node.force_reserved_instances,
            'system_metrics': node.enable_system_metrics,
            'anonymous_metrics': node.enable_anonymous_metrics,
            'compute_node': node.compute_stack,
            'stack_id': node.stack_id,
            'provisioning_time': Utils.current_time_ms(),
        }

        if node.fsx_lustre:
            params = node.fsx_lustre.as_job_submit_params()
            for param in params:
                attributes[param] = params[param]

        self.set_node_attributes(host=node.host, attributes=attributes)

        return True

    def get_node(self, host: str, **kwargs) -> Optional[SocaComputeNode]:
        nodes = self.list_nodes(host=host, **kwargs)
        if len(nodes) == 0:
            return None
        return nodes[0]

    def delete_node(self, host: str) -> bool:
        cmd = [openpbs_constants.QMGR, '-c', 'delete node ' + host]
        result = self._shell.invoke(cmd=cmd)
        if result.returncode != 0:
            if result.returncode == openpbs_constants.QMGR_ERROR_CODE_OBJECT_BUSY:
                raise exceptions.SocaException(
                    error_code=errorcodes.SCHEDULER_NODE_BUSY, message=f'{result}'
                )
            else:
                raise exceptions.SocaException(
                    error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
                )
        return True

    def set_node_state(self, host: str, state: SocaComputeNodeState) -> bool:
        pbs_state = self._converter.from_soca_compute_node_state(state)
        cmd = [openpbs_constants.QMGR, '-c', f'set node {host} state={pbs_state}']
        result = self._shell.invoke(cmd)

        if result.returncode != 0:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
            )

        return True

    def set_node_attributes(self, host: str, attributes: Dict[str, Any]) -> bool:
        cmd = [openpbs_constants.QMGR, '-c']

        tokens = []
        for attr in attributes:
            value = attributes[attr]
            if value is None:
                continue
            tokens.append(f'resources_available.{attr}={value}')

        args = ','.join(tokens)
        cmd.append(f'set node {host} {args}')
        result = self._shell.invoke(cmd)

        if result.returncode != 0:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
            )

        return True

    def create_queue(self, queue_name: str):
        existing_queues = self.list_queues()
        found = None
        for existing_queue in existing_queues:
            if existing_queue.name == queue_name:
                found = existing_queue

        if found is None:
            result = self._shell.invoke(
                [openpbs_constants.QMGR, '-c', f'create queue {queue_name}'],
                skip_error_logging=True,
            )
            if result.returncode != 0:
                raise exceptions.SocaException(
                    error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
                )
            is_enabled = False
        else:
            is_enabled = Utils.is_true(found.enabled, False) and Utils.is_false(
                found.started, False
            )

        if not is_enabled:
            self.set_queue_attributes(
                queue_name=queue_name,
                attributes={
                    'queue_type': 'Execution',
                    'started': True,
                    'enabled': True,
                },
            )

    def set_queue_attributes(self, queue_name: str, attributes: Dict[str, Any]):
        cmd = [openpbs_constants.QMGR, '-c']

        tokens = []
        for attr in attributes:
            value = attributes[attr]
            if value is None:
                continue
            tokens.append(f'{attr}={value}')

        args = ','.join(tokens)
        cmd.append(f'set queue {queue_name} {args}')
        result = self._shell.invoke(cmd)

        if result.returncode != 0:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
            )

    def list_queues(self, queue: Optional[str] = None) -> List[SocaQueue]:
        json_data = None
        try:
            if queue is not None:
                cmd = f'{openpbs_constants.QSTAT} -Q -f -F json {queue}'.split()
            else:
                cmd = f'{openpbs_constants.QSTAT} -Q -f -F json'.split()
            result = self._shell.invoke(cmd, skip_error_logging=True)

            if result.returncode != 0:
                if (
                    result.returncode
                    == openpbs_constants.QSTAT_ERROR_CODE_UNKNOWN_QUEUE
                ):
                    raise exceptions.SocaException(
                        error_code=errorcodes.SCHEDULER_QUEUE_NOT_FOUND,
                        message=f'{result}',
                    )
                else:
                    raise exceptions.SocaException(
                        error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
                    )

            json_response = Utils.from_json(result.stdout)
            queues = Utils.get_value_as_dict('Queue', json_response)
            if len(queues.keys()) == 0:
                return []

            response = []
            for queue_name, params in queues.items():
                soca_queue = self._converter.to_soca_queue(
                    queue_name=queue_name, params=params
                )
                if soca_queue is None:
                    continue
                response.append(soca_queue)

            return response
        except orjson.JSONDecodeError as e:
            self._logger.error(
                f'failed to parse json data during pbs list queues: {json_data}'
            )
            raise e

    def delete_queue(self, queue_name: str):
        try:
            existing_queue = self.get_queue(queue_name)
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.SCHEDULER_QUEUE_NOT_FOUND:
                return
            else:
                raise e

        if Utils.is_true(existing_queue.enabled) or Utils.is_true(
            existing_queue.started
        ):
            self.set_queue_attributes(
                queue_name, attributes={'enabled': False, 'started': False}
            )

        cmd = [openpbs_constants.QMGR, '-c', f'delete queue {queue_name}']
        result = self._shell.invoke(cmd=cmd)
        if result.returncode != 0:
            if result.returncode == openpbs_constants.QMGR_ERROR_CODE_OBJECT_BUSY:
                raise exceptions.SocaException(
                    error_code=errorcodes.SCHEDULER_QUEUE_BUSY, message=f'{result}'
                )
            else:
                raise exceptions.SocaException(
                    error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
                )

    def get_queue(self, queue: str) -> Optional[SocaQueue]:
        try:
            queues = self.list_queues(queue=queue)
            if len(queues) == 0:
                return None
            return queues[0]
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.SCHEDULER_ERROR:
                if f'Unknown queue {queue}' in e.message:
                    return None
            raise e

    def is_queue_enabled(self, queue: str) -> bool:
        soca_queue = self.get_queue(queue)
        if soca_queue is None:
            return False
        return soca_queue.enabled and soca_queue.started

    def list_jobs(
        self,
        queue: Optional[str] = None,
        job_ids: Optional[List[str]] = None,
        owners: Optional[List[str]] = None,
        job_state: Optional[SocaJobState] = None,
        queued_after: Optional[arrow.Arrow] = None,
        max_jobs: int = -1,
        stack_id: str = None,
    ) -> List[SocaJob]:
        return OpenPBSQStat(
            context=self._context,
            logger=self._logger,
            shell=self._shell,
            converter=self._converter,
            queue=queue,
            job_ids=job_ids,
            owners=owners,
            job_state=job_state,
            queued_after=queued_after,
            max_jobs=max_jobs,
            stack_id=stack_id,
        ).list_jobs()

    def job_iterator(
        self,
        queue: Optional[str] = None,
        job_ids: Optional[List[str]] = None,
        owners: Optional[List[str]] = None,
        job_state: Optional[SocaJobState] = None,
        queued_after: Optional[arrow.Arrow] = None,
        max_jobs: int = -1,
        stack_id: str = None,
    ) -> Generator[SocaJob, None, None]:
        return OpenPBSQStat(
            context=self._context,
            logger=self._logger,
            shell=self._shell,
            converter=self._converter,
            queue=queue,
            job_ids=job_ids,
            owners=owners,
            job_state=job_state,
            queued_after=queued_after,
            max_jobs=max_jobs,
            stack_id=stack_id,
        ).job_iterator()

    def get_job(self, job_id: str) -> Optional[SocaJob]:
        return OpenPBSQStat(
            context=self._context,
            logger=self._logger,
            shell=self._shell,
            converter=self._converter,
            job_ids=[job_id],
        ).get_job()

    def delete_job(self, job_id: str):
        result = self._shell.invoke(
            [
                'su',
                str(self.get_job(job_id=job_id).owner),
                '-c',
                f'{openpbs_constants.QDEL} {job_id}',
            ],
            skip_error_logging=True,
        )
        if result.returncode != 0:
            raise exceptions.soca_exception(
                errorcodes.SCHEDULER_ERROR, f'Failed to delete job: {result}'
            )

    def is_job_active(self, job_id: str) -> bool:
        cmd = [openpbs_constants.QSTAT, job_id]
        result = self._shell.invoke(cmd, skip_error_logging=True)
        if result.returncode == 0:
            return True
        elif result.returncode == openpbs_constants.QSTAT_ERROR_CODE_UNKNOWN_JOB_ID:
            return False
        elif result.returncode == openpbs_constants.QSTAT_ERROR_CODE_JOB_FINISHED:
            return False

        raise exceptions.SocaException(
            error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
        )

    def get_finished_job(self, job_id: str) -> Optional[SocaJob]:
        return OpenPBSQStat(
            context=self._context,
            logger=self._logger,
            shell=self._shell,
            converter=self._converter,
            job_ids=[job_id],
            job_state=SocaJobState.FINISHED,
        ).get_job()

    def is_job_queued_or_running(self, job_id: str) -> bool:
        cmd = [openpbs_constants.QSTAT, job_id]
        result = self._shell.invoke(cmd=cmd)
        return result.returncode == 0

    def set_job_attributes(self, job_id: str, attributes: Dict[str, Any]) -> bool:
        cmd = [openpbs_constants.QALTER]
        for attr in attributes:
            cmd.append('-l')
            value = attributes[attr]
            if value is None:
                cmd.append(f'{attr}=')
            else:
                if attr == 'error_message':
                    error_message = str(value)
                    # replace all white spaces with _ as qalter does not accept whitespace in str
                    error_message = re.sub(' ', '_', error_message)
                    # replace all ";" as the accounting log file splits log entry using ";"
                    error_message = error_message.replace(';', '_')
                    # replace other cases
                    error_message = error_message.replace("'", '_').replace('"', '_')
                    value = error_message
                cmd.append(f'{attr}={value}')
        cmd.append(job_id)
        result = self._shell.invoke(cmd)
        if result.returncode != 0:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_ERROR, message=f'{result}'
            )
        return True

    def provision_job(self, job: SocaJob, stack_id: str) -> int:
        try:
            select = job.params.custom_params['select'].split(':compute_node')[0]
            select += f':compute_node={job.get_compute_stack()}'
            provisioning_time = Utils.current_time_ms()
            self.set_job_attributes(
                job_id=job.job_id,
                attributes={
                    'select': select,
                    'stack_id': stack_id,
                    'provisioning_time': provisioning_time,
                },
            )
            return provisioning_time
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.SCHEDULER_JOB_FINISHED:
                return -1
            else:
                raise e

    def reset_job(self, job_id: str) -> bool:
        try:
            job = self.get_job(job_id=job_id)
            select = job.params.custom_params['select'].split(':compute_node')[0]
            select += ':compute_node=tbd'

            # do not delete job group
            return self.set_job_attributes(
                job_id=job_id,
                attributes={
                    'select': select,
                    'stack_id': 'tbd',
                    'provisioning_time': None,
                },
            )
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.SCHEDULER_JOB_FINISHED:
                return True
            else:
                raise e
