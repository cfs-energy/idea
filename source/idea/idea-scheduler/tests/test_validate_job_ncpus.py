"""CPU sizing runs with real queue defaults and CPU validation, without AWS."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideadatamodel import SocaJobParams, SocaException, errorcodes
from ideascheduler.app.api.opepbs_api import OpenPBSAPI
from ideascheduler.app.scheduler.openpbs.openpbs_api import OpenPBSHookRequest
from ideascheduler.app.scheduler.openpbs.openpbs_api_invocation_context import (
    OpenPBSAPIInvocationContext,
)
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSEvent, OpenPBSJob
from ideascheduler.app.scheduler.pbs_select import select_compute_node
from test_job_project_default import _projects, _use_queue, _validate_hook


@pytest.fixture
def submit(context, monkeypatch):
    # no persistent pool exists yet unless a test provides one
    monkeypatch.setattr(
        type(context.aws_util()),
        'cloudformation_describe_stack',
        lambda self, stack_name: None,
    )

    def run(
        resources,
        *,
        defaults=None,
        queue='normal',
        event_type='queuejob',
        before_response=None,
        queue_settings=None,
        job_o=None,
        src_queue=None,
        source_profile=None,
        job=None,
    ):
        _use_queue(context, monkeypatch, _projects('project-a'))
        profile = context.queue_profiles.get_queue_profile.return_value
        for name, value in (queue_settings or {}).items():
            setattr(profile, name, value)
        profile.default_job_params = defaults or SocaJobParams(
            instance_types=['c5n.18xlarge']
        )
        if source_profile is not None:
            context.queue_profiles.get_queue_profile.side_effect = lambda **kw: (
                source_profile if kw.get('queue_name') == src_queue else profile
            )
        monkeypatch.setattr(context, 'job_monitor', Mock())
        event = OpenPBSEvent(
            type=event_type,
            requestor='researcher',
            job_o=job_o,
            src_queue=src_queue,
            job=job
            or OpenPBSJob(
                queue=queue,
                project='project-a',
                Job_Name='job',
                Resource_List=resources,
                # the PBS hook copies the dry_run resource onto the job (handler :1110)
                dry_run=resources.get('dry_run'),
            ),
        )
        api_context = Mock(context=context)
        api_context.get_request_payload_as.return_value = OpenPBSHookRequest(
            event=event
        )
        hook = OpenPBSAPIInvocationContext(api_context)
        monkeypatch.setattr(hook, 'check_incidentals', Mock())
        monkeypatch.setattr(hook, 'get_bom_cost', Mock())
        monkeypatch.setattr(hook, 'get_budget_usage', Mock())
        if before_response:
            original = hook.build_and_validate_job

            def build():
                original()
                before_response(hook)

            monkeypatch.setattr(hook, 'build_and_validate_job', build)
            monkeypatch.setattr(hook, 'get_bom_cost', Mock())
        result = _validate_hook(context, monkeypatch, hook)
        return result, hook

    return run


@pytest.mark.parametrize('ht,expected', [(None, 36), (False, 36), (True, 72)])
def test_queue_default_sizes_existing_select(submit, ht, expected):
    resources = {'select': '2:mem=8gb'}
    if ht is not None:
        resources['ht_support'] = str(ht)
    result, hook = submit(resources)
    assert result.accept is True, result.formatted_user_message
    assert (
        result.resources_updated['select']
        == f'2:mem=8gb:ncpus={expected}:compute_node=tbd'
    )
    assert hook.job.params.instance_types == ['c5n.18xlarge']


def test_queue_ht_default(submit):
    result, _ = submit(
        {'select': '1:mem=8gb'},
        defaults=SocaJobParams(instance_types=['c5n.18xlarge'], enable_ht_support=True),
    )
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['select'] == '1:mem=8gb:ncpus=72:compute_node=tbd'


@pytest.mark.parametrize(
    'select,expected',
    [
        ('1:ncpus=4', '1:ncpus=4:compute_node=tbd'),
        (
            '1:ncpus=4+2:mem=8gb',
            '1:ncpus=4:compute_node=tbd+2:mem=8gb:ncpus=36:compute_node=tbd',
        ),
        (
            '1:compute_node=tbd:ncpus=4+2:mem=8gb:compute_node=tbd:mpiprocs=3',
            '1:ncpus=4:compute_node=tbd+2:mem=8gb:mpiprocs=3:ncpus=36:compute_node=tbd',
        ),
    ],
)
def test_preserves_explicit_cpus_and_all_chunks(submit, select, expected):
    result, _ = submit({'select': select})
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['select'] == expected


@pytest.mark.parametrize('ht,expected', [('false', 1), ('true', 2)])
def test_instance_alternatives_use_minimum(submit, ht, expected):
    result, _ = submit(
        {'select': '1', 'instance_type': 'c5.xlarge+c5.large', 'ht_support': ht}
    )
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['select'] == f'1:ncpus={expected}:compute_node=tbd'


def test_chunk_types_override_job_type_and_preserve_alternatives(submit):
    result, _ = submit(
        {
            'select': '1:instance_type=c5.xlarge+c5.large+2:mem=8gb',
            'instance_type': 'c5n.18xlarge',
        }
    )
    assert result.accept is True, result.formatted_user_message
    assert (
        result.resources_updated['select']
        == '1:instance_type=c5.xlarge+c5.large:ncpus=1:compute_node=tbd+2:mem=8gb:ncpus=36:compute_node=tbd'
    )


@pytest.mark.parametrize('select', ['1:ncpus=37+2:mem=8gb', '1:mem=8gb+2:ncpus=37'])
def test_oversized_chunk_still_rejects(submit, select):
    result, _ = submit({'select': select})
    assert result.accept is False
    assert '- c5n.18xlarge has 36 cores' in result.formatted_user_message


@pytest.mark.parametrize(
    'queue,select', [('job-shared', '1:mem=8gb'), ('job-shared', '1:ncpus=4+2:mem=8gb')]
)
def test_job_shared_never_fills_cpus(submit, queue, select):
    result, _ = submit({'select': select}, queue=queue)
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['select'] == select_compute_node(select, 'tbd')


@pytest.mark.parametrize(
    'resources,expected',
    [
        ({}, '1:ncpus=36:compute_node=tbd'),
        ({'nodes': '2', 'ncpus': '4'}, '2:ncpus=4:compute_node=tbd'),
    ],
)
def test_no_select(submit, resources, expected):
    result, _ = submit(resources)
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['select'] == expected


@pytest.mark.parametrize('failure', ['missing', 'exception', 'partial', 'empty_count'])
def test_unresolved_sizing_leaves_select_unchanged(
    submit, context, monkeypatch, failure
):
    log = Mock()
    original_lookup = context.aws_util().get_ec2_instance_type

    def after_build(hook):
        monkeypatch.setattr(context, 'logger', lambda *args: log)
        if failure == 'missing':
            hook._queue_profile.default_job_params.instance_types = None
        else:

            def lookup(instance_type):
                if failure == 'partial' and instance_type == 'c5.xlarge':
                    return original_lookup(instance_type)
                if failure == 'empty_count':
                    info = original_lookup(instance_type)
                    return info.model_copy(update={'vcpu_info_default_cores': None})
                raise RuntimeError('cache unavailable')

            monkeypatch.setattr(context.aws_util(), 'get_ec2_instance_type', lookup)

    resources = {'select': '1:ncpus=4+2:mem=8gb'}
    defaults = (
        SocaJobParams(instance_types=['c5.xlarge', 'c5n.18xlarge'])
        if failure == 'partial'
        else None
    )
    if failure == 'partial':
        resources['select'] = '1:ncpus=1+2:mem=8gb'
    result, _ = submit(resources, defaults=defaults, before_response=after_build)
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['select'] == select_compute_node(
        resources['select'], 'tbd'
    )
    log.warning.assert_called_once()
    assert 'CPU sizing skipped' in log.warning.call_args.args[0]


def test_compatibility_instance_type_endpoint(context):
    api = OpenPBSAPI.__new__(OpenPBSAPI)
    api.context = context
    api.logger = Mock()
    request = Mock(namespace='OpenPBSHook.InstanceTypeInfo')
    request.is_administrator.return_value = True
    request.get_request.return_value = {'payload': {'instance_type': 'g5.xlarge'}}
    api.invoke(request)
    request.success.assert_called_once_with(
        {'instance_type': 'g5.xlarge', 'vcpus': 4, 'cores': 2, 'threads_per_core': 2}
    )
    request.get_request.return_value = {'payload': {'instance_type': 'nope.large'}}
    with pytest.raises(SocaException) as exc:
        api.invoke(request)
    assert exc.value.error_code == errorcodes.INVALID_EC2_INSTANCE_TYPE


@pytest.mark.parametrize(
    'resources,expected',
    [
        (
            {'select': '1:mem=8gb', 'enable_ht_support': 'true'},
            '1:mem=8gb:ncpus=72:compute_node=tbd',
        ),
        (
            {'select': '1:mem=8gb', 'enable_ht_support': 'false', 'ht_support': 'true'},
            '1:mem=8gb:ncpus=72:compute_node=tbd',
        ),
        (
            {'select': '1:enable_ht_support=true+2:mem=8gb'},
            '1:enable_ht_support=true:ncpus=72:compute_node=tbd+2:mem=8gb:ncpus=36:compute_node=tbd',
        ),
    ],
)
def test_enable_ht_support_sizes_like_ht_support(submit, resources, expected):
    # the parameter builder accepts both names and provisions the node with HT on either;
    # sizing by one name only would give such a job half the node
    result, _ = submit(resources)
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['select'] == expected


def test_chunk_ht_does_not_leak_into_other_chunks(submit):
    result, _ = submit({'select': '1:ht_support=true+2:mem=8gb'})
    assert result.accept is True, result.formatted_user_message
    assert (
        result.resources_updated['select']
        == '1:ht_support=true:ncpus=72:compute_node=tbd+2:mem=8gb:ncpus=36:compute_node=tbd'
    )


@pytest.mark.parametrize(
    'select,accepted',
    [
        (
            '1:instance_type=c5.large:ncpus=1+2:instance_type=c5n.18xlarge:ncpus=36',
            True,
        ),
        (
            '1:instance_type=c5.large:ncpus=2+2:instance_type=c5n.18xlarge:ncpus=36',
            False,
        ),
        (
            '1:instance_type=c5n.18xlarge:ncpus=36+2:instance_type=c5.large:ncpus=2',
            False,
        ),
    ],
)
def test_each_explicit_chunk_uses_its_own_cpu_limit(submit, select, accepted):
    result, _ = submit({'select': select})
    assert result.accept is accepted, result.formatted_user_message
    if not accepted:
        assert 'c5.large has 1 cores' in result.formatted_user_message


@pytest.mark.parametrize('event_type', ['modifyjob', 'movejob'])
def test_sizing_only_runs_on_queuejob(submit, event_type):
    result, _ = submit({'select': '1:mem=8gb'}, event_type=event_type)
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['select'] == '1:mem=8gb:compute_node=tbd'


@pytest.mark.parametrize(
    'option,queue_settings',
    [
        ({'terminate_when_idle': '5'}, {'terminate_when_idle': 10}),
        ({'keep_forever': 'true'}, {'keep_forever': True}),
    ],
)
def test_shared_capacity_keeps_requested_resources(submit, option, queue_settings):
    # a job may ask for shared capacity only on a queue set up for it
    result, hook = submit(
        {'select': '1:ncpus=4+2:mem=8gb', **option}, queue_settings=queue_settings
    )
    assert result.accept is True, result.formatted_user_message
    assert hook.job.is_shared_capacity()
    compute_node = hook.job.get_compute_stack() if 'keep_forever' in option else 'tbd'
    assert (
        result.resources_updated['select']
        == f'1:ncpus=4:compute_node={compute_node}+2:mem=8gb:compute_node={compute_node}'
    )


def test_provision_and_reset_preserve_chunks(submit):
    from ideascheduler.app.scheduler.openpbs.openpbs_scheduler import OpenPBSScheduler

    result, hook = submit({'select': '1:ncpus=4+2:mem=8gb'})
    assert result.accept is True
    job = hook.job
    job.params.custom_params['select'] = result.resources_updated['select']
    scheduler = OpenPBSScheduler.__new__(OpenPBSScheduler)
    scheduler.set_job_attributes = Mock(return_value=True)
    scheduler.get_job = Mock(return_value=job)
    scheduler.provision_job(job, 'stack-id')
    provisioned = scheduler.set_job_attributes.call_args.kwargs['attributes']['select']
    assert (
        provisioned
        == f'1:ncpus=4:compute_node={job.get_compute_stack()}+2:mem=8gb:ncpus=36:compute_node={job.get_compute_stack()}'
    )
    job.params.custom_params['select'] = provisioned
    assert scheduler.reset_job(job.job_id)
    assert (
        scheduler.set_job_attributes.call_args.kwargs['attributes']['select']
        == result.resources_updated['select']
    )


def test_terminate_when_idle_above_the_queue_is_refused(submit):
    result, _ = submit(
        {'select': '1:ncpus=4', 'terminate_when_idle': '10'},
        queue_settings={'terminate_when_idle': 5},
    )
    assert result.accept is False
    assert 'can be at most 5' in result.formatted_user_message


def test_submission_pins_the_queue_default_existing_fsx_on_the_job(submit):
    from ideadatamodel import SocaFSxLustreConfig

    dns = 'fs-0123456789abcdef0.fsx.us-east-2.amazonaws.com'
    result, hook = submit(
        {'select': '1:ncpus=4'},
        defaults=SocaJobParams(
            instance_types=['c5n.18xlarge'],
            scratch_provider='fsx-lustre-existing',
            fsx_lustre=SocaFSxLustreConfig(enabled=True, existing_fsx=dns),
        ),
    )
    assert result.accept is True, result.formatted_user_message
    # written to the job, so a later read shows and prices what the job mounts
    assert result.resources_updated['fsx_lustre'] == dns


def test_submission_without_fsx_writes_none(submit):
    result, _ = submit({'select': '1:ncpus=4'})
    assert result.accept is True, result.formatted_user_message
    assert 'fsx_lustre' not in result.resources_updated


def test_a_supplied_job_uid_that_belongs_to_another_job_is_refused(
    submit, context, monkeypatch
):
    # SCH-09: a later job's completion record would replace the earlier job's
    taken = {'uid-of-another-job'}
    monkeypatch.setattr(
        context,
        'job_cache',
        SimpleNamespace(is_job_uid_taken=lambda uid: uid in taken),
        raising=False,
    )
    result, _ = submit({'select': '1:ncpus=4', 'job_uid': 'uid-of-another-job'})
    assert result.accept is False
    assert 'already belongs to another job' in result.formatted_user_message
    result, _ = submit({'select': '1:ncpus=4', 'job_uid': 'uid-free'})
    assert result.accept is True, result.formatted_user_message


def test_a_dry_run_on_a_keep_forever_queue_queues_nothing(submit, context):
    # SCH-07: the keep-forever branch accepted before the dry-run branch was reached
    result, hook = submit(
        {'select': '1:ncpus=4', 'dry_run': 'json:bom'},
        queue_settings={'keep_forever': True, 'stack_uuid': 'stack-1'},
    )
    assert result.accept is False
    assert hook.job_submission_result.accepted is False
    context.job_monitor.job_queued.assert_not_called()


def test_a_keep_forever_submission_is_recorded_as_accepted(submit):
    # SCH-08: unset accepted rolled back the dedupe key, so a retry queued a second copy
    result, hook = submit(
        {'select': '1:ncpus=4'},
        queue_settings={'keep_forever': True, 'stack_uuid': 'stack-1'},
    )
    assert result.accept is True, result.formatted_user_message
    assert hook.job_submission_result.accepted is True


@pytest.mark.parametrize(
    'pool_profile,accepted',
    [(None, True), ('arn:aws:iam::1:instance-profile/other-project', False)],
)
def test_a_keep_forever_job_is_refused_on_a_pool_of_another_instance_profile(
    submit, context, monkeypatch, pool_profile, accepted
):
    # SCH-03: the job ran under the instance profile the pool was provisioned with
    from ideadatamodel import constants
    from ideadatamodel.aws import CloudFormationStack

    tags = (
        []
        if pool_profile is None
        else [{'Key': constants.IDEA_TAG_INSTANCE_PROFILE, 'Value': pool_profile}]
    )
    monkeypatch.setattr(
        type(context.aws_util()),
        'cloudformation_describe_stack',
        lambda self, stack_name: CloudFormationStack(entry={'Tags': tags}),
    )
    result, _ = submit(
        {'select': '1:ncpus=4'},
        queue_settings={'keep_forever': True, 'stack_uuid': 'stack-1'},
    )
    assert result.accept is accepted, result.formatted_user_message
    if not accepted:
        assert 'another instance profile' in result.formatted_user_message


@pytest.mark.parametrize(
    'recorded_profile,accepted',
    [
        ('arn:aws:iam::1:instance-profile/other-project', False),
        (None, True),
        ('unreadable', True),
    ],
)
def test_a_pool_built_before_the_tag_is_checked_against_its_recorded_job(
    submit, context, monkeypatch, recorded_profile, accepted
):
    # verify-scheduler #1 (SCH-03 residual): a keep-forever pool built by an earlier
    # release has no instance-profile tag; the profile is read from the job recorded in
    # the pool's template
    from ideadatamodel import SocaJob
    from ideadatamodel.aws import CloudFormationStack

    monkeypatch.setattr(
        type(context.aws_util()),
        'cloudformation_describe_stack',
        lambda self, stack_name: CloudFormationStack(entry={'Tags': []}),
    )

    def recorded(self, stack_name):
        if recorded_profile == 'unreadable':
            raise RuntimeError('template not read')
        return SocaJob(params=SocaJobParams(instance_profile=recorded_profile))

    monkeypatch.setattr(type(context.aws_util()), 'get_soca_job_from_stack', recorded)
    result, _ = submit(
        {'select': '1:ncpus=4'},
        queue_settings={'keep_forever': True, 'stack_uuid': 'stack-1'},
    )
    assert result.accept is accepted, result.formatted_user_message
    if not accepted:
        assert 'another instance profile' in result.formatted_user_message


def test_a_keep_forever_job_on_its_own_pool_is_accepted(submit, context, monkeypatch):
    from ideadatamodel import constants
    from ideadatamodel.aws import CloudFormationStack

    seen = {}

    def describe(self, stack_name):
        return CloudFormationStack(
            entry={
                'Tags': [
                    {
                        'Key': constants.IDEA_TAG_INSTANCE_PROFILE,
                        'Value': seen['profile'],
                    }
                ]
            }
        )

    def capture(hook):
        seen['profile'] = hook.job.params.instance_profile

    monkeypatch.setattr(
        type(context.aws_util()), 'cloudformation_describe_stack', describe
    )
    result, _ = submit(
        {'select': '1:ncpus=4'},
        queue_settings={'keep_forever': True, 'stack_uuid': 'stack-1'},
        before_response=capture,
    )
    assert result.accept is True, result.formatted_user_message


def test_a_job_naming_another_cluster_gets_this_clusters_name(submit, context):
    # SJP-09: the job's cluster_name resource named its stack and tag
    result, hook = submit({'select': '1:ncpus=4', 'cluster_name': 'other-cluster'})
    assert result.accept is True, result.formatted_user_message
    assert hook.job.cluster_name == context.cluster_name()
    assert result.resources_updated['cluster_name'] == context.cluster_name()


def test_an_edit_of_a_bound_job_keeps_its_selector_and_stack(submit, context):
    # PBS-13 / SJ-36 / A-S1 / A-S2: every accepted edit wrote compute_node=tbd; a new select
    # lost the node selector; the cache refresh went to the placeholder id
    def bound(hook):
        hook.event.job_o = OpenPBSJob(
            id='1234.scheduler',
            queue='normal',
            Resource_List={
                'select': '1:ncpus=4:compute_node=stack-7',
                'stack_id': 'arn:stack-7',
            },
        )

    result, _ = submit(
        {'select': '1:ncpus=2', 'walltime': '00:10:00'},
        event_type='modifyjob',
        before_response=bound,
    )
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['select'] == '1:ncpus=2:compute_node=stack-7'
    assert 'stack_id' not in result.resources_updated
    [call] = context.job_monitor.job_modified.call_args_list
    assert call.kwargs['job'].job_id == '1234'
    context.job_monitor.job_queued.assert_not_called()


def test_an_edit_of_an_unbound_job_is_queued_as_before(submit, context):
    result, _ = submit({'select': '1:ncpus=4'}, event_type='modifyjob')
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['stack_id'] == 'tbd'
    context.job_monitor.job_queued.assert_called_once()


def test_an_edit_of_a_job_carrying_the_queues_pinned_fsx_passes_a_restricted_queue(
    submit,
):
    # A-S3: the DNS name written at submission read back as the job's own fsx_lustre and
    # the queue that restricts it refused the edit
    from ideadatamodel import SocaFSxLustreConfig
    from ideadatamodel.scheduler import SocaQueueManagementParams

    dns = 'fs-0123456789abcdef0.fsx.us-east-2.amazonaws.com'
    result, _ = submit(
        {'select': '1:ncpus=4', 'walltime': '00:10:00'},
        event_type='modifyjob',
        job_o=OpenPBSJob(
            id='55.scheduler',
            queue='normal',
            Resource_List={'select': '1:ncpus=4', 'fsx_lustre': dns},
        ),
        defaults=SocaJobParams(
            instance_types=['c5n.18xlarge'],
            scratch_provider='fsx-lustre-existing',
            fsx_lustre=SocaFSxLustreConfig(enabled=True, existing_fsx=dns),
        ),
        queue_settings={
            'queue_management_params': SocaQueueManagementParams(
                restricted_parameters=['fsx_lustre']
            )
        },
    )
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['fsx_lustre'] == dns


def _restricted_fsx_move(submit, carried, **kwargs):
    from ideadatamodel import SocaFSxLustreConfig
    from ideadatamodel.scheduler import SocaQueueManagementParams

    kwargs.setdefault(
        'defaults',
        SocaJobParams(
            instance_types=['c5n.18xlarge'],
            scratch_provider='fsx-lustre-existing',
            fsx_lustre=SocaFSxLustreConfig(enabled=True, existing_fsx=PINNED_DNS),
        ),
    )
    kwargs.setdefault(
        'queue_settings',
        {
            'queue_management_params': SocaQueueManagementParams(
                restricted_parameters=['fsx_lustre']
            )
        },
    )
    return submit(
        {'select': '1:ncpus=4', 'walltime': '00:10:00', 'fsx_lustre': carried},
        event_type='movejob',
        **kwargs,
    )


PINNED_DNS = 'fs-0123456789abcdef0.fsx.us-east-2.amazonaws.com'


def test_a_move_carrying_the_queues_pinned_fsx_passes_a_restricted_queue(submit):
    # S1: a move carries the whole job in event.job, pinned fsx_lustre included
    result, _ = _restricted_fsx_move(submit, PINNED_DNS, src_queue='normal')
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['fsx_lustre'] == PINNED_DNS


SOURCE_DNS = 'fs-0fedcba9876543210.fsx.us-east-2.amazonaws.com'


def _source_queue():
    from ideadatamodel import SocaFSxLustreConfig

    return Mock(
        default_job_params=SocaJobParams(
            fsx_lustre=SocaFSxLustreConfig(enabled=True, existing_fsx=SOURCE_DNS)
        )
    )


@pytest.mark.parametrize('target_default', [True, False])
def test_a_move_keeping_the_source_queues_fsx_into_a_restricted_queue_is_refused(
    submit, target_default
):
    # rereview3 S2: Resource_List is read-only on movejob, so the target's value (or
    # none) cannot replace the source queue's file system the job carries: the move is
    # refused rather than leaving the job on the source queue's file system
    from ideadatamodel.scheduler import SocaQueueManagementParams

    kwargs = {}
    if not target_default:
        kwargs['defaults'] = SocaJobParams(instance_types=['c5n.18xlarge'])
        kwargs['queue_settings'] = {
            'queue_management_params': SocaQueueManagementParams(
                restricted_parameters=['fsx_lustre']
            )
        }
    result, _ = _restricted_fsx_move(
        submit, SOURCE_DNS, src_queue='old-q', source_profile=_source_queue(), **kwargs
    )
    assert result.accept is False
    assert (
        f'uses the FSx for Lustre file system {SOURCE_DNS}, the default of queue old-q'
        in result.formatted_user_message
    )
    assert 'Submit the job to queue normal' in result.formatted_user_message


def test_a_move_keeping_the_source_queues_fsx_into_an_open_queue_passes(submit):
    from ideadatamodel.scheduler import SocaQueueManagementParams

    result, _ = _restricted_fsx_move(
        submit,
        SOURCE_DNS,
        src_queue='old-q',
        source_profile=_source_queue(),
        queue_settings={'queue_management_params': SocaQueueManagementParams()},
    )
    assert result.accept is True, result.formatted_user_message


def test_a_move_naming_another_fsx_is_still_refused_on_a_restricted_queue(submit):
    other = 'fs-0aaaaaaaaaaaaaaaa.fsx.us-east-2.amazonaws.com'
    result, _ = _restricted_fsx_move(submit, other, src_queue='normal')
    assert result.accept is False
    assert 'fsx_lustre' in result.formatted_user_message
