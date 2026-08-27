"""
Test Cases for Scheduler.SubmitJob idempotency

one form submission must produce one job. these cover the deduplicator itself and the
submit_job policy around it: which submissions claim a key, which are replayed and which are
released so they can be submitted again.
"""

from typing import Any, List, Optional

import pytest
import pathlib
import re
from ideascheduler.app.api import scheduler_api

from ideadatamodel import errorcodes, exceptions, SocaJob
from ideadatamodel.scheduler import DryRunOption, SubmitJobRequest, SubmitJobResult
from ideascheduler.app.api.scheduler_api import (
    JobSubmissionDeduplicator,
    SchedulerAPI,
)
from ideasdk.utils import Utils

JOB_SCRIPT = Utils.base64_encode('#!/bin/bash\n#PBS -l select=1:ncpus=1\necho hello\n')
OTHER_JOB_SCRIPT = Utils.base64_encode(
    '#!/bin/bash\n#PBS -l select=64:ncpus=96\necho hello\n'
)


def build_request(
    job_script: str = JOB_SCRIPT,
    client_submission_id: Optional[str] = 'submission-1',
    dry_run: Optional[bool] = None,
) -> SubmitJobRequest:
    return SubmitJobRequest(
        project='mockproject',
        dry_run=dry_run,
        job_script_interpreter='pbs',
        job_script=job_script,
        client_submission_id=client_submission_id,
    )


def build_result(accepted: bool, job_id: str = '1001') -> SubmitJobResult:
    return SubmitJobResult(accepted=accepted, job=SocaJob(job_id=job_id))


class MockApiInvocationContext:
    def __init__(self, request: SubmitJobRequest, username: str = 'mockuser'):
        self.request = request
        self.username = username
        self.result: Optional[Any] = None

    def get_request_payload_as(self, _payload_type):
        return self.request

    def get_username(self) -> str:
        return self.username

    def success(self, payload):
        self.result = payload


class SubmitJobRecorder:
    """
    replaces SchedulerAPI._submit_job. everything below submit_job writes a file and shells out
    to qsub, so the dedupe policy is exercised against a recorder instead.
    """

    def __init__(self, results: List[SubmitJobResult]):
        self.results = list(results)
        self.calls: List[Optional[DryRunOption]] = []

    def __call__(self, request: SubmitJobRequest, job_owner: str, dry_run):
        self.calls.append(dry_run)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def submit(api: SchedulerAPI, request: SubmitJobRequest, username: str = 'mockuser'):
    context = MockApiInvocationContext(request=request, username=username)
    api.submit_job(context)
    return context.result


def test_deduplicator_replays_a_completed_submission():
    deduplicator = JobSubmissionDeduplicator()
    key = JobSubmissionDeduplicator.build_key('mockuser', 'submission-1')
    fingerprint = JobSubmissionDeduplicator.build_fingerprint(build_request())
    result = build_result(accepted=True)

    assert deduplicator.begin(key=key, fingerprint=fingerprint) is None
    deduplicator.commit(key=key, fingerprint=fingerprint, result=result)

    assert deduplicator.begin(key=key, fingerprint=fingerprint) is result


def test_deduplicator_rejects_a_submission_still_in_flight():
    deduplicator = JobSubmissionDeduplicator()
    key = JobSubmissionDeduplicator.build_key('mockuser', 'submission-1')
    fingerprint = JobSubmissionDeduplicator.build_fingerprint(build_request())

    assert deduplicator.begin(key=key, fingerprint=fingerprint) is None

    # the second half of a double click arrives before the first qsub returns
    with pytest.raises(exceptions.SocaException) as exc_info:
        deduplicator.begin(key=key, fingerprint=fingerprint)
    assert exc_info.value.error_code == errorcodes.JOB_SUBMISSION_IN_PROGRESS


def test_deduplicator_rollback_frees_the_key():
    deduplicator = JobSubmissionDeduplicator()
    key = JobSubmissionDeduplicator.build_key('mockuser', 'submission-1')
    fingerprint = JobSubmissionDeduplicator.build_fingerprint(build_request())

    assert deduplicator.begin(key=key, fingerprint=fingerprint) is None
    deduplicator.rollback(key=key)
    assert deduplicator.begin(key=key, fingerprint=fingerprint) is None


def test_deduplicator_does_not_replay_a_different_request():
    deduplicator = JobSubmissionDeduplicator()
    key = JobSubmissionDeduplicator.build_key('mockuser', 'submission-1')
    fingerprint = JobSubmissionDeduplicator.build_fingerprint(build_request())
    deduplicator.commit(
        key=key, fingerprint=fingerprint, result=build_result(accepted=True)
    )

    # a client that re-uses a submission id for a different job script must get a fresh
    # submission, never the previous job's result.
    other_fingerprint = JobSubmissionDeduplicator.build_fingerprint(
        build_request(job_script=OTHER_JOB_SCRIPT)
    )
    assert other_fingerprint != fingerprint
    assert deduplicator.begin(key=key, fingerprint=other_fingerprint) is None


