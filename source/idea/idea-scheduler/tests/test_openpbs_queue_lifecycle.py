"""
Test Scheduler Queue Lifecycle

covers queue deletion ordering: a queue that cannot be deleted must never be left disabled,
and the failure must be surfaced to the caller instead of being logged and dropped.
"""

from typing import List, Optional

import pytest

from ideadatamodel import exceptions, errorcodes, SocaAnyPayload
from ideadatamodel.scheduler import SocaQueue
from ideascheduler.app.provisioning.job_provisioning_queue.hpc_queue_profiles_dao import (
    HpcQueueProfilesDAO,
)
from ideascheduler.app.provisioning.job_provisioning_queue.hpc_queue_profiles_service import (
    HpcQueueProfilesService,
)
from ideascheduler.app.scheduler.openpbs import openpbs_constants
from ideascheduler.app.scheduler.openpbs.openpbs_scheduler import OpenPBSScheduler

QUEUE_NAME = 'comsol'


class MockShellResult:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stdout = ''
        self.stderr = ''

    def __str__(self):
        return f'returncode: {self.returncode}'


class MockShellInvoker:
    """
    records the qmgr commands executed and replays the queued return codes in order.
    return codes are exhausted in sequence; any additional invocation succeeds.
    """

    def __init__(self, returncodes: List[int]):
        self._returncodes = list(returncodes)
        self.commands: List[str] = []

    def invoke(self, cmd=None, **_) -> MockShellResult:
        self.commands.append(' '.join(cmd))
        if len(self._returncodes) == 0:
            return MockShellResult(returncode=0)
        return MockShellResult(returncode=self._returncodes.pop(0))


def build_scheduler(
    context, returncodes: List[int], existing_queue: Optional[SocaQueue]
) -> (OpenPBSScheduler, MockShellInvoker):
    scheduler = OpenPBSScheduler(context=context)
    shell = MockShellInvoker(returncodes=returncodes)
    scheduler._shell = shell
    scheduler.get_queue = lambda queue: existing_queue
    return scheduler, shell


def delete_queue_commands(shell: MockShellInvoker) -> List[str]:
    return [command for command in shell.commands if 'delete queue' in command]


def set_queue_commands(shell: MockShellInvoker) -> List[str]:
    return [command for command in shell.commands if 'set queue' in command]


def test_delete_queue_deletes_before_disabling(context):
    """
    a queue that can be deleted must not be disabled first
    """
    scheduler, shell = build_scheduler(
        context=context,
        returncodes=[0],
        existing_queue=SocaQueue(name=QUEUE_NAME, enabled=True, started=True),
    )

    scheduler.delete_queue(QUEUE_NAME)

    assert len(delete_queue_commands(shell)) == 1
    assert len(set_queue_commands(shell)) == 0


