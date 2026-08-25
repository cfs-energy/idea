#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

"""
Test Cases for Scheduler.SubmitJob refusal handling

a job the submit hook refused has no job id and was never queued. these pin that a real
submission surfaces that as JOB_SUBMISSION_FAILED, while a dry run - whose whole output is
the same refusal - still returns its report.
"""

import os
import shutil

import pytest

from ideadatamodel import errorcodes, exceptions, SocaAnyPayload, SocaJob
from ideadatamodel.scheduler import DryRunOption, SubmitJobRequest, SubmitJobResult
from ideascheduler.app.api import scheduler_api as scheduler_api_module
from ideascheduler.app.api.scheduler_api import SchedulerAPI

JOB_SCRIPT_TEXT = '#!/bin/bash\n#PBS -N mockjob\n#PBS -l select=1:ncpus=1\necho hello\n'

# what qsub writes when the submit hook refuses the job. the reason reaches the client
# only through this text, so the test asserts it survives into the raised message.
QSUB_REFUSAL = 'qsub: Job submission failed: project PROJECT_DOES_NOT_EXIST is invalid'

# the refusal reaches the client only through this text, so each test asserts it survives


def build_request(dry_run=None) -> SubmitJobRequest:
    from ideasdk.utils import Utils

    return SubmitJobRequest(
        job_script_interpreter='pbs',
        job_script=Utils.base64_encode(JOB_SCRIPT_TEXT),
        dry_run=dry_run,
    )


class MockShell:
    def __init__(self, returncode: int, stdout: str, stderr: str = ''):
        self.result = SocaAnyPayload()
        self.result.returncode = returncode
        self.result.stdout = stdout
        self.result.stderr = stderr

    def invoke(self, *_args, **_kwargs):
        return self.result


class MockTracker:
    """stands in for the hook: returns whatever the hook would have recorded."""

    def __init__(self, recorded):
        self.recorded = recorded

    def get(self, _job_uid):
        return self.recorded


def build_api(context, monkeypatch, tmp_path, returncode, stdout, recorded, stderr=''):
    api = SchedulerAPI(context)

    mount_dir = str(tmp_path)

    class MockConfigView:
        @staticmethod
        def get_string(key, required=False):
            assert key == 'shared-storage.data.mount_dir'
            return mount_dir

    monkeypatch.setattr(api.context, 'config', lambda: MockConfigView())
    monkeypatch.setattr(api.context, 'shell', MockShell(returncode, stdout, stderr))
    monkeypatch.setattr(api.context, 'job_submission_tracker', MockTracker(recorded))
    monkeypatch.setattr(
        api.group_name_helper, 'get_user_group', lambda _user: 'mockgroup'
    )
    # the tests do not run as root and ownership is not what is under test
    monkeypatch.setattr(shutil, 'chown', lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler_api_module, 'chown_fd', lambda *_a, **_k: None)
    return api


def submitted_scripts(tmp_path) -> list:
    jobs_dir = os.path.join(str(tmp_path), 'home', 'mockuser', 'jobs')
    if not os.path.isdir(jobs_dir):
        return []
    return sorted(os.listdir(jobs_dir))


def test_refused_submission_raises_job_submission_failed(
    context, monkeypatch, tmp_path
):
    # the hook refused the job: qsub exits non-zero and the recorded result is not accepted
    api = build_api(
        context,
        monkeypatch,
        tmp_path,
        returncode=1,
        stdout='',
        stderr=QSUB_REFUSAL,
        recorded=SubmitJobResult(accepted=False, job=SocaJob(job_id=None)),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        api._submit_job(request=build_request(), job_owner='mockuser', dry_run=None)

    assert exc_info.value.error_code == errorcodes.JOB_SUBMISSION_FAILED
    # the hook's text verbatim: the shell result repr would also contain it, so the
    # equality is what tells the two apart
    assert exc_info.value.message == f'Failed to submit job: {QSUB_REFUSAL}'
    assert submitted_scripts(tmp_path) == []


def test_refused_dry_run_returns_the_validation_report(context, monkeypatch, tmp_path):
    # a dry run is refused by design - the report is the answer, not an error
    recorded = SubmitJobResult(accepted=False, job=SocaJob(job_id=None))
    api = build_api(
        context,
        monkeypatch,
        tmp_path,
        returncode=1,
        stdout='',
        stderr=QSUB_REFUSAL,
        recorded=recorded,
    )

    result = api._submit_job(
        request=build_request(dry_run=True),
        job_owner='mockuser',
        dry_run=DryRunOption.DEFAULT,
    )

    assert result is recorded
    assert result.accepted is False


def test_accepted_submission_returns_the_job_id(context, monkeypatch, tmp_path):
    recorded = SubmitJobResult(accepted=True, job=SocaJob(job_id=None))
    api = build_api(
        context,
        monkeypatch,
        tmp_path,
        returncode=0,
        stdout='1234.mockcluster',
        recorded=recorded,
    )

    result = api._submit_job(
        request=build_request(), job_owner='mockuser', dry_run=None
    )

    assert result.accepted is True
    assert result.job.job_id == '1234'
    # a queued job keeps its script in the owner's jobs dir; a refused one is removed
    assert submitted_scripts(tmp_path) == []


def test_refused_submission_leaves_no_job_id_on_the_result(
    context, monkeypatch, tmp_path
):
    # guards the reporting contract the integration harness depends on: a refusal must not
    # reach a caller as a success carrying a job id it could poll
    api = build_api(
        context,
        monkeypatch,
        tmp_path,
        returncode=1,
        stdout=QSUB_REFUSAL,
        stderr='',
        recorded=SubmitJobResult(accepted=False, job=SocaJob(job_id=None)),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        api._submit_job(request=build_request(), job_owner='mockuser', dry_run=None)

    # qsub wrote to stdout rather than stderr; the reason must still reach the caller
    assert exc_info.value.message == f'Failed to submit job: {QSUB_REFUSAL}'
    assert submitted_scripts(tmp_path) == []
