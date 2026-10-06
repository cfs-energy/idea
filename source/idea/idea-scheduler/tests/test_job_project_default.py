"""
A job with no project must not be charged to the queue profile's first project.
"""

from unittest.mock import Mock

import pytest

from ideadatamodel import (
    HpcQueueProfile,
    Project,
    SocaJob,
    SocaJobParams,
    exceptions,
)
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


def _validation_messages(hook):
    return [
        entry.message
        for entry in hook.job_validation_result.results
        if entry.message is not None
    ]


def test_missing_project_uses_first_membership(context, monkeypatch):
    """
    no -P, and the user belongs only to the second project on the queue:
    the job uses that project and says so
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
    assert 'no project given, using project-b' in (hook.job.comment or '')


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
    hook.build_and_validate_job()

    message = ' '.join(_validation_messages(hook))
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
