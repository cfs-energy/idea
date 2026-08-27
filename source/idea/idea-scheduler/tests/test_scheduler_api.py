"""
Test Cases for SchedulerAPI

Covers the job_owner / context user check in Scheduler.SubmitJob. job_owner is
deserialized from the request, so it is never the same str object as the username
read from the token - the comparison must be by value.
"""

from ideadatamodel import constants, errorcodes, exceptions
from ideadatamodel.scheduler import GetJobResult, SocaJob, SubmitJobRequest
from ideasdk.api import ApiInvocationContext
from ideasdk.utils import Utils, GroupNameHelper
from ideascheduler.app.api import scheduler_api as scheduler_api_module
from ideascheduler.app.api.scheduler_api import (
    SchedulerAPI,
    sanitize_job_name_prefix,
    open_new_job_script,
)

from typing import Dict
import json
import os
import pytest


CONTEXT_USER = 'testuser'


def build_invocation_context(
    context, payload: Dict, namespace: str = 'Scheduler.SubmitJob'
) -> ApiInvocationContext:
    return ApiInvocationContext(
        context=context,
        request={
            'header': {
                'namespace': namespace,
                'request_id': Utils.uuid(),
            },
            'payload': payload,
        },
        invocation_source=constants.API_INVOCATION_SOURCE_HTTP,
        group_name_helper=GroupNameHelper(context=context),
        logger=context.logger(),
    )


@pytest.fixture()
def scheduler_api(context, monkeypatch):
    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: CONTEXT_USER, raising=False
    )
    return SchedulerAPI(context=context)


def test_submit_job_accepts_deserialized_job_owner(scheduler_api, context):
    """
    a job_owner equal to the context user must pass, even though deserialization
    produces a distinct str object
    """
    job_owner = json.loads(json.dumps({'job_owner': CONTEXT_USER}))['job_owner']
    assert job_owner == CONTEXT_USER
    assert job_owner is not CONTEXT_USER

    api_context = build_invocation_context(context, {'job_owner': job_owner})

    # the owner check must pass; validation then stops on the missing job_script
    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler_api.submit_job(api_context)
    assert 'job_script is required' in exc_info.value.message


def test_submit_job_rejects_mismatched_job_owner(scheduler_api, context):
    """
    a job_owner belonging to another user must be rejected
    """
    api_context = build_invocation_context(context, {'job_owner': 'someone-else'})

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler_api.submit_job(api_context)
    assert 'Mismatched user information' in exc_info.value.message


def test_submit_job_defaults_job_owner_to_context_user(scheduler_api, context):
    """
    an omitted job_owner defaults to the context user and passes the owner check
    """
    api_context = build_invocation_context(context, {})

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler_api.submit_job(api_context)
    assert 'job_script is required' in exc_info.value.message


def test_submit_job_requires_context_user(context, monkeypatch):
    """
    an unauthenticated invocation is rejected before the owner check
    """
    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: None, raising=False
    )
    api = SchedulerAPI(context=context)
    api_context = build_invocation_context(context, {'job_owner': CONTEXT_USER})

    with pytest.raises(exceptions.SocaException) as exc_info:
        api.submit_job(api_context)
    assert 'Empty context user' in exc_info.value.message


# --- job name -> filename sanitization: '#PBS -N' is untrusted and root writes the file ---


@pytest.mark.parametrize(
    'job_name,expected',
    [
        ('/etc/profile.d/pwn', 'pwn'),  # leading slash resets os.path.join
        ('../../../etc/x', 'x'),  # path traversal
        ('a/b', 'b'),  # embedded separator
        ('..', ''),  # would become '..'
        ('.', ''),  # would become '.'
        ('my job#1', 'my_job_1'),  # charset scrub
        (None, ''),
        ('', ''),
    ],
)
def test_sanitize_job_name_prefix(job_name, expected):
    """
    the sanitized prefix never contains a path separator or '..' and never escapes
    the intended directory when joined into a filename.
    """
    result = sanitize_job_name_prefix(job_name)
    assert result == expected
    assert '/' not in result
    assert os.sep not in result
    assert result not in ('.', '..')


