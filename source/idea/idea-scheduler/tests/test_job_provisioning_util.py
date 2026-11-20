"""
Test Cases for JobProvisioningUtil
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideadatamodel import (
    exceptions,
    errorcodes,
    SocaJob,
    SocaJobLicenseAsk,
    SocaJobParams,
    SocaScalingMode,
)
from ideadatamodel.scheduler.scheduler_model import (
    SocaJobProvisioningOptions,
    SocaQueueManagementParams,
)
from ideascheduler.app.app_protocols import LicenseAvailability
from ideascheduler.app.provisioning import JobProvisioningUtil
from ideascheduler.app.provisioning.job_provisioner import (
    job_provisioner,
    job_provisioning_util,
)
from ideascheduler.app.provisioning.job_provisioner.job_provisioner import ProvisionJobs
from ideasdk.aws import AWSUtil
from ideatestutils import MockInstanceTypes


def build_job(enable_efa_support: bool) -> SocaJob:
    return SocaJob(
        name='mock-job',
        job_id='1',
        job_uid='mock-job-uid',
        params=SocaJobParams(
            nodes=2,
            base_os='amazonlinux2',
            instance_ami='ami-mock',
            instance_types=['hpc7a.96xlarge'],
            subnet_ids=['subnet-mock1', 'subnet-mock2'],
            security_groups=['sg-mock'],
            enable_efa_support=enable_efa_support,
        ),
    )


def test_ec2_dry_run_request_without_efa(context):
    """
    without EFA the dry run keeps the top level subnet / security group shape
    """
    util = JobProvisioningUtil(context=context, jobs=[build_job(False)])
    request = util.ec2_dry_run_request(instance_type='hpc7a.96xlarge')

    assert request['DryRun'] is True
    assert request['InstanceType'] == 'hpc7a.96xlarge'
    assert request['SubnetId'] == 'subnet-mock1'
    assert request['SecurityGroupIds'] == ['sg-mock']
    assert 'NetworkInterfaces' not in request


def test_ec2_dry_run_request_with_efa(context):
    """
    with EFA the dry run must mirror the launch template: an InterfaceType='efa' network
    interface instead of top level SubnetId / SecurityGroupIds, which RunInstances rejects
    alongside NetworkInterfaces.
    """
    util = JobProvisioningUtil(context=context, jobs=[build_job(True)])
    request = util.ec2_dry_run_request(instance_type='hpc7a.96xlarge')

    assert 'SubnetId' not in request
    assert 'SecurityGroupIds' not in request

    interfaces = request['NetworkInterfaces']
    assert len(interfaces) == 1
    assert interfaces[0]['InterfaceType'] == 'efa'
    assert interfaces[0]['DeviceIndex'] == 0
    assert interfaces[0]['NetworkCardIndex'] == 0
    assert interfaces[0]['SubnetId'] == 'subnet-mock1'
    assert interfaces[0]['Groups'] == ['sg-mock']


def test_ec2_dry_run_request_with_efa_multi_rail(context, monkeypatch):
    """
    with scheduler.efa.multi_rail_enabled the dry run models the same interface list the
    launch template attaches (build_efa_network_interface_shapes) - one EFA interface per
    network card, subnet on every interface - so a multi-ENI rejection is caught at dry run.
    """

    def get_multi_card_instance_type(_aws_util, instance_type: str):
        ec2_instance_type = MockInstanceTypes().get_instance_type(instance_type)
        network_info = ec2_instance_type.instance_type_data()['NetworkInfo']
        network_info['MaximumNetworkCards'] = 2
        network_info['EfaInfo'] = {'MaximumEfaInterfaces': 2}
        return ec2_instance_type

    monkeypatch.setattr(AWSUtil, 'get_ec2_instance_type', get_multi_card_instance_type)
    context.config().put('scheduler.efa.multi_rail_enabled', True)

    util = JobProvisioningUtil(context=context, jobs=[build_job(True)])
    request = util.ec2_dry_run_request(instance_type='hpc7a.96xlarge')

    assert 'SubnetId' not in request
    assert 'SecurityGroupIds' not in request

    interfaces = request['NetworkInterfaces']
    assert len(interfaces) == 2
    assert [interface['NetworkCardIndex'] for interface in interfaces] == [0, 1]
    # AWS shape: DeviceIndex 0 on the first network card, 1 on every additional card
    assert [interface['DeviceIndex'] for interface in interfaces] == [0, 1]
    for interface in interfaces:
        assert interface['InterfaceType'] == 'efa'
        assert interface['SubnetId'] == 'subnet-mock1'
        assert interface['Groups'] == ['sg-mock']


# a batch shares one stack, but each of its jobs is charged to its own project and asks
# for its own license seats. these walk ProvisionJobs.create_new_stack for a batch.


LICENSE_NAME = 'mock_lic_app'


class ProjectBudgets:
    """projects named over-* have spent their budget"""

    def __init__(self, context, job):
        self.job = job

    def check_budget_availability(self):
        if self.job.project.startswith('gone-'):
            # what AwsBudgetsHelper.get_budget raises for a budget deleted in AWS
            raise exceptions.SocaException(
                error_code=errorcodes.BUDGET_NOT_FOUND,
                message=f'budget for project {self.job.project} not found',
            )
        if self.job.project.startswith('over-'):
            raise exceptions.SocaException(
                error_code=errorcodes.BUDGETS_LIMIT_EXCEEDED,
                message=f'Project {self.job.project} has used its budget',
            )


class Seats:
    def __init__(self, available: int):
        self.availability = LicenseAvailability(
            available_count=available, check_ok=True
        )

    def get_license_availability(self, license_resource_name: str):
        return self.availability


def batch_job(job_id: str, project: str = 'ok-a', seats: int = 0) -> SocaJob:
    licenses = [SocaJobLicenseAsk(name=LICENSE_NAME, count=seats)] if seats else None
    return SocaJob(
        job_id=job_id,
        job_uid=f'uid-{job_id}',
        queue='normal',
        project=project,
        scaling_mode=SocaScalingMode.BATCH,
        params=SocaJobParams(nodes=1, job_group='g1234', licenses=licenses),
    )


@pytest.fixture()
def batch(context, monkeypatch):
    """create_new_stack with every AWS step stubbed; returns the provisioned job ids"""
    provisioned = []
    for check in (
        'check_bedrock',
        'ec2_dry_run',
        'check_service_quota',
        'check_reserved_instance_usage',
    ):
        monkeypatch.setattr(JobProvisioningUtil, check, lambda self: None)
    monkeypatch.setattr(JobProvisioningUtil, 'provisioned_capacity', 0)
    monkeypatch.setattr(job_provisioning_util, 'AwsBudgetsHelper', ProjectBudgets)
    monkeypatch.setattr(
        job_provisioner,
        'BatchCapacityHelper',
        lambda context, jobs, provisioned_capacity: SimpleNamespace(
            invoke=lambda: SimpleNamespace(
                provisioned_jobs=jobs,
                unprovisioned_jobs=[],
                capacity_info=SimpleNamespace(target_capacity=len(jobs), comment=''),
            )
        ),
    )
    monkeypatch.setattr(
        job_provisioner,
        'CloudFormationStackBuilder',
        lambda **_: SimpleNamespace(build=lambda: 'mock-stack-id'),
    )
    monkeypatch.setattr(
        ProvisionJobs,
        'provision_job_in_scheduler',
        lambda self, job, stack_id: provisioned.append(job.job_id),
    )
    monkeypatch.setattr(context, 'metrics', Mock(), raising=False)
    context.job_cache = SimpleNamespace(get_active_license_count=lambda license_name: 0)
    return provisioned


def create_stack(context, jobs):
    provision_jobs = ProvisionJobs(
        context=context, jobs=jobs, logger=context.logger('test')
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        provision_jobs.create_new_stack()
    assert exc_info.value.error_code == errorcodes.RETRY_JOB_PROVISIONING
    return [job.job_id for job in exc_info.value.ref]


def test_only_jobs_of_a_project_over_budget_wait(context, batch):
    jobs = [batch_job('1', 'ok-a'), batch_job('2', 'over-b'), batch_job('3', 'ok-c')]
    assert create_stack(context, jobs) == ['2']
    assert batch == ['1', '3']


def test_a_batch_whose_projects_are_all_over_budget_is_refused_with_the_reason(
    context, monkeypatch
):
    monkeypatch.setattr(job_provisioning_util, 'AwsBudgetsHelper', ProjectBudgets)
    util = JobProvisioningUtil(
        context=context, jobs=[batch_job('1', 'over-a'), batch_job('2', 'over-b')]
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        util.check_budgets()
    assert exc_info.value.error_code == errorcodes.BUDGETS_LIMIT_EXCEEDED
    assert exc_info.value.message == 'Project over-a has used its budget'


def test_a_batch_takes_no_more_license_seats_than_are_free(context, batch):
    context.license_service = Seats(available=4)
    jobs = [batch_job('1', seats=2), batch_job('2', seats=2), batch_job('3', seats=2)]
    assert create_stack(context, jobs) == ['3']
    assert batch == ['1', '2']


def test_a_batch_with_no_free_seats_waits_with_the_license_reason(context, batch):
    context.license_service = Seats(available=1)
    util = JobProvisioningUtil(
        context=context, jobs=[batch_job('1', seats=2), batch_job('2', seats=2)]
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        util.check_licenses()
    assert exc_info.value.error_code == errorcodes.NOT_ENOUGH_LICENSES
    assert exc_info.value.message == (
        f'Not enough {LICENSE_NAME} licenses: this job needs 2, 1 are free right now. '
        'Try again when other jobs finish.'
    )


def test_a_project_whose_budget_is_gone_holds_back_only_its_own_jobs(context, batch):
    # SCH-31: BUDGET_NOT_FOUND failed the whole batch every cycle
    jobs = [batch_job('1', 'ok-a'), batch_job('2', 'gone-b')]
    assert create_stack(context, jobs) == ['2']
    assert batch == ['1']


# S2: the queue gate lets shared jobs through so idle nodes they reuse cannot block them;
# a keep-forever profile (no scaling mode, as the portal saves it) must still not build
# past max_provisioned_instances when it adds a stack.


def keep_forever_job(nodes: int) -> SocaJob:
    return SocaJob(
        cluster_name='idea-mock',
        job_id='kf1',
        job_uid='uid-kf1',
        queue='normal',
        queue_type='compute',
        project='ok-a',
        provisioning_options=SocaJobProvisioningOptions(keep_forever=True),
        params=SocaJobParams(
            nodes=nodes,
            cpus=1,
            job_group='g1234',
            instance_types=['c5.large'],
            instance_ami='ami-mock',
            base_os='amazonlinux2023',
            enable_ht_support=False,
        ),
    )


@pytest.fixture()
def keep_forever(context, batch, monkeypatch):
    built = []
    monkeypatch.setattr(
        job_provisioner,
        'CloudFormationStackBuilder',
        lambda **_: SimpleNamespace(build=lambda: built.append(1) or 'mock-stack-id'),
    )
    monkeypatch.setattr(
        context,
        'queue_profiles',
        SimpleNamespace(
            get_queue_profile=lambda queue_profile_name: SimpleNamespace(
                queue_management_params=SocaQueueManagementParams(
                    max_provisioned_instances=2
                )
            )
        ),
        raising=False,
    )

    def instances_up(count: int):
        context.instance_cache = SimpleNamespace(
            get_queue_profile_instance_count=lambda queue_profile: count
        )

    return SimpleNamespace(built=built, placed=batch, instances_up=instances_up)


def test_a_keep_forever_stack_past_the_instance_limit_is_not_built(
    context, keep_forever
):
    keep_forever.instances_up(0)
    job = keep_forever_job(nodes=3)
    assert job.is_shared_capacity() and job.scaling_mode is None
    with pytest.raises(exceptions.SocaException) as exc_info:
        ProvisionJobs(
            context=context, jobs=[job], logger=context.logger('test')
        ).create_new_stack()
    assert exc_info.value.error_code == errorcodes.MAX_PROVISIONED_INSTANCES_LIMIT
    assert keep_forever.built == [] and keep_forever.placed == []


def test_a_keep_forever_stack_at_the_limit_with_no_idle_nodes_waits(
    context, keep_forever
):
    keep_forever.instances_up(2)
    with pytest.raises(exceptions.SocaException) as exc_info:
        ProvisionJobs(
            context=context,
            jobs=[keep_forever_job(nodes=1)],
            logger=context.logger('test'),
        ).create_new_stack()
    assert exc_info.value.error_code == errorcodes.MAX_PROVISIONED_INSTANCES_LIMIT
    assert keep_forever.built == []


def test_a_keep_forever_stack_below_the_limit_is_built(context, keep_forever):
    keep_forever.instances_up(1)
    ProvisionJobs(
        context=context, jobs=[keep_forever_job(nodes=1)], logger=context.logger('test')
    ).create_new_stack()
    assert keep_forever.built == [1] and keep_forever.placed == ['kf1']


def test_a_keep_forever_job_at_the_limit_runs_on_the_idle_stack(
    context, keep_forever, monkeypatch
):
    keep_forever.instances_up(2)
    job = keep_forever_job(nodes=2)
    provision = ProvisionJobs(
        context=context, jobs=[job], logger=context.logger('test')
    )
    stack = SimpleNamespace(
        stack_id='mock-stack-id',
        soca_job_group=job.get_job_group(),
        soca_capacity_signature=job.get_capacity_signature(),
        soca_job_queue=job.queue,
    )
    provision.provisioning_util._stack = stack
    monkeypatch.setattr(JobProvisioningUtil, 'is_spot_fleet', False)
    monkeypatch.setattr(
        JobProvisioningUtil,
        'auto_scaling_group',
        SimpleNamespace(desired_capacity=job.desired_capacity()),
    )
    provision.handle_completed()
    assert keep_forever.placed == ['kf1'] and keep_forever.built == []


@pytest.mark.parametrize('nodes,built', [(3, False), (2, True)])
def test_a_single_job_stack_is_checked_against_the_request_it_is_built_from(
    context, keep_forever, nodes, built
):
    # signoff2 verify-scheduler #2a: the queue gate read the cached request (1 node), the
    # stack is built from the refreshed one; an edit to 3 nodes passed a limit of 2
    keep_forever.instances_up(0)
    job = keep_forever_job(nodes=nodes)
    job.provisioning_options = SocaJobProvisioningOptions(terminate_when_idle=0)
    assert job.is_ephemeral_capacity()
    provision = ProvisionJobs(
        context=context, jobs=[job], logger=context.logger('test')
    )
    if built:
        provision.create_new_stack()
        assert keep_forever.built == [1]
        return
    with pytest.raises(exceptions.SocaException) as exc_info:
        provision.create_new_stack()
    assert exc_info.value.error_code == errorcodes.MAX_PROVISIONED_INSTANCES_LIMIT
    assert keep_forever.built == [] and keep_forever.placed == []
