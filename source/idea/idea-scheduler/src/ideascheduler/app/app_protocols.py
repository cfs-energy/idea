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

from ideasdk.protocols import SocaBaseProtocol, SocaServiceProtocol
from ideadatamodel.aws import EC2Instance
from ideadatamodel.scheduler import (
    SocaJobState,
    SocaJobExecutionHost,
    SocaJob,
    SocaComputeNodeState,
    SocaComputeNode,
    SocaQueue,
    HpcQueueProfile,
    ListNodesRequest,
    ListNodesResult,
    ListJobsRequest,
    ListJobsResult,
    SubmitJobResult,
    CreateHpcApplicationRequest,
    CreateHpcApplicationResult,
    UpdateHpcApplicationRequest,
    UpdateHpcApplicationResult,
    GetHpcApplicationRequest,
    GetHpcApplicationResult,
    DeleteHpcApplicationRequest,
    DeleteHpcApplicationResult,
    ListHpcApplicationsRequest,
    ListHpcApplicationsResult,
    GetUserApplicationsRequest,
    GetUserApplicationsResult,
    CreateHpcLicenseResourceRequest,
    CreateHpcLicenseResourceResult,
    GetHpcLicenseResourceRequest,
    GetHpcLicenseResourceResult,
    UpdateHpcLicenseResourceRequest,
    UpdateHpcLicenseResourceResult,
    DeleteHpcLicenseResourceRequest,
    DeleteHpcLicenseResourceResult,
    ListHpcLicenseResourcesRequest,
    ListHpcLicenseResourcesResult,
    CheckHpcLicenseResourceAvailabilityRequest,
    CheckHpcLicenseResourceAvailabilityResult
)

from abc import abstractmethod, ABC
from typing import List, Optional, Dict, Any, TypeVar, Union, Generator
import arrow
import dataset


class JobCacheProtocol(SocaBaseProtocol):

    @abstractmethod
    def sync(self, jobs: List[SocaJob]):
        ...

    @abstractmethod
    def add_finished_job(self, job: SocaJob):
        ...

    @abstractmethod
    def list_jobs(self, _limit: int = -1, _offset: int = 0, **kwargs) -> List[SocaJob]:
        ...

    @abstractmethod
    def list_completed_jobs(self, _limit: int = -1, _offset: int = 0, **kwargs) -> List[SocaJob]:
        ...

    @abstractmethod
    def get_job(self, job_id: str) -> Optional[SocaJob]:
        ...

    @abstractmethod
    def get_completed_job(self, job_id: str) -> Optional[SocaJob]:
        ...

    @abstractmethod
    def delete_jobs(self, job_ids: List[str]):
        ...

    @abstractmethod
    def log_job_execution(self, job_id: str, execution_host: SocaJobExecutionHost,
                          instance: Optional[EC2Instance] = None):
        ...

    @abstractmethod
    def get_job_execution_hosts(self, job_id: str) -> Optional[List[SocaJobExecutionHost]]:
        ...

    @abstractmethod
    def delete_job_execution_hosts(self, job_id: str):
        ...

    @abstractmethod
    def get_jobs_table(self) -> dataset.Table:
        ...

    @abstractmethod
    def get_connection(self) -> dataset.Database:
        ...

    @abstractmethod
    def get_desired_capacity(self, job_group: str) -> int:
        ...

    @abstractmethod
    def get_active_jobs(self, queue_profile: str) -> int:
        ...

    @abstractmethod
    def get_count(self, **kwargs) -> int:
        ...

    @abstractmethod
    def get_completed_jobs_count(self, **kwargs) -> int:
        ...

    @abstractmethod
    def get_active_license_count(self, license_name: str) -> int:
        ...

    @abstractmethod
    def is_ready(self) -> bool:
        ...

    @abstractmethod
    def set_ready(self):
        ...

    @abstractmethod
    def add_active_licenses(self, jobs: List[SocaJob]):
        ...

    @abstractmethod
    def convert_db_entry_to_job(self, entry: Dict) -> Optional[SocaJob]:
        ...

    @abstractmethod
    def set_job_provisioning_error(self, job_id: str, error_code: str, message: str):
        ...

    @abstractmethod
    def clear_job_provisioning_error(self, job_id: str):
        ...


JobCacheType = TypeVar('JobCacheType', bound=JobCacheProtocol)


class JobMonitorProtocol(SocaServiceProtocol):

    @abstractmethod
    def job_queued(self, job: SocaJob):
        ...

    @abstractmethod
    def job_modified(self, job: SocaJob):
        ...

    @abstractmethod
    def job_running(self, job: SocaJob):
        ...

    @abstractmethod
    def job_status_update(self, job: SocaJob):
        ...