def test_sanitize_job_name_prefix_caps_length():
    result = sanitize_job_name_prefix('a' * 500)
    assert len(result) == 64


def test_open_new_job_script_rejects_existing_file(tmp_path):
    """O_EXCL: a pre-planted file at the path is not overwritten."""
    target = tmp_path / 'planted.sh'
    target.write_text('attacker owned')
    with pytest.raises(FileExistsError):
        open_new_job_script(str(target))
    # the pre-existing content is untouched
    assert target.read_text() == 'attacker owned'


def test_open_new_job_script_rejects_symlink(tmp_path):
    """O_NOFOLLOW / O_EXCL: a pre-planted symlink cannot redirect the write."""
    secret = tmp_path / 'outside.txt'
    secret.write_text('original')
    link = tmp_path / 'link.sh'
    os.symlink(str(secret), str(link))
    with pytest.raises(OSError):
        open_new_job_script(str(link))
    # the symlink target was not written through
    assert secret.read_text() == 'original'


class _FakeShellResult:
    def __init__(self, returncode=0, stdout='123.pbs', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeShell:
    def __init__(self, returncode=0, stderr=''):
        self.commands = []
        self.returncode = returncode
        self.stderr = stderr

    def invoke(self, command, **_):
        self.commands.append(command)
        return _FakeShellResult(returncode=self.returncode, stderr=self.stderr)


def _prepare_submit(context, monkeypatch, returncode=0, stderr=''):
    """wire up a SchedulerAPI whose _submit_job can run without root or a real shell."""
    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: CONTEXT_USER, raising=False
    )
    monkeypatch.setattr(scheduler_api_module.shutil, 'chown', lambda *a, **k: None)
    monkeypatch.setattr(scheduler_api_module, 'chown_fd', lambda *a, **k: None)
    monkeypatch.setattr(
        GroupNameHelper, 'get_user_group', lambda *a, **k: 'grp', raising=False
    )
    context.shell = _FakeShell(returncode=returncode, stderr=stderr)
    # the mock data mount dir is stable across tests; start from an empty jobs dir
    import shutil as _shutil

    jobs_dir = _jobs_dir(context)
    if os.path.isdir(jobs_dir):
        _shutil.rmtree(jobs_dir)
    api = SchedulerAPI(context=context)
    return api


def _jobs_dir(context):
    data_dir = context.config().get_string(
        'shared-storage.data.mount_dir', required=True
    )
    return os.path.join(data_dir, 'home', CONTEXT_USER, 'jobs')


def _record_script_writes(monkeypatch) -> list:
    """
    the script is removed once the submission finishes, so record where each write went
    (the bare filename and the stat of the directory it was opened relative to).
    """
    written = []
    original = scheduler_api_module.open_new_job_script

    def recording_open(path, dir_fd=None):
        written.append((path, os.fstat(dir_fd) if dir_fd is not None else None))
        return original(path, dir_fd=dir_fd)

    monkeypatch.setattr(scheduler_api_module, 'open_new_job_script', recording_open)
    return written


def _same_directory(dir_stat, directory: str) -> bool:
    target = os.stat(directory)
    return dir_stat is not None and (dir_stat.st_dev, dir_stat.st_ino) == (
        target.st_dev,
        target.st_ino,
    )


@pytest.mark.parametrize(
    'malicious_name', ['/etc/profile.d/pwn', '../../../etc/x', 'a/b']
)
def test_submit_job_writes_only_inside_jobs_dir(context, monkeypatch, malicious_name):
    """
    a malicious '#PBS -N' name must produce a job script INSIDE the user's jobs dir
    with a sanitized basename - never at the attacker-chosen path.
    """
    api = _prepare_submit(context, monkeypatch)
    jobs_dir = _jobs_dir(context)
    written = _record_script_writes(monkeypatch)

    script = f'#!/bin/bash\n#PBS -N {malicious_name}\necho hi\n'
    request = SubmitJobRequest(
        job_owner=CONTEXT_USER,
        job_script=Utils.base64_encode(script),
        job_script_interpreter='bash',
    )

    result = api._submit_job(request=request, job_owner=CONTEXT_USER, dry_run=None)
    assert Utils.get_as_bool(result.accepted, False) is True

    assert len(written) == 1
    name, dir_stat = written[0]
    # basename is sanitized: no separator, opened relative to the jobs dir itself
    assert '/' not in name
    assert '..' not in name
    assert _same_directory(dir_stat, jobs_dir)
    # nothing root-created is left behind once the submission finished
    assert os.listdir(jobs_dir) == []