def test_delete_queue_busy_leaves_queue_enabled(context):
    """
    a busy queue (jobs queued or running) must be left enabled and the failure raised
    """
    scheduler, shell = build_scheduler(
        context=context,
        returncodes=[openpbs_constants.QMGR_ERROR_CODE_OBJECT_BUSY],
        existing_queue=SocaQueue(name=QUEUE_NAME, enabled=True, started=True),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler.delete_queue(QUEUE_NAME)

    assert exc_info.value.error_code == errorcodes.SCHEDULER_QUEUE_BUSY
    assert len(delete_queue_commands(shell)) == 1
    assert len(set_queue_commands(shell)) == 0


def test_delete_queue_retries_with_queue_disabled(context):
    """
    a non-busy failure is retried with the queue disabled
    """
    scheduler, shell = build_scheduler(
        context=context,
        returncodes=[1, 0, 0],
        existing_queue=SocaQueue(name=QUEUE_NAME, enabled=True, started=True),
    )

    scheduler.delete_queue(QUEUE_NAME)

    assert len(delete_queue_commands(shell)) == 2
    assert set_queue_commands(shell) == [
        f'{openpbs_constants.QMGR} -c set queue {QUEUE_NAME} enabled=False,started=False'
    ]


def test_delete_queue_restores_state_when_retry_fails(context):
    """
    when the retry fails too, the previous enabled/started state must be restored
    """
    scheduler, shell = build_scheduler(
        context=context,
        returncodes=[1, 0, 1, 0],
        existing_queue=SocaQueue(name=QUEUE_NAME, enabled=True, started=True),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler.delete_queue(QUEUE_NAME)

    assert exc_info.value.error_code == errorcodes.SCHEDULER_ERROR
    assert 'could not be re-enabled' not in exc_info.value.message
    assert set_queue_commands(shell)[-1] == (
        f'{openpbs_constants.QMGR} -c set queue {QUEUE_NAME} enabled=True,started=True'
    )


def test_delete_queue_surfaces_failed_restore(context):
    """
    a queue left disabled because the restore failed must say so
    """
    scheduler, shell = build_scheduler(
        context=context,
        returncodes=[1, 0, 1, 1],
        existing_queue=SocaQueue(name=QUEUE_NAME, enabled=True, started=True),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler.delete_queue(QUEUE_NAME)

    assert 'could not be re-enabled' in exc_info.value.message


def test_delete_queue_disabled_queue_is_not_re_enabled(context):
    """
    a queue that was already disabled must not be enabled by a failed deletion
    """
    scheduler, shell = build_scheduler(
        context=context,
        returncodes=[1],
        existing_queue=SocaQueue(name=QUEUE_NAME, enabled=False, started=False),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler.delete_queue(QUEUE_NAME)

    assert exc_info.value.error_code == errorcodes.SCHEDULER_ERROR
    assert len(delete_queue_commands(shell)) == 1
    assert len(set_queue_commands(shell)) == 0


def test_delete_queue_not_found(context):
    """
    deleting a queue that does not exist is a no-op
    """
    scheduler, shell = build_scheduler(
        context=context, returncodes=[], existing_queue=None
    )

    scheduler.delete_queue(QUEUE_NAME)

    assert len(shell.commands) == 0


def test_create_queue_does_not_reset_healthy_queue(context):
    """
    an enabled and started queue must not be updated on every create_queue sweep
    """
    scheduler, shell = build_scheduler(
        context=context, returncodes=[], existing_queue=None
    )
    scheduler.list_queues = lambda: [
        SocaQueue(name=QUEUE_NAME, enabled=True, started=True)
    ]

    scheduler.create_queue(queue_name=QUEUE_NAME)

    assert len(shell.commands) == 0


def test_create_queue_enables_stopped_queue(context):
    """
    a queue left disabled by a failed deletion is re-enabled
    """
    scheduler, shell = build_scheduler(
        context=context, returncodes=[0], existing_queue=None
    )
    scheduler.list_queues = lambda: [
        SocaQueue(name=QUEUE_NAME, enabled=False, started=False)
    ]

    scheduler.create_queue(queue_name=QUEUE_NAME)

    assert set_queue_commands(shell) == [
        f'{openpbs_constants.QMGR} -c set queue {QUEUE_NAME} '
        f'queue_type=Execution,started=True,enabled=True'
    ]


def build_queue_profiles_service(context, monkeypatch, delete_queue):
    monkeypatch.setattr(HpcQueueProfilesDAO, '__init__', lambda self, _context: None)
    monkeypatch.setattr(HpcQueueProfilesDAO, 'initialize', lambda self: None)
    context.scheduler = SocaAnyPayload()
    context.scheduler.delete_queue = delete_queue
    return HpcQueueProfilesService(context=context)


def test_delete_queues_returns_failures(context, monkeypatch):
    """
    queue deletion failures must be returned to the caller, not just logged
    """
    busy_queue = 'ansys'

    def delete_queue(queue_name: str):
        if queue_name == busy_queue:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_QUEUE_BUSY, message='returncode: 179'
            )

    service = build_queue_profiles_service(context, monkeypatch, delete_queue)

    failures = service._delete_queues(queue_names=[QUEUE_NAME, busy_queue])

    assert list(failures.keys()) == [busy_queue]
    assert failures[busy_queue].error_code == errorcodes.SCHEDULER_QUEUE_BUSY


def test_queue_deletion_failed_exception(context):
    """
    the raised exception names the queues that are still active
    """
    failures = {
        'ansys': exceptions.SocaException(
            error_code=errorcodes.SCHEDULER_QUEUE_BUSY, message='returncode: 179'
        )
    }

    error = HpcQueueProfilesService._queue_deletion_failed_exception(
        failures=failures, message='queue profile: hpc updated'
    )

    assert error.error_code == errorcodes.SCHEDULER_QUEUE_BUSY
    assert 'queue profile: hpc updated' in error.message
    assert 'ansys: returncode: 179' in error.message


def test_queue_deletion_failed_exception_mixed_error_codes(context):
    """
    heterogeneous failures are reported with the generic scheduler error code
    """
    failures = {
        'ansys': exceptions.SocaException(
            error_code=errorcodes.SCHEDULER_QUEUE_BUSY, message='returncode: 179'
        ),
        'comsol': exceptions.SocaException(
            error_code=errorcodes.SCHEDULER_ERROR, message='returncode: 1'
        ),
    }

    error = HpcQueueProfilesService._queue_deletion_failed_exception(
        failures=failures, message='queue profile: hpc deleted'
    )

    assert error.error_code == errorcodes.SCHEDULER_ERROR
    assert 'ansys' in error.message
    assert 'comsol' in error.message
