"""
Test Cases for the provisioning job comment

a job waiting on capacity idea has not created yet carries the pbs scheduler's own comment
("Can Never Run: ..."), which reads as terminal. these tests cover the replacement comment:
the qalter -W command that writes it, the sanitization that keeps qalter parsable and the
provisioner call site that re-asserts it per attempt.
"""

from typing import List, Optional

from ideadatamodel import SocaJob
from ideascheduler import SchedulerAppContext
from ideascheduler.app.provisioning import JobProvisioner
from ideascheduler.app.provisioning.job_provisioner.job_provisioner import (
    JOB_COMMENT_PROVISIONING,
)
from ideascheduler.app.scheduler.openpbs import openpbs_constants
from ideascheduler.app.scheduler.openpbs.openpbs_scheduler import (
    JOB_COMMENT_MAX_LENGTH,
    OpenPBSScheduler,
    sanitize_job_comment,
)


class MockShellResult:
    def __init__(self, returncode: int):
        self.returncode = returncode

    def __str__(self):
        return f'returncode: {self.returncode}'


class MockShellInvoker:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.invocations: List[List[str]] = []

    def invoke(self, cmd, **_) -> MockShellResult:
        self.invocations.append(cmd)
        return MockShellResult(self.returncode)


class MockScheduler:
    def __init__(self, raise_error: bool = False):
        self.comments = {}
        self.calls = 0
        self.raise_error = raise_error

    def set_job_comment(self, job_id: str, comment: str) -> bool:
        self.calls += 1
        if self.raise_error:
            raise Exception('qalter unavailable')
        self.comments[job_id] = comment
        return True


class MockJobProvisioner:
    """
    stands in for JobProvisioner. _set_provisioning_comment only needs the context and the
    logger, so the real service (and its provisioning queue) is not constructed here.
    """

    def __init__(self, context: SchedulerAppContext):
        self._context = context
        self._logger = context.logger('test-job-provisioner')

    def set_provisioning_comment(
        self, jobs: List[SocaJob], attempt: Optional[int], applied=None
    ):
        return JobProvisioner._set_provisioning_comment(
            self, jobs=jobs, attempt=attempt, applied=applied
        )


def build_job(job_id: str, provisioned: Optional[bool] = False) -> SocaJob:
    return SocaJob(job_id=job_id, owner='mockuser', provisioned=provisioned)


def test_sanitize_job_comment_removes_qalter_hostile_characters():
    """
    qalter -W parses name=value pairs: an '=' in the value makes qalter split the value at the
    preceding comma or fail outright, and an unmatched quote is a syntax error.
    """
    sanitized = sanitize_job_comment(
        'select=2:ncpus=4, "spot" price; capacity \'unavailable\''
    )
    assert '=' not in sanitized
    assert ',' not in sanitized
    assert ';' not in sanitized
    assert '"' not in sanitized
    assert "'" not in sanitized
    # content that is safe must survive, else the comment would be useless
    assert 'select' in sanitized
    assert 'ncpus' in sanitized
    assert 'capacity' in sanitized


def test_sanitize_job_comment_collapses_whitespace_and_bounds_length():
    assert sanitize_job_comment('  provisioning    capacity  ') == (
        'provisioning capacity'
    )

    sanitized = sanitize_job_comment('a' * (JOB_COMMENT_MAX_LENGTH * 2))
    assert len(sanitized) == JOB_COMMENT_MAX_LENGTH

    assert sanitize_job_comment(None) == ''
    assert sanitize_job_comment('') == ''
    # a comment made up entirely of unsafe characters must not become a blank qalter value
    assert sanitize_job_comment('=,;"') == ''


def test_set_job_comment_invokes_qalter_with_w_option(context):
    scheduler = OpenPBSScheduler(context=context)
    shell = MockShellInvoker(returncode=0)
    scheduler._shell = shell

    assert scheduler.set_job_comment(job_id='1234', comment='provisioning capacity')

    assert len(shell.invocations) == 1
    assert shell.invocations[0] == [
        openpbs_constants.QALTER,
        '-W',
        'comment=provisioning capacity',
        '1234',
    ]


def test_set_job_comment_does_not_raise_when_qalter_fails(context):
    scheduler = OpenPBSScheduler(context=context)
    shell = MockShellInvoker(returncode=1)
    scheduler._shell = shell

    # the comment is advisory: a qalter failure must not surface as an exception, or it would
    # abort the provisioning attempt that is only trying to annotate the job.
    assert scheduler.set_job_comment(job_id='1234', comment='provisioning') is False
    assert len(shell.invocations) == 1


def test_set_job_comment_skips_qalter_for_empty_comment(context):
    scheduler = OpenPBSScheduler(context=context)
    shell = MockShellInvoker(returncode=0)
    scheduler._shell = shell

    assert scheduler.set_job_comment(job_id='1234', comment='=,;') is False
    assert len(shell.invocations) == 0


def test_set_provisioning_comment_annotates_unprovisioned_jobs(context):
    scheduler = MockScheduler()
    context.scheduler = scheduler

    MockJobProvisioner(context).set_provisioning_comment(
        jobs=[
            build_job('1001', provisioned=False),
            build_job('1002', provisioned=True),
        ],
        attempt=3,
    )

    assert scheduler.comments == {'1001': f'{JOB_COMMENT_PROVISIONING} (attempt 3)'}
    # the comment must survive sanitization intact, or the job owner sees a mangled reason
    assert (
        sanitize_job_comment(scheduler.comments['1001']) == scheduler.comments['1001']
    )


def test_set_provisioning_comment_writes_each_comment_once_per_pass(context):
    """
    the attempt number is fixed for a provisioning pass, so the retry cycles inside it
    would rewrite the same comment: the pass dict makes the repeat cost no qalter.
    """
    scheduler = MockScheduler()
    context.scheduler = scheduler
    applied = {}
    jobs = [build_job('1001'), build_job('1002')]

    for _ in range(3):
        MockJobProvisioner(context).set_provisioning_comment(
            jobs=jobs, attempt=2, applied=applied
        )

    assert scheduler.calls == 2
    assert scheduler.comments == {
        '1001': f'{JOB_COMMENT_PROVISIONING} (attempt 2)',
        '1002': f'{JOB_COMMENT_PROVISIONING} (attempt 2)',
    }


def test_set_provisioning_comment_without_an_attempt_number(context):
    scheduler = MockScheduler()
    context.scheduler = scheduler

    MockJobProvisioner(context).set_provisioning_comment(
        jobs=[build_job('1001')], attempt=None
    )

    assert scheduler.comments == {'1001': JOB_COMMENT_PROVISIONING}


def test_set_provisioning_comment_swallows_scheduler_errors(context):
    context.scheduler = MockScheduler(raise_error=True)

    # provisioning must proceed even if the scheduler cannot be annotated
    MockJobProvisioner(context).set_provisioning_comment(
        jobs=[build_job('1001')], attempt=1
    )