def test_submit_job_does_not_escape_to_external_dir(context, monkeypatch, tmp_path):
    """
    '#PBS -N' points at a writable directory outside the jobs dir. the write stays inside
    the jobs dir and the external dir stays empty.
    """
    api = _prepare_submit(context, monkeypatch)
    jobs_dir = _jobs_dir(context)
    written = _record_script_writes(monkeypatch)

    external = tmp_path / 'external'
    external.mkdir()
    script = f'#!/bin/bash\n#PBS -N {external}/pwned\necho hi\n'
    request = SubmitJobRequest(
        job_owner=CONTEXT_USER,
        job_script=Utils.base64_encode(script),
        job_script_interpreter='bash',
    )

    api._submit_job(request=request, job_owner=CONTEXT_USER, dry_run=None)

    assert os.listdir(str(external)) == []  # nothing escaped
    assert len(written) == 1
    name, dir_stat = written[0]
    assert name.startswith('pwned_')
    assert _same_directory(dir_stat, jobs_dir)


class _FakeTracker:
    def __init__(self, result):
        self._result = result

    def get(self, job_uid):
        return self._result


def test_submit_job_surfaces_hook_reason_not_shell_repr(context, monkeypatch):
    """
    a pbs submission that qsub rejects (rc != 0) never ran. it must fail the call, and the
    message must be the submit hook's own text rather than the su/qsub shell repr.
    """
    from ideadatamodel.scheduler import SubmitJobResult

    hook_reason = 'project PROJECT_DOES_NOT_EXIST is invalid'
    api = _prepare_submit(context, monkeypatch, returncode=1, stderr=hook_reason)
    context.job_submission_tracker = _FakeTracker(
        SubmitJobResult(accepted=False, validations=None)
    )

    request = SubmitJobRequest(
        job_owner=CONTEXT_USER,
        job_script=Utils.base64_encode('#PBS -N myjob\necho hi\n'),
        job_script_interpreter='pbs',
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        api._submit_job(request=request, job_owner=CONTEXT_USER, dry_run=None)

    assert exc_info.value.error_code == errorcodes.JOB_SUBMISSION_FAILED
    assert hook_reason in exc_info.value.message
    # the command line and the user's home path stay out of the client-facing message
    assert 'shell>' not in exc_info.value.message
    assert CONTEXT_USER not in exc_info.value.message


def test_submit_job_rejected_leaves_no_script_behind(context, monkeypatch):
    """a refused submission queued nothing, so nothing of it stays in the jobs dir"""
    from ideadatamodel.scheduler import SubmitJobResult

    api = _prepare_submit(context, monkeypatch, returncode=1, stderr='refused')
    context.job_submission_tracker = _FakeTracker(
        SubmitJobResult(accepted=False, validations=None)
    )

    request = SubmitJobRequest(
        job_owner=CONTEXT_USER,
        job_script=Utils.base64_encode('#PBS -N myjob\necho hi\n'),
        job_script_interpreter='pbs',
    )

    with pytest.raises(exceptions.SocaException):
        api._submit_job(request=request, job_owner=CONTEXT_USER, dry_run=None)

    assert os.listdir(_jobs_dir(context)) == []


def test_submit_job_creates_the_script_relative_to_the_jobs_dir(context, monkeypatch):
    """the script is created through a directory fd, not by an absolute path open"""
    opened = []
    real_open = scheduler_api_module.open_new_job_script

    def record(path, dir_fd=None):
        opened.append((path, dir_fd))
        return real_open(path, dir_fd=dir_fd)

    monkeypatch.setattr(scheduler_api_module, 'open_new_job_script', record)
    api = _prepare_submit(context, monkeypatch)
    request = SubmitJobRequest(
        job_owner=CONTEXT_USER,
        job_script=Utils.base64_encode('echo hi\n'),
        job_script_interpreter='bash',
    )

    api._submit_job(request=request, job_owner=CONTEXT_USER, dry_run=None)

    assert len(opened) == 1
    path, dir_fd = opened[0]
    assert '/' not in path
    assert isinstance(dir_fd, int)


def test_submit_job_dry_run_returns_the_refusal_instead_of_raising(
    context, monkeypatch
):
    """
    a dry run is refused by design - the recorded report is its answer, not an error.
    """
    from ideadatamodel.scheduler import DryRunOption, SubmitJobResult

    api = _prepare_submit(context, monkeypatch, returncode=1, stderr='would not run')
    tracked = SubmitJobResult(accepted=False, validations=None)
    context.job_submission_tracker = _FakeTracker(tracked)

    request = SubmitJobRequest(
        job_owner=CONTEXT_USER,
        job_script=Utils.base64_encode('#PBS -N myjob\necho hi\n'),
        job_script_interpreter='pbs',
        dry_run=True,
    )

    result = api._submit_job(
        request=request, job_owner=CONTEXT_USER, dry_run=DryRunOption.DEFAULT
    )
    assert result is tracked
    assert Utils.get_as_bool(result.accepted, False) is False


def test_submit_job_raises_only_when_tracker_empty(context, monkeypatch):
    """
    when qsub fails and the tracker has nothing, the raw failure is surfaced as a
    last resort (no structured result exists to return).
    """
    api = _prepare_submit(context, monkeypatch, returncode=1)
    context.job_submission_tracker = _FakeTracker(None)

    request = SubmitJobRequest(
        job_owner=CONTEXT_USER,
        job_script=Utils.base64_encode('#PBS -N myjob\necho hi\n'),
        job_script_interpreter='pbs',
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        api._submit_job(request=request, job_owner=CONTEXT_USER, dry_run=None)
    assert 'Failed to submit job' in exc_info.value.message


# --- job read scoping: Scheduler.GetActiveJob / Scheduler.GetCompletedJob ---

OTHER_USER = 'someone-else'


class _FakeJobCache:
    """job ids are sequential, so any id returns the same job here."""

    def __init__(self, job: SocaJob):
        self._job = job

    def get_job(self, _job_id):
        return self._job

    def get_completed_job(self, _job_id):
        return self._job

    def get_completed_job_by_uid(self, _job_uid):
        return self._job


def _prepare_get_job(
    context, monkeypatch, owner: str, administrator: bool = False, manager: bool = False
):
    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: CONTEXT_USER, raising=False
    )
    # is_authorized itself is left real: stubbing it would hide which grant the api asks
    # for, and elevated_access=False resolves to any member of the module users group.
    monkeypatch.setattr(
        ApiInvocationContext,
        'is_administrator',
        lambda _self: administrator,
        raising=False,
    )
    monkeypatch.setattr(
        ApiInvocationContext, 'is_manager', lambda _self: manager, raising=False
    )
    monkeypatch.setattr(
        ApiInvocationContext, 'is_authorized_user', lambda _self: True, raising=False
    )
    context.job_cache = _FakeJobCache(SocaJob(job_id='1', name='j', owner=owner))
    # no document store: the completed job read falls back to the job cache
    context.document_store = _FakeDocumentStore(enabled=False)
    return SchedulerAPI(context=context)


@pytest.mark.parametrize(
    'method,namespace',
    [
        ('get_active_job', 'Scheduler.GetActiveJob'),
        ('get_completed_job', 'Scheduler.GetCompletedJob'),
    ],
)
def test_get_job_allows_owner(context, monkeypatch, method, namespace):
    """the requesting user reads their own job"""
    api = _prepare_get_job(context, monkeypatch, owner=CONTEXT_USER)
    api_context = build_invocation_context(context, {'job_id': '1'}, namespace)

    getattr(api, method)(api_context)

    assert api_context.is_success()


@pytest.mark.parametrize(
    'method,namespace',
    [
        ('get_active_job', 'Scheduler.GetActiveJob'),
        ('get_completed_job', 'Scheduler.GetCompletedJob'),
    ],
)
def test_get_job_denies_non_owner(context, monkeypatch, method, namespace):
    """
    another user's job is refused. without this the sequential job ids are enumerable
    by any authenticated cluster user.
    """
    api = _prepare_get_job(context, monkeypatch, owner=OTHER_USER)
    api_context = build_invocation_context(context, {'job_id': '1'}, namespace)

    with pytest.raises(exceptions.SocaException) as exc_info:
        getattr(api, method)(api_context)
    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


@pytest.mark.parametrize(
    'method,namespace',
    [
        ('get_active_job', 'Scheduler.GetActiveJob'),
        ('get_completed_job', 'Scheduler.GetCompletedJob'),
    ],
)
def test_get_job_allows_administrator(context, monkeypatch, method, namespace):
    """an administrator keeps cluster wide read access"""
    api = _prepare_get_job(context, monkeypatch, owner=OTHER_USER, administrator=True)
    api_context = build_invocation_context(context, {'job_id': '1'}, namespace)

    getattr(api, method)(api_context)

    assert api_context.is_success()


@pytest.mark.parametrize(
    'method,namespace',
    [
        ('get_active_job', 'Scheduler.GetActiveJob'),
        ('get_completed_job', 'Scheduler.GetCompletedJob'),
    ],
)
def test_get_job_allows_manager(context, monkeypatch, method, namespace):
    """a cluster manager or module administrator keeps cluster wide read access"""
    api = _prepare_get_job(context, monkeypatch, owner=OTHER_USER, manager=True)
    api_context = build_invocation_context(context, {'job_id': '1'}, namespace)

    getattr(api, method)(api_context)

    assert api_context.is_success()


@pytest.mark.parametrize(
    'method,namespace',
    [
        ('get_active_job', 'Scheduler.GetActiveJob'),
        ('get_completed_job', 'Scheduler.GetCompletedJob'),
    ],
)
def test_get_job_denies_job_without_owner(context, monkeypatch, method, namespace):
    """a job carrying no owner is not readable by a non elevated caller"""
    api = _prepare_get_job(context, monkeypatch, owner=None)
    api_context = build_invocation_context(context, {'job_id': '1'}, namespace)

    with pytest.raises(exceptions.SocaException) as exc_info:
        getattr(api, method)(api_context)
    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


# --- completed jobs owner filter ---


class _FakeDocumentStore:
    def __init__(self, enabled: bool = True, jobs=None):
        self.options = None
        self.get_job_args = None
        self._enabled = enabled
        # indexed jobs, by job_uid
        self._jobs = jobs or {}

    def is_enabled(self):
        return self._enabled

    def search_jobs(self, options):
        self.options = options
        from ideadatamodel import SocaPaginator
        from ideadatamodel.scheduler import ListJobsResult

        return ListJobsResult(
            listing=[],
            paginator=SocaPaginator(total=0, page_size=10, start=0),
        )

    def get_job(self, job_uid=None, job_id=None, owner=None):
        self.get_job_args = {'job_uid': job_uid, 'job_id': job_id, 'owner': owner}
        if Utils.is_not_empty(job_uid):
            return self._jobs.get(job_uid)
        # a job id can name more than one document: newest first, as the search does
        for job in reversed(list(self._jobs.values())):
            if job.job_id != job_id:
                continue
            if Utils.is_not_empty(owner) and job.owner != owner:
                continue
            return job
        return None


def _filter_keys(options):
    return [f.key for f in options.filters]


def test_list_completed_jobs_filters_on_owner_raw(context, monkeypatch):
    """
    the owner term filter must target owner.raw. against the analysed field a
    hyphenated username matches none of its own documents.
    """
    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: 'jane-doe', raising=False
    )
    document_store = _FakeDocumentStore()
    context.document_store = document_store
    api = SchedulerAPI(context=context)
    api_context = build_invocation_context(context, {}, 'Scheduler.ListCompletedJobs')

    api.list_completed_jobs(api_context)

    owner_filters = [f for f in document_store.options.filters if 'owner' in f.key]
    assert len(owner_filters) == 1
    assert owner_filters[0].key == 'owner.raw'
    assert owner_filters[0].value == 'jane-doe'


