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
Test Cases for ProvisioningLifecycleEvents
"""

import datetime
import json
import os

import pytest

from ideadatamodel import (
    errorcodes,
    exceptions,
    SocaJob,
    SocaJobParams,
    SocaJobState,
    SocaScalingMode,
    SocaJobEstimatedBOMCost,
    SocaAmount,
)
from ideasdk.utils import Utils
from ideascheduler import AppContext
from ideascheduler.app.provisioning.lifecycle_events import (
    ProvisioningLifecycleEvents,
    EVENT_SCHEMA_VERSION,
    EVENT_TYPE_ATTEMPT,
    EVENT_TYPE_RETRY,
    EVENT_TYPE_STACK_CREATED,
    EVENT_TYPE_STACK_FAILED,
    EVENT_TYPE_CAPACITY_WAIT,
    EVENT_TYPE_HELD_AT_CAP,
    EVENT_TYPE_DISPOSITION,
    DISPOSITION_RAN,
    DISPOSITION_FAILED,
    DISPOSITION_HELD,
    DISPOSITION_DELETED,
    OUTCOME_PENDING,
    OUTCOME_SUCCESS,
    OUTCOME_FAILURE,
    OUTCOME_CANCELLED,
    REASON_CLASS_BUDGET,
    REASON_CLASS_CAPACITY,
    REASON_CLASS_CLOUDFORMATION,
    REASON_CLASS_LICENSES,
    REASON_CLASS_SERVICE_QUOTA,
    REASON_CLASS_RETRIES_EXHAUSTED,
    REASON_CLASS_UNKNOWN,
)


class EventRecorder:
    """
    stand-in for the rotating file logger. captures emitted JSON lines.
    """

    def __init__(self):
        self.lines = []

    def critical(self, message: str):
        self.lines.append(message)

    @property
    def events(self):
        return [json.loads(line) for line in self.lines]


@pytest.fixture()
def recorder():
    return EventRecorder()


@pytest.fixture()
def lifecycle_events(context: AppContext, recorder: EventRecorder):
    emitter = ProvisioningLifecycleEvents(context=context)
    emitter._event_logger = recorder
    return emitter


def make_job(**kwargs) -> SocaJob:
    params = {
        'job_id': '100',
        'job_uid': 'mock-job-100',
        'name': 'mock-job',
        'owner': 'mockuser',
        'project': 'default',
        'queue': 'normal',
        'queue_type': 'compute',
        'scaling_mode': SocaScalingMode.SINGLE_JOB,
        'state': SocaJobState.QUEUED,
        'cluster_name': 'idea-mock',
    }
    params.update(kwargs)
    return SocaJob(**params)


def test_lifecycle_events_envelope(lifecycle_events, recorder):
    """
    every event carries the versioned envelope and job context fields
    """
    job = make_job()
    lifecycle_events.job_provisioning_attempt(job=job)

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event['schema_version'] == EVENT_SCHEMA_VERSION
    assert event['event_type'] == EVENT_TYPE_ATTEMPT
    assert 'timestamp' in event
    assert 'timestamp_ms' in event
    assert event['cluster_name'] == 'idea-mock'
    assert event['module_id'] == 'scheduler'
    assert event['job_id'] == '100'
    assert event['owner'] == 'mockuser'
    assert event['queue'] == 'normal'
    assert event['queue_profile'] == 'compute'
    assert event['project'] == 'default'
    assert event['attempt_number'] == 1
    assert event['job_count'] == 1
    assert event['outcome'] == OUTCOME_PENDING
    assert event['terminal'] is False


def test_lifecycle_events_single_line_json(lifecycle_events, recorder):
    """
    events must be single-line JSON documents so log shippers can consume them
    """
    lifecycle_events.job_provisioning_attempt(job=make_job())
    assert '\n' not in recorder.lines[0]


def test_lifecycle_events_disabled_no_emission(context, recorder):
    """
    config gate: no events are written when disabled
    """
    context.config().put('scheduler.provisioning_lifecycle_events.enabled', False)
    try:
        emitter = ProvisioningLifecycleEvents(context=context)
        emitter._event_logger = recorder
        emitter.job_provisioning_attempt(job=make_job())
        assert len(recorder.lines) == 0
    finally:
        context.config().put('scheduler.provisioning_lifecycle_events.enabled', True)


def test_lifecycle_events_retry(lifecycle_events, recorder):
    job = make_job()
    lifecycle_events.job_provisioning_retry(
        job=job,
        attempt_number=3,
        error_code=errorcodes.RETRY_JOB_PROVISIONING,
        job_count=5,
    )
    event = recorder.events[0]
    assert event['event_type'] == EVENT_TYPE_RETRY
    assert event['attempt_number'] == 3
    assert event['job_count'] == 5
    assert event['error_code'] == errorcodes.RETRY_JOB_PROVISIONING
    assert event['reason_class'] == REASON_CLASS_CAPACITY


def test_lifecycle_events_stack_created(lifecycle_events, recorder):
    queue_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=120
    )
    job = make_job(queue_time=queue_time)
    lifecycle_events.stack_created(
        job=job, stack_id='arn:aws:cloudformation:stack/mock', job_count=2
    )
    event = recorder.events[0]
    assert event['event_type'] == EVENT_TYPE_STACK_CREATED
    assert event['stack_id'] == 'arn:aws:cloudformation:stack/mock'
    assert event['job_count'] == 2
    assert 118 <= event['time_to_stack_seconds'] <= 125


def test_lifecycle_events_stack_failed_reason_classes(lifecycle_events, recorder):
    job = make_job()

    lifecycle_events.stack_failed(
        job=job, error_code=errorcodes.BUDGETS_LIMIT_EXCEEDED, message='over budget'
    )
    lifecycle_events.stack_failed(
        job=job, error_code=errorcodes.SERVICE_QUOTA_NOT_AVAILABLE
    )
    lifecycle_events.stack_failed(job=job, error_code=errorcodes.NOT_ENOUGH_LICENSES)
    lifecycle_events.stack_failed(
        job=job, error_code=errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED
    )
    lifecycle_events.stack_failed(job=job, error_code='SOME_NEW_ERROR_CODE')

    events = recorder.events
    assert [e['event_type'] for e in events] == [EVENT_TYPE_STACK_FAILED] * 5
    assert events[0]['reason_class'] == REASON_CLASS_BUDGET
    assert events[0]['error_code'] == errorcodes.BUDGETS_LIMIT_EXCEEDED
    assert events[0]['message'] == 'over budget'
    assert events[1]['reason_class'] == REASON_CLASS_SERVICE_QUOTA
    assert events[2]['reason_class'] == REASON_CLASS_LICENSES
    assert events[3]['reason_class'] == REASON_CLASS_CLOUDFORMATION
    assert events[4]['reason_class'] == REASON_CLASS_UNKNOWN


def test_lifecycle_events_stack_failed_explicit_reason_class(
    lifecycle_events, recorder
):
    job = make_job()
    lifecycle_events.stack_failed(
        job=job,
        error_code=errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED,
        reason_class='stack_timeout',
    )
    assert recorder.events[0]['reason_class'] == 'stack_timeout'


def test_lifecycle_events_capacity_wait(lifecycle_events, recorder):
    job = make_job()
    lifecycle_events.capacity_wait(
        job=job,
        error_code=errorcodes.SHARED_CAPACITY_UNAVAILABLE,
        message='not enough capacity',
        job_count=4,
    )
    event = recorder.events[0]
    assert event['event_type'] == EVENT_TYPE_CAPACITY_WAIT
    assert event['error_code'] == errorcodes.SHARED_CAPACITY_UNAVAILABLE
    assert event['job_count'] == 4


def test_lifecycle_events_held_at_cap(lifecycle_events, recorder):
    lifecycle_events.held_at_cap(
        queue_profile='compute',
        limit_type='max_provisioned_instances',
        queue_threshold=10,
        queue_current=12,
        pending_jobs=7,
        job=make_job(),
    )
    event = recorder.events[0]
    assert event['event_type'] == EVENT_TYPE_HELD_AT_CAP
    assert event['queue_profile'] == 'compute'
    assert event['limit_type'] == 'max_provisioned_instances'
    assert event['queue_threshold'] == 10
    assert event['queue_current'] == 12
    assert event['pending_jobs'] == 7
    assert event['job_id'] == '100'


def test_lifecycle_events_disposition_ran(lifecycle_events, recorder):
    queue_time = datetime.datetime(2026, 1, 1, 0, 0, 0)
    provisioning_time = datetime.datetime(2026, 1, 1, 0, 2, 0)
    start_time = datetime.datetime(2026, 1, 1, 0, 3, 0)
    end_time = datetime.datetime(2026, 1, 1, 1, 3, 0)
    job = make_job(
        state=SocaJobState.FINISHED,
        provisioned=True,
        exit_status=0,
        queue_time=queue_time,
        provisioning_time=provisioning_time,
        start_time=start_time,
        end_time=end_time,
    )
    lifecycle_events.job_disposition(job=job)
    event = recorder.events[0]
    assert event['event_type'] == EVENT_TYPE_DISPOSITION
    assert event['disposition'] == DISPOSITION_RAN
    assert event['exit_status'] == 0
    assert event['provisioning_seconds'] == 120
    assert event['queue_wait_seconds'] == 180
    assert event['run_seconds'] == 3600
    assert event['total_seconds'] == 3780
    assert event['queue_time'].startswith('2026-01-01T00:00:00')


def test_lifecycle_events_disposition_failed(lifecycle_events, recorder):
    job = make_job(
        state=SocaJobState.FINISHED,
        provisioned=True,
        exit_status=1,
        start_time=datetime.datetime(2026, 1, 1, 0, 0, 0),
    )
    lifecycle_events.job_disposition(job=job)
    assert recorder.events[0]['disposition'] == DISPOSITION_FAILED


def test_lifecycle_events_disposition_held_is_inferred_from_state(
    lifecycle_events, recorder
):
    """
    a job parked by the retry cap reports 'held', not 'deleted'.

    recorded identically, a job the platform gave up on cannot be told from one somebody
    deleted. 'held' is what makes a retry-capped job legible in Completed Jobs.
    """
    job = make_job(state=SocaJobState.HELD, provisioned=True)
    lifecycle_events.job_disposition(job=job)
    assert recorder.events[0]['disposition'] == DISPOSITION_HELD


def test_lifecycle_events_disposition_deleted_unprovisioned(lifecycle_events, recorder):
    job = make_job(provisioned=False)
    lifecycle_events.job_disposition(job=job)
    assert recorder.events[0]['disposition'] == DISPOSITION_DELETED


def test_lifecycle_events_disposition_explicit_override(lifecycle_events, recorder):
    job = make_job(provisioned=True, exit_status=0)
    lifecycle_events.job_disposition(job=job, disposition=DISPOSITION_DELETED)
    assert recorder.events[0]['disposition'] == DISPOSITION_DELETED


def test_lifecycle_events_disposition_cost_enrichment(lifecycle_events, recorder):
    """
    the disposition event carries the fields needed to price the job (instance type,
    node count, walltime, compute stack, elapsed seconds, estimated cost total).
    """
    job = make_job(
        state=SocaJobState.FINISHED,
        provisioned=True,
        exit_status=0,
        start_time=datetime.datetime(2026, 1, 1, 0, 0, 0),
        end_time=datetime.datetime(2026, 1, 1, 1, 0, 0),
        total_time_secs=3600,
        params=SocaJobParams(
            instance_types=['c5.large'],
            nodes=2,
            walltime='01:00:00',
            compute_stack='mock-compute-stack',
        ),
        estimated_bom_cost=SocaJobEstimatedBOMCost(total=SocaAmount(amount=12.34)),
    )
    lifecycle_events.job_disposition(job=job)
    event = recorder.events[0]
    assert event['instance_type'] == 'c5.large'
    assert event['nodes'] == 2
    assert event['walltime_seconds'] == 3600
    assert event['compute_stack'] == 'mock-compute-stack'
    assert event['total_time_secs'] == 3600
    assert event['estimated_bom_cost_total'] == 12.34


def test_lifecycle_events_disposition_enrichment_optional(lifecycle_events, recorder):
    """
    a job without params must not raise and simply omits the cost enrichment fields.
    """
    job = make_job(provisioned=False)
    lifecycle_events.job_disposition(job=job)
    event = recorder.events[0]
    assert event['event_type'] == EVENT_TYPE_DISPOSITION
    assert 'instance_type' not in event
    assert 'estimated_bom_cost_total' not in event


def test_lifecycle_events_never_raises(context, recorder):
    """
    emission failures must never propagate into the provisioning path
    """
    emitter = ProvisioningLifecycleEvents(context=context)

    class BrokenLogger:
        def critical(self, message):
            raise RuntimeError('disk full')

    emitter._event_logger = BrokenLogger()
    emitter.job_provisioning_attempt(job=make_job())


def test_lifecycle_events_partial_job_no_raise(lifecycle_events, recorder):
    """
    jobs without params must not break stack event emission
    """
    job = SocaJob(job_id='1', state=SocaJobState.QUEUED)
    lifecycle_events.stack_failed(job=job, error_code=errorcodes.GENERAL_ERROR)
    event = recorder.events[0]
    assert event['event_type'] == EVENT_TYPE_STACK_FAILED
    assert 'compute_stack' not in event


def test_finished_job_processor_emits_disposition(context, recorder):
    """
    emission point: ProcessFinishedJob.publish_lifecycle_event
    """
    from ideascheduler.app.provisioning.job_monitor.finished_job_processor import (
        ProcessFinishedJob,
    )

    emitter = ProvisioningLifecycleEvents(context=context)
    emitter._event_logger = recorder
    context.lifecycle_events = emitter
    try:
        job = make_job(
            state=SocaJobState.FINISHED,
            provisioned=True,
            exit_status=0,
            start_time=datetime.datetime(2026, 1, 1, 0, 0, 0),
            end_time=datetime.datetime(2026, 1, 1, 1, 0, 0),
        )
        processor = ProcessFinishedJob(
            context=context,
            logger=context.logger('test'),
            job=job,
            job_export_logger=EventRecorder(),
        )
        processor.publish_lifecycle_event()

        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert event['event_type'] == EVENT_TYPE_DISPOSITION
        assert event['disposition'] == DISPOSITION_RAN
    finally:
        context.lifecycle_events = None


def test_provision_jobs_emits_stack_failed_on_failed_status(context, recorder):
    """
    emission point: ProvisionJobs.invoke on FAILED provisioning status
    """
    from ideadatamodel import ProvisioningStatus
    from ideascheduler.app.provisioning.job_provisioner.job_provisioner import (
        ProvisionJobs,
    )

    emitter = ProvisioningLifecycleEvents(context=context)
    emitter._event_logger = recorder
    context.lifecycle_events = emitter
    try:
        job = make_job(
            provisioned=True,
            params=SocaJobParams(
                stack_id='mock-stack-id',
                compute_stack='idea-mock-compute-onDemand-100',
                job_group='g1234',
            ),
        )
        provision_jobs = ProvisionJobs(
            context=context, jobs=[job], logger=context.logger('test')
        )
        provision_jobs._provisioning_status = ProvisioningStatus.FAILED

        result = provision_jobs.invoke()

        assert result.status is False
        assert result.error_code == errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED
        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert event['event_type'] == EVENT_TYPE_STACK_FAILED
        assert event['error_code'] == errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED
        assert event['reason_class'] == REASON_CLASS_CLOUDFORMATION
    finally:
        context.lifecycle_events = None


def test_lifecycle_events_attempt_number_on_every_event(lifecycle_events, recorder):
    """
    a consumer joins an event to the attempt that produced it, so attempt_number is
    carried by the stack/capacity/disposition events, not only by attempt and retry.
    """
    job = make_job(
        provisioned=True, exit_status=0, start_time=datetime.datetime(2026, 1, 1)
    )
    lifecycle_events.job_provisioning_attempt(job=job, attempt_number=1)
    lifecycle_events.job_provisioning_retry(job=job, attempt_number=2)
    lifecycle_events.stack_created(job=job, stack_id='arn:mock', attempt_number=3)
    lifecycle_events.stack_failed(
        job=job, error_code=errorcodes.GENERAL_ERROR, attempt_number=4
    )
    lifecycle_events.capacity_wait(
        job=job, error_code=errorcodes.SHARED_CAPACITY_UNAVAILABLE, attempt_number=5
    )
    lifecycle_events.held_at_cap(queue_profile='compute', job=job, attempt_number=6)
    lifecycle_events.job_disposition(job=job, attempt_number=7)

    events = recorder.events
    assert [e['attempt_number'] for e in events] == [1, 2, 3, 4, 5, 6, 7]
    assert all(e['job_id'] == '100' for e in events)
    assert all('timestamp' in e for e in events)


def test_lifecycle_events_outcome_and_terminal(lifecycle_events, recorder):
    """
    outcome and terminal let a consumer aggregate without its own event_type lookup table
    """
    job = make_job()
    lifecycle_events.job_provisioning_attempt(job=job)
    lifecycle_events.stack_created(job=job, stack_id='arn:mock')
    lifecycle_events.stack_failed(job=job, error_code=errorcodes.GENERAL_ERROR)
    lifecycle_events.capacity_wait(job=job)
    lifecycle_events.held_at_cap(queue_profile='compute', job=job)

    assert [
        (e['event_type'], e['outcome'], e['terminal']) for e in recorder.events
    ] == [
        (EVENT_TYPE_ATTEMPT, OUTCOME_PENDING, False),
        (EVENT_TYPE_STACK_CREATED, OUTCOME_SUCCESS, False),
        (EVENT_TYPE_STACK_FAILED, OUTCOME_FAILURE, False),
        (EVENT_TYPE_CAPACITY_WAIT, OUTCOME_PENDING, False),
        (EVENT_TYPE_HELD_AT_CAP, OUTCOME_PENDING, False),
    ]


def test_lifecycle_events_terminal_dispositions_are_distinguishable(
    lifecycle_events, recorder
):
    """
    the four ways provisioning ends for a job must be tellable apart: it ran, it failed,
    the retry cap parked it, or it was deleted. a held job is not terminal - it stays in
    the queue until it is released or deleted.
    """
    ran = make_job(
        provisioned=True, exit_status=0, start_time=datetime.datetime(2026, 1, 1)
    )
    failed = make_job(
        provisioned=True, exit_status=2, start_time=datetime.datetime(2026, 1, 1)
    )
    lifecycle_events.job_disposition(job=ran)
    lifecycle_events.job_disposition(job=failed)
    lifecycle_events.job_held(
        job=make_job(),
        attempt_number=3,
        error_code=errorcodes.JOB_PROVISIONING_RETRIES_EXHAUSTED,
        message='held after 3 attempts',
    )
    lifecycle_events.job_disposition(job=make_job(provisioned=False))

    events = recorder.events
    assert all(e['event_type'] == EVENT_TYPE_DISPOSITION for e in events)
    assert [e['disposition'] for e in events] == [
        DISPOSITION_RAN,
        DISPOSITION_FAILED,
        DISPOSITION_HELD,
        DISPOSITION_DELETED,
    ]
    assert [e['outcome'] for e in events] == [
        OUTCOME_SUCCESS,
        OUTCOME_FAILURE,
        OUTCOME_FAILURE,
        OUTCOME_CANCELLED,
    ]
    assert [e['terminal'] for e in events] == [True, True, False, True]
    assert events[2]['reason_class'] == REASON_CLASS_RETRIES_EXHAUSTED
    assert events[2]['attempt_number'] == 3


def test_lifecycle_events_cycle_attempt_is_separate_from_attempt_number(
    lifecycle_events, recorder
):
    """
    the in-cycle retry index must not be written into attempt_number: a consumer grouping
    by (job_id, attempt_number) would merge the first retry of every provisioning cycle.
    """
    # the two values are never equal on any single event, so writing one into the other
    # cannot pass unnoticed
    job = make_job()
    lifecycle_events.job_provisioning_attempt(
        job=job, attempt_number=2, cycle_attempt=1
    )
    lifecycle_events.job_provisioning_retry(job=job, attempt_number=2, cycle_attempt=3)
    lifecycle_events.stack_created(
        job=job, stack_id='arn:mock', attempt_number=2, cycle_attempt=4
    )
    lifecycle_events.stack_failed(
        job=job,
        error_code=errorcodes.GENERAL_ERROR,
        attempt_number=2,
        cycle_attempt=5,
    )
    lifecycle_events.capacity_wait(job=job, attempt_number=2, cycle_attempt=6)

    events = recorder.events
    assert [e['attempt_number'] for e in events] == [2, 2, 2, 2, 2]
    assert [e['cycle_attempt'] for e in events] == [1, 3, 4, 5, 6]


def test_lifecycle_events_attempt_number_absent_when_no_attempt_consumed(
    lifecycle_events, recorder
):
    """
    attempt_number is a 1-based index, so a job deleted before it was ever provisioned
    reports no attempt rather than attempt 0.
    """
    lifecycle_events.job_disposition(job=make_job(provisioned=False), attempt_number=0)

    event = recorder.events[0]
    assert event['disposition'] == DISPOSITION_DELETED
    assert 'attempt_number' not in event


def test_attempt_counters_agree_on_the_same_attempt(context, recorder):
    """
    the in-flight counter and the counter stamped by the retry cap have to name the same
    attempt, or an event emitted during an attempt cannot be joined to its outcome.
    """
    from ideascheduler.app.provisioning import JobCache

    context.job_cache = JobCache(context=context)
    emitter = ProvisioningLifecycleEvents(context=context)
    emitter._event_logger = recorder
    job = make_job(job_id=Utils.short_uuid())

    assert emitter.current_attempt(job=job) == 1
    assert emitter.attempts_consumed(job=job) == 0

    for expected in (1, 2, 3):
        failed_attempt = context.job_cache.increment_job_provisioning_retry(
            job_id=job.job_id
        )
        # the attempt that just failed is the one that was in flight a moment ago
        assert failed_attempt == expected
        assert emitter.attempts_consumed(job=job) == expected
        assert emitter.current_attempt(job=job) == expected + 1


class HoldRecordingScheduler:
    def __init__(self, hold_error: bool = False):
        self.held_jobs = []
        self._hold_error = hold_error

    def hold_job(self, job_id: str) -> bool:
        if self._hold_error:
            raise exceptions.soca_exception(
                error_code=errorcodes.SCHEDULER_ERROR, message='qhold failed'
            )
        self.held_jobs.append(job_id)
        return True

    def set_job_attributes(self, job_id: str, attributes) -> bool:
        return True

    def set_job_comment(self, job_id: str, comment: str) -> bool:
        return True


def exhaust_retry_cap(context, recorder, hold_error: bool = False):
    from ideascheduler.app.provisioning import JobCache, JobProvisioningUtil

    context.job_cache = JobCache(context=context)
    context.scheduler = HoldRecordingScheduler(hold_error=hold_error)
    emitter = ProvisioningLifecycleEvents(context=context)
    emitter._event_logger = recorder
    context.lifecycle_events = emitter

    job = make_job(job_id=Utils.short_uuid(), params=SocaJobParams(nodes=1, cpus=1))
    provisioning_util = JobProvisioningUtil(context=context, jobs=[job])
    results = [
        provisioning_util.track_provisioning_failure(job=job, message='mock failure')
        for _ in range(3)
    ]
    assert results == [False, False, True]
    return job


def test_retry_cap_emits_held_disposition(context, recorder):
    """
    emission point: the retry cap parks a job, so the stream must carry a held
    disposition. a stack-failed alone reads the same as a failure that will be retried.
    """
    try:
        job = exhaust_retry_cap(context=context, recorder=recorder)

        dispositions = [
            e for e in recorder.events if e['event_type'] == EVENT_TYPE_DISPOSITION
        ]
        assert len(dispositions) == 1
        event = dispositions[0]
        assert event['disposition'] == DISPOSITION_HELD
        assert event['outcome'] == OUTCOME_FAILURE
        assert event['terminal'] is False
        assert event['attempt_number'] == 3
        assert event['error_code'] == errorcodes.JOB_PROVISIONING_RETRIES_EXHAUSTED
        assert event['reason_class'] == REASON_CLASS_RETRIES_EXHAUSTED
        assert event['job_id'] == job.job_id
    finally:
        context.lifecycle_events = None


def test_retry_cap_emits_disposition_when_hold_fails(context, recorder):
    """
    a job whose hold failed is excluded from re-queue but is not held in the scheduler
    either. that state needs its own event, flagged so it is not read as a clean hold.
    """
    try:
        exhaust_retry_cap(context=context, recorder=recorder, hold_error=True)

        dispositions = [
            e for e in recorder.events if e['event_type'] == EVENT_TYPE_DISPOSITION
        ]
        assert len(dispositions) == 1
        event = dispositions[0]
        assert event['disposition'] == DISPOSITION_HELD
        assert event['hold_failed'] is True
        assert event['terminal'] is False
        assert event['attempt_number'] == 3
    finally:
        context.lifecycle_events = None


def test_retry_cap_held_disposition_is_not_flagged_when_hold_succeeds(
    context, recorder
):
    """
    the flag must separate the two cases, not be set on every held event
    """
    try:
        exhaust_retry_cap(context=context, recorder=recorder)
        dispositions = [
            e for e in recorder.events if e['event_type'] == EVENT_TYPE_DISPOSITION
        ]
        assert [e['hold_failed'] for e in dispositions] == [False]
    finally:
        context.lifecycle_events = None


def test_lifecycle_events_are_parseable_records_on_disk(context, tmp_path, monkeypatch):
    """
    the events have to be readable by a log shipper, so parse the file the emitter really
    writes instead of trusting the payload it hands the logger. covers a success path
    (attempt, stack created, job ran) and a failure path (stack failed, job held).
    """
    # write to a per-test log directory, so the assertions never read a file an earlier run
    # left in the shared app deploy directory
    monkeypatch.setattr(context.logging(), 'get_log_dir', lambda: str(tmp_path))

    emitter = ProvisioningLifecycleEvents(context=context)
    event_logger = emitter._get_event_logger()
    log_file = event_logger.handlers[0].baseFilename

    def read_lines():
        if not os.path.isfile(log_file):
            return []
        with open(log_file) as f:
            return [line for line in f.read().splitlines() if line.strip()]

    job = make_job(queue_time=datetime.datetime(2026, 1, 1))
    emitter.job_provisioning_attempt(job=job, attempt_number=1, job_count=2)
    emitter.stack_created(
        job=job, stack_id='arn:aws:cloudformation:stack/mock', attempt_number=1
    )
    emitter.job_disposition(
        job=make_job(
            provisioned=True,
            exit_status=0,
            queue_time=datetime.datetime(2026, 1, 1),
            start_time=datetime.datetime(2026, 1, 1, 0, 5),
            end_time=datetime.datetime(2026, 1, 1, 1, 5),
        ),
        attempt_number=1,
    )
    emitter.stack_failed(
        job=job,
        error_code=errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED,
        # a multi-line failure message must not split the record
        message='stack rollback\nreason: insufficient capacity',
        attempt_number=2,
    )
    emitter.job_held(
        job=job,
        attempt_number=3,
        error_code=errorcodes.JOB_PROVISIONING_RETRIES_EXHAUSTED,
        message='held after 3 attempts',
    )
    for handler in event_logger.handlers:
        handler.flush()

    lines = read_lines()
    assert len(lines) == 5

    events = [json.loads(line) for line in lines]
    for event in events:
        assert event['schema_version'] == EVENT_SCHEMA_VERSION
        assert event['job_id'] == '100'
        assert event['attempt_number'] in (1, 2, 3)
        assert event['outcome'] in (
            OUTCOME_PENDING,
            OUTCOME_SUCCESS,
            OUTCOME_FAILURE,
            OUTCOME_CANCELLED,
        )
        assert isinstance(event['terminal'], bool)
        assert isinstance(event['timestamp_ms'], int)
        datetime.datetime.fromisoformat(event['timestamp'])

    assert [e['event_type'] for e in events] == [
        EVENT_TYPE_ATTEMPT,
        EVENT_TYPE_STACK_CREATED,
        EVENT_TYPE_DISPOSITION,
        EVENT_TYPE_STACK_FAILED,
        EVENT_TYPE_DISPOSITION,
    ]
    assert events[2]['disposition'] == DISPOSITION_RAN
    assert events[2]['terminal'] is True
    assert events[3]['message'] == 'stack rollback\nreason: insufficient capacity'
    assert events[4]['disposition'] == DISPOSITION_HELD
    assert events[4]['terminal'] is False