JobMonitorType = TypeVar('JobMonitorType', bound=JobMonitorProtocol)


class NodeMonitorProtocol(SocaServiceProtocol, ABC):
    pass


NodeMonitorType = TypeVar('NodeMonitorType', bound=NodeMonitorProtocol)


class JobProvisioningQueueProtocol(SocaBaseProtocol):

    @abstractmethod
    def put(self, job: SocaJob, modified=False):
        ...

    @abstractmethod
    def get(self, timeout: Optional[float] = 1) -> SocaJob:
        ...

    @abstractmethod
    def delete(self, job_id: str):
        ...

    @abstractmethod
    def get_queue_size(self, key: Optional[str] = None) -> int:
        ...


JobProvisioningQueueType = TypeVar('JobProvisioningQueueType', bound=JobProvisioningQueueProtocol)


class InstanceCacheProtocol(SocaBaseProtocol):

    @abstractmethod
    def list_instances(self, **kwargs) -> List[EC2Instance]:
        ...

    @abstractmethod
    def list_compute_instances(self, cluster_name: str, **kwargs) -> List[EC2Instance]:
        ...

    @abstractmethod
    def list_dcv_instances(self, cluster_name: str, **kwargs) -> List[EC2Instance]:
        ...

    @abstractmethod
    def get_instance(self, instance_id: str) -> Optional[EC2Instance]:
        ...

    @abstractmethod
    def is_ready(self) -> bool:
        ...

    @abstractmethod
    def get_job_instance_count(self, job_id: str) -> int:
        ...

    @abstractmethod
    def get_job_group_instance_count(self, job_group: str) -> int:
        ...

    @abstractmethod
    def get_queue_profile_instance_count(self, queue_profile: str) -> int:
        ...


InstanceCacheType = TypeVar('InstanceCacheType', bound=InstanceCacheProtocol)


class SocaSchedulerProtocol(SocaBaseProtocol):

    @abstractmethod
    def is_ready(self) -> bool:
        ...

    @abstractmethod
    def list_nodes(self, **kwargs) -> List[SocaComputeNode]:
        ...

    @abstractmethod
    def create_node(self, node: SocaComputeNode) -> bool:
        ...

    @abstractmethod
    def get_node(self, host: str, **kwargs) -> Optional[SocaComputeNode]:
        ...

    @abstractmethod
    def delete_node(self, host: str) -> bool:
        ...

    @abstractmethod
    def set_node_state(self, host: str, state: SocaComputeNodeState):
        ...

    @abstractmethod
    def set_node_attributes(self, host: str, attributes: Dict[str, Any]) -> bool:
        ...

    @abstractmethod
    def create_queue(self, queue_name: str):
        ...

    @abstractmethod
    def set_queue_attributes(self, queue_name: str, attributes: Dict[str, Any]):
        ...

    @abstractmethod
    def list_queues(self, **kwargs) -> List[SocaQueue]:
        ...

    @abstractmethod
    def delete_queue(self, queue_name: str):
        ...

    @abstractmethod
    def is_queue_enabled(self, queue: str) -> bool:
        ...

    @abstractmethod
    def get_queue(self, queue: str) -> Optional[SocaQueue]:
        ...

    @abstractmethod
    def list_jobs(self, queue: Optional[str] = None, job_ids: Optional[List[str]] = None,
                  owners: Optional[List[str]] = None, job_state: Optional[SocaJobState] = None,
                  queued_after: Optional[arrow.Arrow] = None,
                  max_jobs: int = -1,
                  stack_id: str = None) -> List[SocaJob]:
        ...

    @abstractmethod
    def job_iterator(self, queue: Optional[str] = None, job_ids: Optional[List[str]] = None,
                     owners: Optional[List[str]] = None, job_state: Optional[SocaJobState] = None,
                     queued_after: Optional[arrow.Arrow] = None,
                     max_jobs: int = -1,
                     stack_id: str = None) -> Generator[SocaJob, None, None]:
        ...

    @abstractmethod
    def get_job(self, job_id: str) -> Optional[SocaJob]:
        ...

    @abstractmethod
    def delete_job(self, job_id: str):
        ...

    @abstractmethod
    def is_job_active(self, job_id: str) -> bool:
        ...

    @abstractmethod
    def get_finished_job(self, job_id: str) -> Optional[SocaJob]:
        ...

    @abstractmethod
    def is_job_queued_or_running(self, job_id: str) -> bool:
        ...

    @abstractmethod
    def set_job_attributes(self, job_id: str, attributes: Dict[str, Any]) -> bool:
        ...

    @abstractmethod
    def provision_job(self, job: SocaJob,
                      stack_id: str) -> int:
        ...

    @abstractmethod
    def reset_job(self, job_id: str) -> bool:
        ...