@pytest.mark.parametrize('supplied_key', ['owner', 'owner.raw'])
def test_list_completed_jobs_strips_client_owner_filter(
    context, monkeypatch, supplied_key
):
    """a client supplied owner term never survives; the server owns that dimension"""
    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: CONTEXT_USER, raising=False
    )
    document_store = _FakeDocumentStore()
    context.document_store = document_store
    api = SchedulerAPI(context=context)
    api_context = build_invocation_context(
        context,
        {'filters': [{'key': supplied_key, 'value': OTHER_USER}]},
        'Scheduler.ListCompletedJobs',
    )

    api.list_completed_jobs(api_context)

    owner_filters = [f for f in document_store.options.filters if 'owner' in f.key]
    assert _filter_keys(document_store.options) == ['owner.raw']
    assert owner_filters[0].value == CONTEXT_USER


# --- the owner term is never absent ---


@pytest.mark.parametrize(
    'method,namespace',
    [
        ('list_completed_jobs', 'Scheduler.ListCompletedJobs'),
        ('get_active_job', 'Scheduler.GetActiveJob'),
        ('get_completed_job', 'Scheduler.GetCompletedJob'),
    ],
)
def test_job_reads_refuse_an_empty_username(context, monkeypatch, method, namespace):
    """
    a filter whose value is empty is dropped before it reaches OpenSearch, leaving a
    match-all query. the read is refused rather than run unscoped.
    """
    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: '', raising=False
    )
    context.document_store = _FakeDocumentStore()
    context.job_cache = _FakeJobCache(SocaJob(job_id='1', name='j', owner=OTHER_USER))
    api = SchedulerAPI(context=context)
    api_context = build_invocation_context(context, {'job_id': '1'}, namespace)

    with pytest.raises(exceptions.SocaException) as exc_info:
        getattr(api, method)(api_context)
    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


