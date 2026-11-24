"""
Test Cases for how Scheduler.SubmitJob runs a job script as its owner

qsub and a bash script run as the job owner, without a shell, with the batch client's
settings and none of the scheduler's own environment, and only with a project the owner
belongs to. these walk the real submission path and the real command runner; qsub is a
stand-in on PATH that records what it was given.
"""

import getpass
import os
import pwd
import stat

import pytest

from ideadatamodel import Project, SocaJob, exceptions
from ideadatamodel.scheduler import DryRunOption, SubmitJobRequest, SubmitJobResult
from ideascheduler.app.api import scheduler_api as scheduler_api_module
from ideascheduler.app.api.scheduler_api import SchedulerAPI
from ideasdk.shell import ShellInvoker
from ideasdk.utils import Utils

OWNER = getpass.getuser()
SERVICE_ONLY = (
    'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
    'AWS_DEFAULT_REGION',
    'ECS_CONTAINER_METADATA_URI_V4',
    'IDEA_CLUSTER_NAME',
)
PROJECT = 'my project; $HOME `x`'


class RecordedResult:
    def get(self, _job_uid):
        return SubmitJobResult(accepted=True, job=SocaJob(job_id=None))


@pytest.fixture()
def api(context, monkeypatch, tmp_path):
    """the submission path with the real command runner and a recording qsub on PATH"""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    record = tmp_path / 'record'
    record.mkdir()
    qsub = bin_dir / 'qsub'
    qsub.write_text(
        '#!/bin/sh\n'
        f'for arg in "$@"; do printf "%s\\n" "$arg"; done > {record}/argv\n'
        f'env > {record}/env\n'
        f'pwd > {record}/cwd\n'
        'echo 1234.mockcluster\n'
    )
    qsub.chmod(qsub.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    for name in SERVICE_ONLY:
        monkeypatch.setenv(name, 'service-value')
    monkeypatch.setenv('PBS_CONF_FILE', '/etc/pbs.conf')
    monkeypatch.setenv('PBS_EXEC', '/opt/pbs')

    data_dir = tmp_path / 'data'

    class ConfigView:
        @staticmethod
        def get_string(key, required=False, default=None):
            assert key == 'shared-storage.data.mount_dir'
            return str(data_dir)

    api = SchedulerAPI(context)
    monkeypatch.setattr(api.context, 'config', lambda: ConfigView())
    monkeypatch.setattr(api.context, 'shell', ShellInvoker())
    monkeypatch.setattr(api.context, 'job_submission_tracker', RecordedResult())
    monkeypatch.setattr(api.group_name_helper, 'get_user_group', lambda _u: 'grp')
    # the owner is the account running the tests, which owns what it creates already
    (data_dir / 'home' / OWNER).mkdir(parents=True)
    monkeypatch.setattr(scheduler_api_module, 'chown_fd', lambda *_a, **_k: None)
    api.record = record
    api.jobs_dir = data_dir / 'home' / OWNER / 'jobs'
    return api


def environment_of(path) -> dict:
    return dict(
        line.split('=', 1) for line in path.read_text().splitlines() if '=' in line
    )


def test_qsub_runs_as_the_owner_with_the_batch_settings_and_the_project_as_given(api):
    request = SubmitJobRequest(
        job_script_interpreter='pbs',
        job_script=Utils.base64_encode('#PBS -N named\necho hi\n'),
        project=PROJECT,
    )

    result = api._submit_job(
        request=request, job_owner=OWNER, dry_run=DryRunOption.JSON_BOM
    )

    assert result.job.job_id == '1234'
    argv = (api.record / 'argv').read_text().splitlines()
    # the project and the dry run mode arrive as single arguments, exactly as requested
    assert argv[argv.index('-P') + 1] == PROJECT
    assert 'dry_run=json:bom' in argv
    assert argv[-1] == result.job_script_path
    env = environment_of(api.record / 'env')
    assert env['PBS_CONF_FILE'] == '/etc/pbs.conf'
    assert env['PBS_EXEC'] == '/opt/pbs'
    assert env['HOME'] == pwd.getpwnam(OWNER).pw_dir
    for name in SERVICE_ONLY:
        assert name not in env
    assert os.path.realpath((api.record / 'cwd').read_text().strip()) == (
        os.path.realpath(api.jobs_dir)
    )


def test_a_bash_script_runs_as_the_owner_without_the_scheduler_environment(api):
    script = f'env > {api.record}/env\nqsub -N from-script\n'
    request = SubmitJobRequest(
        job_script_interpreter='bash', job_script=Utils.base64_encode(script)
    )

    result = api._submit_job(request=request, job_owner=OWNER, dry_run=None)

    assert result.accepted is True
    env = environment_of(api.record / 'env')
    assert env['PBS_EXEC'] == '/opt/pbs'
    for name in SERVICE_ONLY:
        assert name not in env
    # qsub is reachable from the script, as the Script Workbench needs
    assert (api.record / 'argv').read_text().splitlines() == ['-N', 'from-script']


class Invocation:
    def __init__(self, request):
        self.request = request
        self.result = None

    def get_request_payload_as(self, _payload_type):
        return self.request

    def get_username(self):
        return 'mockuser'

    def success(self, payload):
        self.result = payload


def test_submit_job_accepts_only_a_project_the_owner_belongs_to(context, monkeypatch):
    api = SchedulerAPI(context)
    monkeypatch.setattr(
        context.projects_client,
        'get_user_projects',
        lambda username: [Project(name='mine')] if username == 'mockuser' else [],
    )
    submitted = []
    monkeypatch.setattr(
        api,
        '_submit_job',
        lambda request, job_owner, dry_run: (
            submitted.append(request.project) or SubmitJobResult(accepted=True)
        ),
    )

    def submit(project):
        api.submit_job(
            Invocation(
                SubmitJobRequest(
                    job_script_interpreter='pbs',
                    job_script=Utils.base64_encode('echo hi\n'),
                    project=project,
                )
            )
        )

    with pytest.raises(exceptions.SocaException) as exc_info:
        submit('theirs')
    assert exc_info.value.error_code == 'INVALID_PARAMS'

    submit('mine')
    submit(None)
    assert submitted == ['mine', None]


def test_a_bash_dry_run_is_refused_and_the_script_is_not_run(api):
    script = f'touch {api.record}/ran\n'
    request = SubmitJobRequest(
        job_script_interpreter='bash', job_script=Utils.base64_encode(script)
    )
    with pytest.raises(exceptions.SocaException) as error:
        api._submit_job(request=request, job_owner=OWNER, dry_run=DryRunOption.JSON_BOM)
    assert 'dry run is not available for bash' in error.value.message
    assert not (api.record / 'ran').exists()


def test_a_bash_script_that_runs_too_long_is_stopped(api, monkeypatch):
    monkeypatch.setattr(scheduler_api_module, 'BASH_SUBMISSION_TIMEOUT_SECONDS', 1)
    request = SubmitJobRequest(
        job_script_interpreter='bash', job_script=Utils.base64_encode('sleep 30\n')
    )
    with pytest.raises(exceptions.SocaException) as error:
        api._submit_job(request=request, job_owner=OWNER, dry_run=None)
    assert 'did not finish within 1 seconds' in error.value.message


def test_bash_output_goes_to_a_log_in_the_jobs_folder_not_the_service(api, caplog):
    # SCH-11: the output was held in the service and a failure wrote it to the service log
    script = 'echo to-stdout-marker\necho to-stderr-marker >&2\nexit 3\n'
    request = SubmitJobRequest(
        job_script_interpreter='bash', job_script=Utils.base64_encode(script)
    )
    with pytest.raises(exceptions.SocaException) as error:
        api._submit_job(request=request, job_owner=OWNER, dry_run=None)
    assert 'exited with status 3' in error.value.message
    [log] = list(api.jobs_dir.glob('*.sh.log'))
    assert log.read_text().splitlines() == ['to-stdout-marker', 'to-stderr-marker']
    assert 'marker' not in caplog.text


def test_a_submission_past_the_bound_is_refused_as_busy(api):
    # SCHED-14: submissions held every worker the PBS hook's answer needed
    from threading import BoundedSemaphore

    api.submission_slots = BoundedSemaphore(1)
    assert api.submission_slots.acquire(blocking=False)
    request = SubmitJobRequest(
        job_script_interpreter='bash', job_script=Utils.base64_encode('true\n')
    )
    with pytest.raises(exceptions.SocaException) as busy:
        api._submit_job(request=request, job_owner=OWNER, dry_run=None)
    assert 'busy with other job submissions' in busy.value.message
    api.submission_slots.release()
    assert api._submit_job(request=request, job_owner=OWNER, dry_run=None).accepted


def test_the_bound_is_half_the_request_workers(context):
    assert SchedulerAPI(context).submission_slots._value == 8
