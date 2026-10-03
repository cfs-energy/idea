"""The canary submits a real PBS script and refuses missing or failed evidence."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ideadatamodel import (
    HpcQueueProfile,
    Project,
    SocaComputeNode,
    SocaJob,
    SubmitJobResult,
    exceptions,
)
from ideascheduler.app.images import compute_image_canary as module
from ideascheduler.app.images.compute_image_canary import (
    ComputeImageCanary,
    canary_script,
)
from ideascheduler.app.api.scheduler_api import SchedulerAPI
from test_compute_image_pipeline import service, row


@pytest.mark.parametrize(
    'failure',
    [None, 'ami', 'mount', 'output', 'exit', 'timeout', 'pbs', 'identity', 'cleanup'],
)
def test_canary_uses_validation_identity_and_requires_every_check(monkeypatch, failure):
    svc = service()
    context = svc.context
    context.config().values.update(
        {
            module.PIPELINE_SETTINGS: {
                'validation_user': 'image-test',
                'validation_project': 'image-test',
                'ready_gate_seconds_linux': 0 if failure == 'timeout' else 300,
            },
            'shared-storage.data.mount_dir': '/data',
            'shared-storage': {
                'apps': {'mount_dir': '/apps', 'scope': ['cluster']},
                'data': {'mount_dir': '/data', 'scope': ['cluster']},
                'extra': {
                    'mount_dir': '/extra',
                    'scope': ['module'],
                    'modules': ['scheduler'],
                },
                'desktop': {
                    'mount_dir': '/desktop',
                    'scope': ['module'],
                    'modules': ['virtual-desktop-controller'],
                },
                'theirs': {
                    'mount_dir': '/theirs',
                    'scope': ['project'],
                    'projects': ['someone-else'],
                },
            },
        }
    )
    context.projects_client.get_project_by_name.return_value = Project(
        project_id='project-test', name='image-test'
    )
    context.queue_profiles.list_queue_profiles.return_value = [
        HpcQueueProfile(name='source')
    ]
    created = []

    def create(profile):
        profile.queue_profile_id = 'canary-profile'
        created.append(profile)
        return profile

    context.queue_profiles.create_queue_profile.side_effect = create
    if failure == 'cleanup':
        # a queue that cannot be deleted does not fail an image whose checks passed
        context.queue_profiles.delete_queue_profile.side_effect = (
            exceptions.soca_exception(module.errorcodes.SCHEDULER_ERROR, 'qmgr failed')
        )
    token = 'unique-test-output'
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=token))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    submission = []
    finished = []

    def submit(self, request, job_owner, dry_run):
        script = base64.b64decode(request.job_script).decode()
        submission.append((script, request.project, job_owner))
        job = SocaJob(
            job_id='1',
            owner='wrong-user' if failure == 'identity' else job_owner,
            project=request.project,
            queue=created[0].name,
        )
        done = job.model_copy(deep=True)
        done.exit_status = 1 if failure == 'exit' else 0
        # plain qstat refuses a finished job (rc 35); only qstat -x returns it
        context.scheduler.get_job.side_effect = exceptions.soca_exception(
            module.errorcodes.SCHEDULER_JOB_FINISHED, 'Job has finished, use -x or -H'
        )
        context.scheduler.get_finished_job.side_effect = [job, done]
        finished.append(done)
        return SubmitJobResult(accepted=True, job=job)

    monkeypatch.setattr(SchedulerAPI, '_submit_job', submit)
    context.scheduler.list_nodes.return_value = (
        []
        if failure == 'pbs'
        else [SocaComputeNode(host='10.0.0.7', instance_id='i-candidate', jobs=['1'])]
    )
    context.scheduler.is_job_active.return_value = False
    context.aws().ec2().describe_instances = Mock(
        return_value={
            'Reservations': [
                {
                    'Instances': [
                        {
                            'PrivateDnsName': 'compute-host.internal',
                            'ImageId': 'ami-wrong'
                            if failure == 'ami'
                            else 'ami-candidate',
                        }
                    ]
                }
            ]
        }
    )
    evidence = {
        'token': 'wrong' if failure == 'output' else token,
        'host': 'compute-host',
        'checks': [
            {
                'name': 'filesystem:' + name,
                'ok': not (failure == 'mount' and name == 'apps'),
                'detail': 'Filesystem probe passed.'
                if failure != 'mount'
                else 'Filesystem probe failed.',
            }
            for name in ('apps', 'data', 'extra', 'desktop')
        ],
    }
    monkeypatch.setattr(Path, 'read_text', lambda self: json.dumps(evidence))
    monkeypatch.setattr(Path, 'unlink', lambda *args, **kwargs: None)
    candidate = row(image_id='ami-candidate')

    def progress(update):
        for key, value in update.items():
            setattr(candidate, key, value)

    if failure and failure != 'cleanup':
        with pytest.raises(exceptions.SocaException):
            ComputeImageCanary(context).validate(candidate, progress)
        assert any(check.ok is False for check in candidate.checks)
    else:
        ComputeImageCanary(context).validate(candidate, progress)
        assert all(check.ok is True for check in candidate.checks)
        assert {check.name for check in candidate.checks} >= {
            'compute_ami',
            'pbs_registered',
            'compute_job',
            'filesystem:apps',
            'filesystem:data',
            'filesystem:extra',
        }
        # a desktop-module or other project's filesystem is not on the canary node
        assert 'filesystem:desktop' not in {check.name for check in candidate.checks}
        assert 'filesystem:theirs' not in {check.name for check in candidate.checks}
    assert submission[0][1:] == ('image-test', 'image-test')
    assert created[0].default_job_params.instance_ami == 'ami-candidate'
    assert created[0].projects[0].project_id == 'project-test'
    assert created[0].image_pinned is True
    assert len(created[0].name) <= 15
    assert 'os.fsync' in submission[0][0]
    context.scheduler.set_queue_attributes.assert_called_once_with(
        created[0].name, {'acl_user_enable': True, 'acl_users': 'image-test'}
    )
    context.queue_profiles.delete_queue_profile.assert_called_once_with(
        queue_profile_id='canary-profile'
    )


def test_canary_probe_script_runs_and_deletes_its_file(tmp_path, monkeypatch, capsys):
    import os
    import time

    monkeypatch.setattr(os.path, 'ismount', lambda path: path == str(tmp_path))
    monkeypatch.setattr(time, 'sleep', lambda _: None)
    script = canary_script(
        'validate-image-test', '/tmp/out', [('data', str(tmp_path))], 'known-output'
    )
    program = script.split(" - <<'PY'\n", 1)[1].rsplit('PY\n', 1)[0]
    with pytest.raises(SystemExit) as result:
        exec(compile(program, '<canary>', 'exec'), {})
    assert result.value.code == 0
    evidence = json.loads(capsys.readouterr().out)
    assert evidence['token'] == 'known-output'
    assert evidence['checks'][0]['ok'] is True
    assert list(tmp_path.iterdir()) == []


def _probe(tmp_path, monkeypatch, capsys, mounts, home):
    import os
    import pwd
    import time

    monkeypatch.setattr(os.path, 'ismount', lambda path: path in [m for _, m in mounts])
    monkeypatch.setattr(time, 'sleep', lambda _: None)
    monkeypatch.setattr(pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_dir=home))
    script = canary_script('validate-image-test', '/tmp/out', mounts, 'known-output')
    program = script.split(" - <<'PY'\n", 1)[1].rsplit('PY\n', 1)[0]
    with pytest.raises(SystemExit) as result:
        exec(compile(program, '<canary>', 'exec'), {})
    checks = {c['name']: c for c in json.loads(capsys.readouterr().out)['checks']}
    return result.value.code, checks


def test_canary_probes_as_a_user_home_for_writes_and_reads_admin_filesystems(
    tmp_path, monkeypatch, capsys
):
    """
    the live failure: the validation user wrote at the top of apps, which only admins may
    write (Errno 13). A user writes in its home; an admin-owned filesystem is read
    """
    apps, data = tmp_path / 'apps', tmp_path / 'data'
    home = data / 'home' / 'image-test'
    home.mkdir(parents=True)
    apps.mkdir()
    (apps / 'tool').write_text('x')
    apps.chmod(0o555)
    data.chmod(0o555)
    try:
        code, checks = _probe(
            tmp_path,
            monkeypatch,
            capsys,
            [('apps', str(apps)), ('data', str(data))],
            str(home),
        )
    finally:
        apps.chmod(0o755)
        data.chmod(0o755)
    assert code == 0, checks
    assert 'read-only to users' in checks['filesystem:apps']['detail']
    assert 'write, fsync, read and delete' in checks['filesystem:data']['detail']
    assert list(home.iterdir()) == []


def test_canary_fails_when_the_users_home_is_not_writable(
    tmp_path, monkeypatch, capsys
):
    data = tmp_path / 'data'
    home = data / 'home' / 'image-test'
    home.mkdir(parents=True)
    home.chmod(0o555)
    try:
        code, checks = _probe(
            tmp_path, monkeypatch, capsys, [('data', str(data))], str(home)
        )
    finally:
        home.chmod(0o755)
    assert code == 1
    assert checks['filesystem:data']['ok'] is False


def _clock(monkeypatch):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'sleep', sleep)


def test_cleanup_waits_for_an_exiting_job_and_a_busy_queue(monkeypatch):
    """
    the live failure: qdel refused the exiting job (state E) and qmgr reported the queue
    busy; both clear within seconds, so cleanup waits instead of failing a validated row
    """
    _clock(monkeypatch)
    context = Mock()
    context.scheduler.is_job_active.side_effect = [True] * 5 + [False]
    busy = exceptions.soca_exception(module.errorcodes.SCHEDULER_QUEUE_BUSY, 'busy')
    context.queue_profiles.delete_queue_profile.side_effect = [busy, busy, None]
    errors = ComputeImageCanary(context)._release(
        '11', SimpleNamespace(queue_profile_id='q')
    )
    assert errors == []
    context.scheduler.delete_job.assert_not_called()
    assert context.queue_profiles.delete_queue_profile.call_count == 3


def test_cleanup_deletes_a_stuck_job_and_reports_what_stayed(monkeypatch):
    _clock(monkeypatch)
    context = Mock()
    context.scheduler.is_job_active.return_value = True
    context.scheduler.delete_job.side_effect = exceptions.soca_exception(
        module.errorcodes.SCHEDULER_ERROR, 'Request invalid for state of job'
    )
    busy = exceptions.soca_exception(module.errorcodes.SCHEDULER_QUEUE_BUSY, 'busy')
    context.queue_profiles.delete_queue_profile.side_effect = busy
    errors = ComputeImageCanary(context)._release(
        '12', SimpleNamespace(queue_profile_id='q')
    )
    context.scheduler.delete_job.assert_called_once_with('12')
    assert any('did not leave PBS' in e for e in errors)
    assert any('busy' in e for e in errors)
    # bounded: the busy retries stop at the cleanup deadline
    assert module.time.monotonic() <= module.CLEANUP_SECONDS + 5
