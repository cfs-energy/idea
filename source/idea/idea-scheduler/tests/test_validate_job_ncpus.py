"""CPU sizing runs with real queue defaults and CPU validation, without AWS."""

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
    def run(
        resources,
        *,
        defaults=None,
        queue='normal',
        event_type='queuejob',
        before_response=None,
    ):
        _use_queue(context, monkeypatch, _projects('project-a'))
        profile = context.queue_profiles.get_queue_profile.return_value
        profile.default_job_params = defaults or SocaJobParams(
            instance_types=['c5n.18xlarge']
        )
        monkeypatch.setattr(context, 'job_monitor', Mock())
        event = OpenPBSEvent(
            type=event_type,
            requestor='researcher',
            job=OpenPBSJob(
                queue=queue,
                project='project-a',
                Job_Name='job',
                Resource_List=resources,
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
    assert '[cpus]' in result.formatted_user_message
    assert 'c5n.18xlarge has 36 cores' in result.formatted_user_message


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
    'option', [{'terminate_when_idle': '10'}, {'keep_forever': 'true'}]
)
def test_shared_capacity_keeps_requested_resources(submit, option):
    result, hook = submit({'select': '1:ncpus=4+2:mem=8gb', **option})
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
