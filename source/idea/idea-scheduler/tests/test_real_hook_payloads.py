"""
Hook requests captured on the development cluster, run through the scheduler.

the fixtures are the request JSON the batch system hook sent, from the PBS server logs
of the dev proofs (usernames, cluster and account scrubbed). runjob_provisioned is a real
runjob request (2026-11-10, when the hook still sent an empty Resource_List) with the
Resource_List a whole read sends for a provisioned job: bound job 587's, from the movejob
request of lane H run 2. once the hook sent a provisioned job's whole request, its
provisioning_time (epoch milliseconds as text) failed to parse and every status update of a
provisioned job was dropped ("failed to process job status hook").
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from ideadatamodel import HpcQueueProfile, Project, SocaJobParams
from ideascheduler.app.api.opepbs_api import OpenPBSAPI
from ideascheduler.app.scheduler.openpbs.openpbs_api import (
    OpenPBSHookRequest,
    OpenPBSHookResult,
)
from ideascheduler.app.scheduler.openpbs.openpbs_api_invocation_context import (
    OpenPBSAPIInvocationContext,
)
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSEvent

FIXTURES = Path(__file__).parent / 'fixtures' / 'pbs_hook_requests'
NAMES = sorted(path.stem for path in FIXTURES.glob('*.json'))


def request(name):
    text = (FIXTURES / f'{name}.json').read_text()
    # the test environment's instance type catalog has no m5.large (the owner's edit)
    return json.loads(text.replace('m5.large', 'c5.xlarge'))


def event(name):
    return OpenPBSEvent(**request(name)['payload']['event'])


@pytest.fixture
def profile(context, monkeypatch):
    profile = HpcQueueProfile(
        name='test',
        enabled=True,
        queues=['test', 'job-shared'],
        default_job_params=SocaJobParams(instance_types=['c5.large']),
    )
    profiles = Mock()
    profiles.get_queue_profile.return_value = profile
    monkeypatch.setattr(context, 'queue_profiles', profiles)
    return profile


def test_the_fixtures_are_the_captured_events():
    assert NAMES == [
        'modifyjob',
        'movejob_bound',
        'movejob_unbound',
        'queuejob',
        'runjob_provisioned',
    ]


@pytest.mark.parametrize('name', NAMES)
def test_every_captured_job_reads_into_a_job(context, profile, name):
    job = event(name).as_soca_job(context=context, queue_profile=profile)
    assert job is not None
    if name in ('movejob_bound', 'runjob_provisioned'):
        assert job.provisioned is True
        assert job.provisioning_time == datetime.fromtimestamp(
            1795131652765 / 1000, tz=job.provisioning_time.tzinfo
        )


def test_a_status_event_of_a_provisioned_job_updates_the_job(context, profile):
    api = OpenPBSAPI.__new__(OpenPBSAPI)
    api.context = context
    api.logger = Mock()
    monitor = Mock()
    context.job_monitor = monitor
    api_context = Mock(context=context)
    api_context.get_request_payload_as.return_value = OpenPBSHookRequest(
        event=event('runjob_provisioned')
    )
    api.hook_job_status(OpenPBSAPIInvocationContext(api_context))

    api.logger.exception.assert_not_called()
    [call] = monitor.job_running.call_args_list
    assert call.kwargs['job'].job_id == '401'
    assert call.kwargs['job'].provisioned is True


@pytest.mark.parametrize(
    'value, expected',
    [
        ('1795131652765', 1795131652.765),
        (1795131652765, 1795131652.765),
        (' 1795131652765 ', 1795131652.765),
        ('0', None),
        ('', None),
        (None, None),
    ],
)
def test_provisioning_time_reads_in_every_form_pbs_returns(value, expected):
    from ideascheduler.app.scheduler.openpbs.openpbs_model import epoch_millis_time

    parsed = epoch_millis_time(value)
    assert (parsed.timestamp() if parsed else None) == expected


def test_a_field_that_does_not_parse_does_not_drop_the_update(context, profile, caplog):
    # an attribute a status update does not need is logged and left out
    import logging

    broken = request('runjob_provisioned')['payload']['event']
    broken['job']['qtime'] = 'not a time'
    broken['job']['Resource_List']['provisioning_time'] = 'not a time either'
    with caplog.at_level(logging.WARNING):
        job = OpenPBSEvent(**broken).as_soca_job(context=context, queue_profile=profile)
    assert job.job_id == '401'
    assert job.queue_time is None and job.provisioning_time is None
    logged = ' '.join(record.getMessage() for record in caplog.records)
    assert 'qtime not read' in logged and 'provisioning_time not read' in logged


@pytest.mark.parametrize(
    'name', ['queuejob', 'modifyjob', 'movejob_unbound', 'movejob_bound']
)
def test_every_captured_submission_event_gets_an_answer_not_an_internal_error(
    context, profile, monkeypatch, name
):
    from test_job_project_default import _validate_hook
    from ideascheduler.app.job_messages import JOB_INTERNAL_ERROR_MESSAGE

    monkeypatch.setattr(context, 'job_monitor', Mock())
    monkeypatch.setattr(
        context,
        'job_cache',
        Mock(is_job_uid_taken=lambda job_uid: False, get_job=lambda job_id: None),
        raising=False,
    )
    monkeypatch.setattr(
        type(context.aws_util()),
        'cloudformation_describe_stack',
        lambda self, stack_name: None,
    )
    monkeypatch.setattr(
        context.projects_client,
        'get_user_projects',
        lambda username: [Project(name='default', project_id='p')],
        raising=False,
    )
    api_context = Mock(context=context)
    api_context.get_request_payload_as.return_value = OpenPBSHookRequest(
        event=event(name)
    )
    hook = OpenPBSAPIInvocationContext(api_context)
    monkeypatch.setattr(hook, 'check_incidentals', Mock())
    monkeypatch.setattr(hook, 'get_bom_cost', Mock())
    monkeypatch.setattr(hook, 'get_budget_usage', Mock())
    # the captured submission is a dry run; its price table needs the pricing service
    monkeypatch.setattr(
        hook,
        'dry_run_post_processing',
        lambda: OpenPBSHookResult(accept=False, formatted_user_message='dry run'),
    )
    result = _validate_hook(context, monkeypatch, hook)
    message = result.formatted_user_message or ''
    assert JOB_INTERNAL_ERROR_MESSAGE not in message
    assert 'not found' not in message
    if name == 'movejob_bound':
        # job 587, bound to a node of queue test, moved to job-shared
        assert result.accept is False
        assert 'This job already has a node from queue test' in message


def test_a_bound_move_gets_exactly_the_one_guidance_line(context, profile, monkeypatch):
    # live run 3 (N-R3-1): the refusal also listed the destination's EFA reason and a
    # compute_stack pattern error that read as internal
    from test_job_project_default import _validate_hook

    monkeypatch.setattr(context, 'cluster_name', lambda: 'idea-test')
    monkeypatch.setattr(context, 'job_monitor', Mock())
    profile.default_job_params = SocaJobParams(
        instance_types=['c5n.18xlarge'], enable_efa_support=True
    )
    api_context = Mock(context=context)
    api_context.get_request_payload_as.return_value = OpenPBSHookRequest(
        event=event('movejob_bound')
    )
    hook = OpenPBSAPIInvocationContext(api_context)
    result = _validate_hook(context, monkeypatch, hook)
    assert result.accept is False
    lines = [
        line[2:]
        for line in result.formatted_user_message.splitlines()
        if line.startswith('- ')
    ]
    assert lines == [
        'This job already has a node from queue test, so it cannot be moved to queue '
        'job-shared. Delete it and submit it to queue job-shared, or move a job before '
        'it is given a node.'
    ]
