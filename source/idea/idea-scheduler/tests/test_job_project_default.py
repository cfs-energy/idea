"""
A job with no project must not be charged to the queue profile's first project.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
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
    _use_queue(context, monkeypatch, _projects(*names))
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
        'project-a on queue: normal. you can use: project-b, project-c'
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


def test_queuejob_persists_selected_project(context, monkeypatch):
    _use_queue(context, monkeypatch, _projects('project-a', 'project-b'))
    monkeypatch.setattr(
        context.projects_client,
        'get_user_projects',
        lambda username: _projects('project-b'),
    )
    monkeypatch.setattr(context, 'job_monitor', Mock())
    hook = _hook(context, project=None)
    # Exercise admission and the PBS response consumer, isolating cost/instance checks.
    monkeypatch.setattr(hook, 'is_valid', lambda: True)
    monkeypatch.setattr(hook, 'check_incidentals', Mock())
    monkeypatch.setattr(hook, 'get_bom_cost', Mock())
    monkeypatch.setattr(hook, 'get_budget_usage', Mock())
    result = _validate_hook(context, monkeypatch, hook)
    assert result.accept is True
    assert result.project == 'project-b'

    event = SimpleNamespace(
        type='queuejob',
        job=SimpleNamespace(project=None, Resource_List={}),
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
            'PRE_EXECUTION_HOOKS': ['queuejob'],
            'HOOK_EVENT_QUEUEJOB': 'queuejob',
            'invoke_soca_scheduler': lambda e: {
                'success': True,
                'payload': result.model_dump(),
            },
            'time': time,
            'start_time': time.time() * 1000,
        },
    )
    assert event.job.project == 'project-b'
    event.accept.assert_called_once()
    event.reject.assert_not_called()