class DocumentStoreProtocol(SocaBaseProtocol):

    @abstractmethod
    def initialize(self):
        ...

    @abstractmethod
    def is_enabled(self) -> bool:
        ...

    @abstractmethod
    def add_jobs(self, jobs: List[SocaJob], **kwargs) -> bool:
        ...

    @abstractmethod
    def add_nodes(self, nodes: List[SocaComputeNode], **kwargs) -> bool:
        ...

    @abstractmethod
    def search_jobs(self, options: ListJobsRequest, **kwargs) -> ListJobsResult:
        ...

    @abstractmethod
    def search_nodes(self, options: ListNodesRequest, **kwargs) -> ListNodesResult:
        ...


class JobSubmissionTrackerProtocol(SocaBaseProtocol):

    @abstractmethod
    def ok(self, result: SubmitJobResult):
        ...

    @abstractmethod
    def fail(self, job_uid: str, exc: BaseException):
        ...

    @abstractmethod
    def get(self, job_uid: str) -> Optional[Union[SubmitJobResult, BaseException]]:
        ...


class HpcApplicationsProtocol:

    @abstractmethod
    def create_application(self, request: CreateHpcApplicationRequest) -> CreateHpcApplicationResult:
        ...

    @abstractmethod
    def get_application(self, request: GetHpcApplicationRequest) -> GetHpcApplicationResult:
        ...

    @abstractmethod
    def update_application(self, request: UpdateHpcApplicationRequest) -> UpdateHpcApplicationResult:
        ...

    @abstractmethod
    def delete_application(self, request: DeleteHpcApplicationRequest) -> DeleteHpcApplicationResult:
        ...

    @abstractmethod
    def list_applications(self, request: ListHpcApplicationsRequest) -> ListHpcApplicationsResult:
        ...

    @abstractmethod
    def get_user_applications(self, request: GetUserApplicationsRequest) -> GetUserApplicationsResult:
        ...


class HpcQueueProfilesServiceProtocol(SocaServiceProtocol):

    @abstractmethod
    def create_queue_profile(self, queue_profile: HpcQueueProfile) -> HpcQueueProfile:
        ...

    @abstractmethod
    def update_queue_profile(self, queue_profile: HpcQueueProfile) -> HpcQueueProfile:
        ...

    @abstractmethod
    def get_queue_profile(self, queue_profile_id: str = None,
                          queue_profile_name: str = None,
                          queue_name: str = None) -> HpcQueueProfile:
        ...

    @abstractmethod
    def list_queue_profiles(self) -> List[HpcQueueProfile]:
        ...

    @abstractmethod
    def get_provisioning_queue(self, queue_profile_name: str) -> Optional[JobProvisioningQueueProtocol]:
        ...

    @abstractmethod
    def enable_queue_profile(self, queue_profile_id: str = None, queue_profile_name: str = None):
        ...

    @abstractmethod
    def disable_queue_profile(self, queue_profile_id: str = None, queue_profile_name: str = None):
        ...

    @abstractmethod
    def delete_queue_profile(self, queue_profile_id: str = None, queue_profile_name: str = None, delete_queues=True):
        ...

    @abstractmethod
    def create_queues(self, queue_names: List[str],
                      queue_profile_id: str = None,
                      queue_profile_name: str = None,
                      update_db=True,
                      check_existing_profile=True):
        ...

    @abstractmethod
    def delete_queues(self, queue_names: List[str],
                      queue_profile_id: str = None,
                      queue_profile_name: str = None,
                      update_db=True,
                      initialize_job_provisioner=True):
        ...


class LicenseServiceProtocol(SocaBaseProtocol):

    def create_license_resource(self, request: CreateHpcLicenseResourceRequest) -> CreateHpcLicenseResourceResult:
        ...

    def get_license_resource(self, request: GetHpcLicenseResourceRequest) -> GetHpcLicenseResourceResult:
        ...

    def update_license_resource(self, request: UpdateHpcLicenseResourceRequest) -> UpdateHpcLicenseResourceResult:
        ...

    def delete_license_resource(self, request: DeleteHpcLicenseResourceRequest) -> DeleteHpcLicenseResourceResult:
        ...

    def list_license_resources(self, request: ListHpcLicenseResourcesRequest) -> ListHpcLicenseResourcesResult:
        ...

    def get_available_licenses(self, license_resource_name: str) -> int:
        ...

    def check_license_resource_availability(self, request: CheckHpcLicenseResourceAvailabilityRequest) -> CheckHpcLicenseResourceAvailabilityResult:
        ...


class JobNotificationsProtocol(SocaBaseProtocol):

    def job_started(self, job: SocaJob):
        ...

    def job_completed(self, job: SocaJob):
        ...