# --- Scheduler.GetCompletedJob: a job id is reused when the host is replaced ---

# the job the completed jobs listing holds for job id 41, and the job the local job
# cache on the current scheduler host holds for the same id.
LISTED_JOB = SocaJob(job_id='41', job_uid='uid-listed', owner=CONTEXT_USER)
CACHED_JOB = SocaJob(job_id='41', job_uid='uid-cached', owner=CONTEXT_USER)


def _prepare_completed_job_read(context, monkeypatch, indexed_jobs, cached_job=None):
    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: CONTEXT_USER, raising=False
    )
    monkeypatch.setattr(
        ApiInvocationContext, 'is_administrator', lambda _self: False, raising=False
    )
    monkeypatch.setattr(
        ApiInvocationContext, 'is_manager', lambda _self: False, raising=False
    )
    monkeypatch.setattr(
        ApiInvocationContext, 'is_authorized_user', lambda _self: True, raising=False
    )
    document_store = _FakeDocumentStore(jobs=indexed_jobs)
    context.document_store = document_store
    context.job_cache = _FakeJobCache(cached_job)
    return SchedulerAPI(context=context), document_store


def _read_completed_job(api, context, payload) -> SocaJob:
    api_context = build_invocation_context(
        context, payload, 'Scheduler.GetCompletedJob'
    )
    api.get_completed_job(api_context)
    assert api_context.is_success()
    return api_context.get_response_payload_as(GetJobResult).job


