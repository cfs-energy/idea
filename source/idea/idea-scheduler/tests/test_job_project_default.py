"""
A job with no project must not be charged to the queue profile's first project.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call
import time

import pytest

from ideadatamodel import (
    HpcQueueProfile,
    Project,
    SocaJob,
    SocaJobParams,
    exceptions,
    errorcodes,
    GetUserResult,
    User,
)
from ideascheduler.app.api.opepbs_api import OpenPBSAPI
from ideascheduler.app.provisioning import JobProvisioningUtil
from ideascheduler.app.scheduler.openpbs.openpbs_api import OpenPBSHookRequest
from ideascheduler.app.scheduler.openpbs.openpbs_api_invocation_context import (
    OpenPBSAPIInvocationContext,
)
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSEvent, OpenPBSJob


def _queue_profile(projects):
    return HpcQueueProfile(
        name='normal',
        enabled=True,
        queues=['normal'],
        projects=projects,
    )


def _projects(*names):
    return [Project(project_id=f'id-{name}', name=name) for name in names]


def _use_queue(context, monkeypatch, projects):
    profiles = Mock()
    profiles.get_queue_profile.return_value = _queue_profile(projects)
    monkeypatch.setattr(context, 'queue_profiles', profiles)


def _hook(app_context, project, requestor='researcher'):
    event = OpenPBSEvent(
        type='queuejob',
        requestor=requestor,
        job=OpenPBSJob(
            queue='normal',
            project=project,
            Job_Name='job',
            Resource_List={
                'select': '1:ncpus=1',
                'instance_type': 't3.micro',
                'nodes': '1',
            },
        ),
    )
    api_context = Mock()
    api_context.context = app_context
    api_context.get_request_payload_as.return_value = OpenPBSHookRequest(event=event)
    return OpenPBSAPIInvocationContext(api_context)


def _validate_hook(context, monkeypatch, hook):
    monkeypatch.setattr(context, 'is_ready', lambda: True)
    monkeypatch.setattr(context, 'job_submission_tracker', Mock())

    def get_user(request):
        if request.username is None:
            raise exceptions.invalid_params('username is required')
        return GetUserResult(user=User(enabled=True))

    context.accounts_client.get_user.side_effect = get_user
    api = OpenPBSAPI.__new__(OpenPBSAPI)
    api.context = context
    api.logger = Mock()
    api.hook_validate_job(hook)
    return hook.api_context.success.call_args.args[0]


def test_missing_project_uses_first_membership(context, monkeypatch):
    """
    no -P, and the user belongs only to the second project on the queue:
    the job uses that project
    """
    _use_queue(context, monkeypatch, _projects('project-a', 'project-b'))
    monkeypatch.setattr(
        context.projects_client,
        'get_user_projects',
        lambda username: _projects('project-b'),
    )

    hook = _hook(context, project=None)
    hook.build_and_validate_job()

    assert hook.job.project == 'project-b'
    assert hook.event.job.project == 'project-b'


def test_missing_project_rejects_when_user_belongs_to_none(context, monkeypatch):
    """
    no -P, and the user belongs to none of the queue's projects
    """
    names = [f'project-{index}' for index in range(1, 13)]
    projects = _projects(*names)
    _use_queue(
        context,
        monkeypatch,
        [Project(project_id=project.project_id) for project in projects],
    )
    lookup = Mock(side_effect=projects)
    monkeypatch.setattr(context.projects_client, 'get_project_by_id', lookup)
    monkeypatch.setattr(
        context.projects_client,
        'get_user_projects',
        lambda username: [],
    )

    hook = _hook(context, project=None)
    result = _validate_hook(context, monkeypatch, hook)

    assert result.accept is False
    message = result.formatted_user_message
    assert (
        'No project given (add `#PBS -P <project>` or `-P <project>`). '
        'Your projects allowed on queue normal: none; '
        'ask an admin to add you to one of: '
    ) in message
    assert 'project-1' in message
    assert 'project-10' in message
    assert 'project-11' not in message
    assert 'project-12' not in message
    assert 'id-project-' not in message
    assert lookup.call_args_list == [
        call(project_id=project.project_id) for project in projects[:10]
    ]


@pytest.mark.parametrize('failure', [RuntimeError('unavailable'), None, Project()])
def test_missing_project_name_lookup_failure_falls_back_per_entry(
    context, monkeypatch, failure
):
    _use_queue(
        context,
        monkeypatch,
        [Project(project_id=f'id-project-{letter}') for letter in 'abc'],
    )
    monkeypatch.setattr(context.projects_client, 'get_user_projects', lambda **_: [])
    lookup = Mock(
        side_effect=[_projects('project-a')[0], failure, _projects('project-c')[0]]
    )
    monkeypatch.setattr(context.projects_client, 'get_project_by_id', lookup)

    result = _validate_hook(context, monkeypatch, _hook(context, project=None))

    assert result.accept is False
    assert (
        'ask an admin to add you to one of: project-a, id-project-b, project-c'
    ) in result.formatted_user_message


@pytest.mark.parametrize('has_name', [False, True])
def test_missing_project_does_not_suggest_disabled_projects(
    context, monkeypatch, has_name
):
    projects = _projects('project-a', 'project-b')
    projects[0].enabled = False
    projects[1].enabled = True
    _use_queue(
        context,
        monkeypatch,
        projects if has_name else [Project(project_id=p.project_id) for p in projects],
    )
    monkeypatch.setattr(context.projects_client, 'get_user_projects', lambda **_: [])
    monkeypatch.setattr(
        context.projects_client, 'get_project_by_id', Mock(side_effect=projects)
    )

    result = _validate_hook(context, monkeypatch, _hook(context, project=None))

    assert result.accept is False
    assert 'project-a' not in result.formatted_user_message
    assert (
        'ask an admin to add you to one of: project-b' in result.formatted_user_message
    )


def test_unauthorized_project_lists_projects_the_user_can_use(context, monkeypatch):
    """
    -P names a project the user is not in; the rejection lists the ones they can use
    """
    _use_queue(context, monkeypatch, _projects('project-a', 'project-b', 'project-c'))
    monkeypatch.setattr(
        context.projects_client,
        'get_user_projects',
        lambda username: _projects('project-b', 'project-c'),
    )
    job = SocaJob(
        owner='researcher',
        project='project-a',
        queue='normal',
        job_id='1',
        params=SocaJobParams(nodes=1, cpus=1),
    )
    util = JobProvisioningUtil(context=context, jobs=[job])

    with pytest.raises(exceptions.SocaException) as exc_info:
        util.check_acls()

    assert (
        'User: researcher is not authorized to submit jobs for project: '
        'project-a on queue: normal. You can use: project-b, project-c'
    ) in exc_info.value.message


@pytest.mark.parametrize('project', [None, 'project-b'])
def test_project_membership_uses_ids_without_name_lookups(
    context, monkeypatch, project
):
    _use_queue(
        context,
        monkeypatch,
        [Project(project_id='id-project-a'), Project(project_id='id-project-b')],
    )
    memberships = Mock(return_value=_projects('project-b', 'project-a'))
    monkeypatch.setattr(context.projects_client, 'get_user_projects', memberships)

    def lookup_project(project_id):
        if project_id == 'id-project-a':
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR, message='lookup unavailable'
            )
        return _projects('project-b')[0]

    lookup = Mock(side_effect=lookup_project)
    monkeypatch.setattr(context.projects_client, 'get_project_by_id', lookup)
    hook = _hook(context, project=project)
    hook.build_and_validate_job()

    assert hook.job.project == (project or 'project-a')
    lookup.assert_not_called()
    if project is None:
        memberships.assert_called_once_with(username='researcher')
    else:
        memberships.assert_not_called()


def test_membership_lookup_failure_rejects_clearly(context, monkeypatch):
    _use_queue(context, monkeypatch, _projects('project-a', 'project-b'))
    monkeypatch.setattr(
        context.projects_client,
        'get_user_projects',
        Mock(
            side_effect=exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR, message='unavailable'
            )
        ),
    )
    hook = _hook(context, project=None)
    result = _validate_hook(context, monkeypatch, hook)
    assert result.accept is False
    assert 'could not read projects' in result.formatted_user_message.lower()
    assert 'none' not in result.formatted_user_message
    assert hook.event.job.project is None


@pytest.mark.parametrize(
    'event_type,changed_project',
    [
        ('queuejob', None),
        ('modifyjob', None),
        ('modifyjob', ''),
        ('modifyjob', 'project-b'),
    ],
)
def test_hook_writes_project_only_when_needed(
    context, monkeypatch, event_type, changed_project
):
    _use_queue(context, monkeypatch, _projects('project-a', 'project-b'))
    monkeypatch.setattr(
        context.projects_client,
        'get_user_projects',
        lambda username: _projects('project-b'),
    )
    monkeypatch.setattr(context, 'job_monitor', Mock())
    hook = _hook(context, project=changed_project)
    hook.event.type = event_type
    if event_type == 'modifyjob':
        hook.event.job_o = OpenPBSJob(project='project-b', Job_Owner='researcher@host')
    # Exercise admission and the PBS response consumer, isolating cost/instance checks.
    monkeypatch.setattr(hook, 'is_valid', lambda: True)
    monkeypatch.setattr(hook, 'check_incidentals', Mock())
    monkeypatch.setattr(hook, 'get_bom_cost', Mock())
    monkeypatch.setattr(hook, 'get_budget_usage', Mock())
    result = _validate_hook(context, monkeypatch, hook)
    assert result.accept is True
    assert result.project == 'project-b'

    writes = []

    class HookJob(SimpleNamespace):
        def __setattr__(self, name, value):
            if name in ('Job_Owner', 'project'):
                writes.append(name)
            super().__setattr__(name, value)

    event = SimpleNamespace(
        type=event_type,
        job=HookJob(project=changed_project, Resource_List={}),
        accept=Mock(),
        reject=Mock(),
    )
    pbs = Mock()
    pbs.event.return_value = event
    handler = (
        Path(__file__).parents[1] / 'resources/openpbs/hooks/openpbs_hook_handler.py'
    )
    # Run the actual hook entry point with its transport stubbed out.
    entrypoint = handler.read_text().split('\ne = pbs.event()\n', 1)[1]
    exec(
        compile(entrypoint, str(handler), 'exec'),
        {
            'e': event,
            'pbs': pbs,
            'is_applicable': lambda e: True,
            'PRE_EXECUTION_HOOKS': ['queuejob', 'modifyjob'],
            'HOOK_EVENT_QUEUEJOB': 'queuejob',
            'HOOK_EVENT_MODIFYJOB': 'modifyjob',
            'invoke_soca_scheduler': lambda e: {
                'success': True,
                'payload': result.model_dump(),
            },
            'time': time,
            'start_time': time.time() * 1000,
        },
    )
    expected_writes = ['project'] if event_type == 'queuejob' or changed_project else []
    assert writes == expected_writes
    assert event.job.project == ('project-b' if expected_writes else changed_project)
    event.accept.assert_called_once()
    event.reject.assert_not_called()


@pytest.mark.parametrize(
    'changed_project,memberships,accepted',
    [
        ('project-a', ['project-b'], False),
        ('project-a', ['project-a', 'project-b'], True),
        (None, ['project-b'], True),
        ('', ['project-b'], True),
    ],
)
def test_modifyjob_validates_requested_project_without_writing_event(
    context, monkeypatch, changed_project, memberships, accepted
):
    _use_queue(context, monkeypatch, _projects('project-a', 'project-b'))
    lookup = Mock(return_value=_projects(*memberships))
    monkeypatch.setattr(context.projects_client, 'get_user_projects', lookup)
    monkeypatch.setattr(context, 'job_monitor', Mock())
    # Keep real admission and ACL checks; isolate cost and capacity checks.
    for method in (
        'check_budgets',
        'check_bedrock',
        'check_reserved_instance_usage',
        'ec2_dry_run',
        'ec2_dry_run_cached',
    ):
        monkeypatch.setattr(JobProvisioningUtil, method, Mock())
    monkeypatch.setattr(
        JobProvisioningUtil,
        'check_service_quota',
        Mock(return_value=SimpleNamespace(quotas=[])),
    )
    hook = _hook(context, project=changed_project, requestor='operator')
    hook.event.type = 'modifyjob'
    hook.event.job_o = OpenPBSJob(
        queue='normal',
        project='project-b',
        Job_Owner='researcher@submit-host',
        Resource_List={
            'select': '1:ncpus=1',
            'instance_type': 't3.micro',
            'nodes': '1',
        },
    )
    hook.event.job.Resource_List = {'walltime': '01:00:00'}
    monkeypatch.setattr(hook, 'get_bom_cost', Mock())
    monkeypatch.setattr(hook, 'get_budget_usage', Mock())
    default_project = Mock(side_effect=AssertionError('modifyjob must not default'))
    monkeypatch.setattr(hook, '_project_when_omitted', default_project)
    writes = []
    original_setattr = OpenPBSJob.__setattr__

    def record_write(job, name, value):
        if name in ('Job_Owner', 'project'):
            writes.append(name)
        original_setattr(job, name, value)

    monkeypatch.setattr(OpenPBSJob, '__setattr__', record_write)
    result = _validate_hook(context, monkeypatch, hook)

    assert result.accept is accepted, result.formatted_user_message
    expected_project = changed_project or 'project-b'
    assert hook.job.project == expected_project
    assert hook.job_builder._context.project == expected_project
    assert hook.job.owner == 'researcher'
    lookup.assert_called_once_with(username='researcher')
    if accepted:
        assert result.project == expected_project
    else:
        assert (
            'not authorized to submit jobs for project: project-a'
            in result.formatted_user_message
        )
    assert writes == []
    assert hook.event.job.project == changed_project
    assert hook.event.job.Job_Owner is None
    assert hook.event.job_o.Job_Owner == 'researcher@submit-host'
    default_project.assert_not_called()


@pytest.mark.parametrize(
    'original_owner,changed_owner,requestor,expected_owner',
    [
        ('researcher@host', 'other@host', 'operator', 'researcher'),
        (None, 'researcher@host', 'operator', 'researcher'),
        (None, None, 'operator@host', 'operator'),
    ],
)
def test_modifyjob_resolves_owner_locally(
    context, monkeypatch, original_owner, changed_owner, requestor, expected_owner
):
    _use_queue(context, monkeypatch, _projects('project-b'))
    hook = _hook(context, project=None, requestor=requestor)
    hook.event.type = 'modifyjob'
    hook.event.job_o = OpenPBSJob(project='project-b', Job_Owner=original_owner)
    hook.event.job.Job_Owner = changed_owner
    original_setattr = OpenPBSJob.__setattr__

    def reject_owner_write(job, name, value):
        assert name != 'Job_Owner', 'Job_Owner is read-only'
        original_setattr(job, name, value)

    monkeypatch.setattr(OpenPBSJob, '__setattr__', reject_owner_write)
    hook.build_and_validate_job()

    assert hook.job.owner == expected_owner
    assert hook.event.job.Job_Owner == changed_owner
    assert hook.event.job_o.Job_Owner == original_owner


@pytest.mark.parametrize('original_job', [None, OpenPBSJob(project=None)])
def test_modifyjob_never_defaults_an_absent_project(context, monkeypatch, original_job):
    _use_queue(context, monkeypatch, _projects('project-b'))
    hook = _hook(context, project=None)
    hook.event.type = 'modifyjob'
    hook.event.job_o = original_job
    default_project = Mock(side_effect=AssertionError('modifyjob must not default'))
    monkeypatch.setattr(hook, '_project_when_omitted', default_project)

    hook.build_and_validate_job()

    default_project.assert_not_called()
    assert hook.job.project is None
    assert hook.event.job.project is None


@pytest.mark.parametrize('projects', [[], [Project(name='disabled', enabled=False)]])
def test_missing_project_with_no_enabled_queue_projects(context, monkeypatch, projects):
    _use_queue(context, monkeypatch, projects)
    monkeypatch.setattr(context.projects_client, 'get_user_projects', lambda **_: [])
    result = _validate_hook(context, monkeypatch, _hook(context, project=None))
    assert result.accept is False
    assert 'Queue normal has no enabled projects' in result.formatted_user_message
    assert 'one of:' not in result.formatted_user_message