def test_deduplicator_keys_are_scoped_to_the_user():
    assert JobSubmissionDeduplicator.build_key(
        'alice', 'submission-1'
    ) != JobSubmissionDeduplicator.build_key('bob', 'submission-1')


def test_submit_job_runs_once_for_a_repeated_submission(context, monkeypatch):
    api = SchedulerAPI(context)
    recorder = SubmitJobRecorder([build_result(accepted=True)])
    monkeypatch.setattr(api, '_submit_job', recorder)

    first = submit(api, build_request())
    second = submit(api, build_request())

    assert len(recorder.calls) == 1
    assert first is second
    assert Utils.get_as_bool(second.accepted, False)


def test_submit_job_allows_resubmission_after_rejection(context, monkeypatch):
    api = SchedulerAPI(context)
    recorder = SubmitJobRecorder(
        [build_result(accepted=False), build_result(accepted=True)]
    )
    monkeypatch.setattr(api, '_submit_job', recorder)

    # a rejected job was never queued: the same form must be submittable again
    assert not Utils.get_as_bool(submit(api, build_request()).accepted, False)
    assert Utils.get_as_bool(submit(api, build_request()).accepted, False)
    assert len(recorder.calls) == 2


def test_submit_job_allows_retry_after_a_failure(context, monkeypatch):
    api = SchedulerAPI(context)
    recorder = SubmitJobRecorder(
        [
            exceptions.soca_exception(errorcodes.JOB_SUBMISSION_FAILED, 'qsub failed'),
            build_result(accepted=True),
        ]
    )
    monkeypatch.setattr(api, '_submit_job', recorder)

    with pytest.raises(exceptions.SocaException):
        submit(api, build_request())
    # the key must have been released, else the retry would raise IN_PROGRESS forever
    assert Utils.get_as_bool(submit(api, build_request()).accepted, False)
    assert len(recorder.calls) == 2


def test_submit_job_does_not_deduplicate_dry_runs(context, monkeypatch):
    api = SchedulerAPI(context)
    recorder = SubmitJobRecorder(
        [build_result(accepted=True), build_result(accepted=True)]
    )
    monkeypatch.setattr(api, '_submit_job', recorder)

    submit(api, build_request(dry_run=True))
    # the real submission that follows shares the form's submission id and must still run
    submit(api, build_request(dry_run=False))

    assert recorder.calls == [DryRunOption.DEFAULT, None]


def test_submit_job_without_a_submission_id_is_unchanged(context, monkeypatch):
    api = SchedulerAPI(context)
    recorder = SubmitJobRecorder(
        [build_result(accepted=True), build_result(accepted=True)]
    )
    monkeypatch.setattr(api, '_submit_job', recorder)

    submit(api, build_request(client_submission_id=None))
    submit(api, build_request(client_submission_id=None))

    # older clients do not send the key and keep the previous behaviour
    assert len(recorder.calls) == 2


def test_submit_job_rejects_a_malformed_submission_id(context, monkeypatch):
    api = SchedulerAPI(context)
    recorder = SubmitJobRecorder([build_result(accepted=True)])
    monkeypatch.setattr(api, '_submit_job', recorder)

    with pytest.raises(exceptions.SocaException) as exc_info:
        submit(api, build_request(client_submission_id='../../etc/passwd'))
    assert exc_info.value.error_code == errorcodes.INVALID_PARAMS
    assert len(recorder.calls) == 0


def _webapp_reply_timeout_seconds():
    """
    The webapp's own timeout, read from source so the two constants cannot drift apart.
    Returns None when the webapp tree is not present.
    """
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        candidate = (
            parent
            / 'source/idea/idea-cluster-manager/webapp/src/client/idea-api-invoker.ts'
        )
        if candidate.exists():
            match = re.search(
                r'SERVICE_WORKER_REPLY_TIMEOUT\s*=\s*(\d+)', candidate.read_text()
            )
            return int(match.group(1)) / 1000 if match else None
    return None


def test_the_dedupe_window_outlasts_the_client_timeout():
    # a user who waits for the visible timeout before retrying must still land inside the
    # window, otherwise the retry becomes a second job
    reply_timeout = _webapp_reply_timeout_seconds()
    if reply_timeout is None:
        pytest.skip('webapp source not available')
    assert scheduler_api.JOB_SUBMISSION_DEDUPE_TTL_SECONDS > reply_timeout, (
        f'dedupe ttl {scheduler_api.JOB_SUBMISSION_DEDUPE_TTL_SECONDS}s must exceed the '
        f'webapp reply timeout {reply_timeout}s'
    )