def test_get_completed_job_reads_the_store_the_listing_reads(context, monkeypatch):
    """
    the local job cache is keyed on a job id the scheduler reuses, so it can hold a
    different job under the id the listing shows - with that job's costs.
    """
    api, _ = _prepare_completed_job_read(
        context, monkeypatch, {'uid-listed': LISTED_JOB}, cached_job=CACHED_JOB
    )

    job = _read_completed_job(api, context, {'job_id': '41'})

    assert job.job_uid == 'uid-listed'


def test_get_completed_job_refuses_a_job_id_the_durable_store_does_not_hold(
    context, monkeypatch
):
    """
    with nothing indexed for the id, the answer is not found. answering from the local
    job cache would return whichever job most recently held the number.
    """
    api, _ = _prepare_completed_job_read(
        context, monkeypatch, {}, cached_job=CACHED_JOB
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        _read_completed_job(api, context, {'job_id': '41'})
    assert exc_info.value.error_code == errorcodes.JOB_NOT_FOUND


def test_get_completed_job_resolves_a_job_uid_exactly(context, monkeypatch):
    """job_uid names one job, whichever job id it was given"""
    api, document_store = _prepare_completed_job_read(
        context,
        monkeypatch,
        {'uid-listed': LISTED_JOB, 'uid-cached': CACHED_JOB},
        cached_job=CACHED_JOB,
    )

    job = _read_completed_job(api, context, {'job_uid': 'uid-listed'})

    assert job.job_uid == 'uid-listed'
    assert document_store.get_job_args['job_uid'] == 'uid-listed'


def test_get_completed_job_reads_a_job_uid_the_index_does_not_have_yet(
    context, monkeypatch
):
    """
    a finished job reaches the job cache before it is indexed. a job_uid lookup there is
    exact, so the gap is closed without risking another job's record.
    """
    api, _ = _prepare_completed_job_read(
        context, monkeypatch, {}, cached_job=CACHED_JOB
    )

    job = _read_completed_job(api, context, {'job_uid': 'uid-cached'})

    assert job.job_uid == 'uid-cached'


def test_get_completed_job_scopes_a_job_id_lookup_to_the_caller(context, monkeypatch):
    """
    the caller's own job is the job they meant. without the owner term the newest holder
    of the id wins, and for someone else's job that is a refusal rather than an answer.
    """
    api, document_store = _prepare_completed_job_read(
        context, monkeypatch, {'uid-listed': LISTED_JOB}, cached_job=CACHED_JOB
    )

    _read_completed_job(api, context, {'job_id': '41'})

    assert document_store.get_job_args['owner'] == CONTEXT_USER


def test_get_completed_job_does_not_scope_an_elevated_lookup(context, monkeypatch):
    """an administrator reads cluster wide, as they do in the listing"""
    api, document_store = _prepare_completed_job_read(
        context, monkeypatch, {'uid-listed': LISTED_JOB}, cached_job=CACHED_JOB
    )
    monkeypatch.setattr(
        ApiInvocationContext, 'is_administrator', lambda _self: True, raising=False
    )

    _read_completed_job(api, context, {'job_id': '41'})

    assert document_store.get_job_args['owner'] is None


def test_get_completed_job_requires_an_identifier(context, monkeypatch):
    api, _ = _prepare_completed_job_read(
        context, monkeypatch, {'uid-listed': LISTED_JOB}, cached_job=CACHED_JOB
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        _read_completed_job(api, context, {})
    assert 'job_uid' in exc_info.value.message


def test_list_active_jobs_always_filters_on_the_token_username(context, monkeypatch):
    """
    the active jobs read goes to the job cache, which filters on the literal value, so
    an empty username matches nothing rather than everything. what it must never do is
    omit the owner.
    """

    class _RecordingJobCache:
        def __init__(self):
            self.list_kwargs = None
            self.count_kwargs = None

        def list_jobs(self, **kwargs):
            self.list_kwargs = kwargs
            return []

        def get_count(self, **kwargs):
            self.count_kwargs = kwargs
            return 0

    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: 'jane-doe', raising=False
    )
    job_cache = _RecordingJobCache()
    context.job_cache = job_cache
    api = SchedulerAPI(context=context)

    api.list_active_jobs(
        build_invocation_context(context, {}, 'Scheduler.ListActiveJobs')
    )

    assert job_cache.list_kwargs['owner'] == 'jane-doe'
    assert job_cache.count_kwargs['owner'] == 'jane-doe'
