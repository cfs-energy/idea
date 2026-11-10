"""
An array job (qsub -J) is one job under its own id, 357[]: one job cache row, one stack, one
finished record, one pair of emails. Its subjobs, 357[1] and so on, share its job_uid and are
runs of that job. PBS reports each subjob's run, begin and end under the subjob's id, lists
only the array in plain qselect, and keeps the last subjob's exit status on the array.

The PBS shapes used here were read on a cluster running OpenPBS 23.06.06.
"""

import json
import logging
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import arrow
import pytest

from ideadatamodel import (
    EC2Instance,
    SocaAnyPayload,
    SocaComputeNode,
    SocaJob,
    SocaJobExecution,
    SocaJobExecutionHost,
    SocaJobExecutionRun,
    SocaJobParams,
    SocaJobState,
    SocaJobSubjob,
    constants,
)
from ideadatamodel.scheduler.scheduler_model import SocaJobProvisioningOptions
from ideasdk.utils import Utils
from ideascheduler.app.notifications.job_notifications import JobNotifications
from ideascheduler.app.provisioning.job_monitor import finished_job_processor
from ideascheduler.app.provisioning.job_monitor.job_cache import JobCache
from ideascheduler.app.provisioning.job_monitor.job_monitor import (
    JobMonitor,
    JobMonitorState,
)
from ideascheduler.app.provisioning.node_monitor.node_house_keeper import (
    NodeHouseKeepingSession,
)
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSJob
from ideascheduler.app.scheduler.openpbs.openpbs_qselect import OpenPBSQSelect
from ideascheduler.app.scheduler.openpbs.openpbs_qstat import (
    OpenPBSQStat,
    array_indices,
)
from ideascheduler.app.scratch_cleanup.job_state import ScratchJobState, job_succeeded
from test_finished_job_record import (
    FakeDocumentStore,
    FakeJobNotifications,
    FakeLifecycleEvents,
    FakeMetrics,
    PricingSpy,
    BudgetsSpy,
    build_poller,
)
from test_job_cache import FakeSchedulerContext
from test_job_monitor import FakeContext as MonitorContext

LOGGER = logging.getLogger('test_array_jobs')
UID = 'array-uid-0001'
START = arrow.get('2026-11-09T22:12:32Z')


def array_job(job_id='7[]', state=SocaJobState.QUEUED, **fields) -> SocaJob:
    values = dict(
        job_id=job_id,
        job_uid=UID,
        name='af-mixed',
        owner='alice',
        project='default',
        queue='test',
        queue_type='test',
        state=state,
        provisioned=True,
        provisioning_options=SocaJobProvisioningOptions(),
        params=SocaJobParams(walltime='01:00:00', nodes=1, cpus=1),
    )
    values.update(fields)
    return SocaJob(**values)


def subjob(index, exit_status=0, terminated=False, start=0, seconds=30, **fields):
    values = dict(
        job_id=f'7[{index}]',
        state=SocaJobState.FINISHED,
        exit_status=exit_status,
        terminated=terminated,
        start_time=START.shift(seconds=start).datetime,
        end_time=START.shift(seconds=start + seconds).datetime,
    )
    values.update(fields)
    return SocaJobSubjob(**values)


def seed_row(cache: JobCache, job: SocaJob):
    """a jobs-table row as an earlier build wrote it; the cache itself refuses a subjob's"""
    cache.get_jobs_table().insert(
        {
            'job_id': job.job_id,
            'job_uid': job.job_uid,
            'state': job.state.value,
            'owner': job.owner,
            'queue': job.queue,
            'queue_profile': job.queue_type,
            'provisioned': job.is_provisioned(),
            'job_data': Utils.to_json(job),
        }
    )


def cached_ids(cache: JobCache):
    return sorted(job.job_id for job in cache.list_jobs())


class Pbs:
    """what PBS answers: plain qselect, qstat -x by id, and an array's subjob history"""

    def __init__(self):
        self.active = []
        self.jobs = {}
        self.subjobs = {}
        self.subjob_error = None
        self.on_qselect = None

    def list_jobs_ids(self, raise_on_error=False):
        if self.on_qselect:
            self.on_qselect()
        return list(self.active)

    def list_jobs(self, job_ids=None, **_):
        return [self.jobs[job_id] for job_id in job_ids or [] if job_id in self.jobs]

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def get_array_subjobs(self, job_id):
        if self.subjob_error:
            raise self.subjob_error
        return self.subjobs.get(job_id, (None, []))


class CycleExit(Event):
    def wait(self, timeout=None):
        self.set()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    PricingSpy.reset()
    BudgetsSpy.reset()
    monkeypatch.setattr(finished_job_processor, 'PricingHelper', PricingSpy)
    monkeypatch.setattr(finished_job_processor, 'AwsBudgetsHelper', BudgetsSpy)
    pbs = Pbs()
    monkeypatch.setattr(finished_job_processor, 'OpenPBSQSelect', lambda context: pbs)
    cache = JobCache(FakeSchedulerContext(str(tmp_path)))
    context = SocaAnyPayload(
        job_cache=cache,
        lifecycle_events=FakeLifecycleEvents(),
        document_store=FakeDocumentStore(),
        scheduler=pbs,
        metrics=FakeMetrics(),
        job_notifications=FakeJobNotifications(),
    )
    context.config = lambda: Mock(get_int=lambda key, default=None: default)
    yield SimpleNamespace(context=context, cache=cache, pbs=pbs)
    cache.stop()


def poll(world):
    poller = build_poller(world.context)
    poller._exit = CycleExit()
    poller._poll_finished_jobs()


def records(world):
    return world.cache.list_completed_jobs()


# ------------------------------------------------------------------ what PBS sends


def test_qstat_writes_a_lone_array_index_as_a_number():
    """
    "array_index":1 on every subjob, and "array_indices_remaining":3 on an array with one
    index left. Read as text only, the first failed every subjob's run sync and the second
    every listing that included that array, cluster-wide.
    """
    subjob_entry = OpenPBSJob(id='357[1].scheduler', job_state='R', array_index=1)
    parent = OpenPBSJob(
        id='358[].scheduler',
        job_state='B',
        array_indices_submitted='1-3',
        array_indices_remaining=3,
    )
    assert subjob_entry.array_index == '1'
    assert parent.array_indices_remaining == '3'
    assert OpenPBSJob(array_indices_remaining='2-3').array_indices_remaining == '2-3'
    assert OpenPBSJob(array_indices_remaining='-').array_indices_remaining == '-'


def test_a_text_field_qstat_writes_as_a_number_does_not_fail_the_listing():
    """qsub -A 1234 and -P 2026: the same bare number, on fields anyone can set"""
    job = OpenPBSJob(id='12.scheduler', Account_Name=1234, project=2026, comment=1.5)
    assert (job.Account_Name, job.project, job.comment) == ('1234', '2026', '1.5')


def test_a_qstat_listing_with_a_running_subjob_is_read():
    context = Mock()
    context.queue_profiles.get_queue_profile.return_value = None
    listing = json.dumps(
        {
            'Jobs': {
                '357[1].scheduler': {
                    'job_state': 'R',
                    'queue': 'test',
                    'array_id': '357[].scheduler',
                    'array_index': 1,
                }
            }
        }
    )
    qstat = OpenPBSQStat(context=context, logger=LOGGER, shell=None, converter=None)
    assert qstat._handle_qstat_result(listing) == []


@pytest.mark.parametrize(
    'submitted, expected',
    [
        ('1-3', [1, 2, 3]),
        ('1-10:4', [1, 5, 9]),
        ('7', [7]),
        ('0-1', [0, 1]),
        ('1-9:4%2', [1, 5, 9]),
        ('1-3%1', [1, 2, 3]),
        (None, None),
        ('', None),
        ('1-3,7', None),
    ],
)
def test_the_indices_an_array_was_submitted_with(submitted, expected):
    found = array_indices(submitted)
    assert (list(found) if found is not None else None) == expected


def history_entry(state='F', exit_status=0, substate=92, start=0, seconds=30, **fields):
    values = {
        'job_state': state,
        'substate': substate,
        'Exit_status': exit_status,
        'stime': str(START.shift(seconds=start).int_timestamp),
        'resources_used': {'walltime': f'00:00:{seconds:02d}'},
    }
    values.update(fields)
    return {key: value for key, value in values.items() if value is not None}


class Shell:
    def __init__(self, answers):
        self.answers = answers
        self.commands = []

    def invoke(self, cmd, **_):
        self.commands.append(cmd)
        asked = tuple(cmd[cmd.index('json') + 1 :])
        returncode, jobs = self.answers.get(asked, (153, None))
        stdout = json.dumps({'Jobs': jobs}) if jobs is not None else ''
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr='')


PARENT_HISTORY = {
    'job_state': 'F',
    'substate': 93,
    'Exit_status': 0,
    'array': 'True',
    'array_indices_submitted': '1-2',
    'array_indices_remaining': '-',
}


def test_an_arrays_subjobs_are_read_from_its_history():
    shell = Shell(
        {
            ('-t', '359[]'): (
                0,
                {
                    '359[].scheduler': PARENT_HISTORY,
                    '359[1].scheduler': history_entry(
                        exit_status=1, substate=93, queue_rank='not-a-number'
                    ),
                    '359[2].scheduler': history_entry(start=30, seconds=59),
                    # -t with no id lists every array; another array's subjob is not ours
                    '360[1].scheduler': history_entry(),
                },
            )
        }
    )
    qstat = OpenPBSQStat(context=Mock(), logger=LOGGER, shell=shell, converter=None)
    submitted, subjobs = qstat.get_array_subjobs('359[]')
    assert submitted == 2
    assert shell.commands == [
        ['/opt/pbs/bin/qstat', '-E', '-x', '-f', '-F', 'json', '-t', '359[]']
    ]
    by_id = {found.job_id: found for found in subjobs}
    assert sorted(by_id) == ['359[1]', '359[2]']
    assert by_id['359[1]'].exit_status == 1
    assert by_id['359[1]'].terminated is False
    assert by_id['359[1]'].state == SocaJobState.FINISHED
    assert by_id['359[2]'].start_time == START.shift(seconds=30).datetime
    assert by_id['359[2]'].end_time == START.shift(seconds=89).datetime


def test_subjobs_that_t_leaves_out_are_asked_for_by_id():
    """qstat -x -f on one subjob id is known to answer; -t with -f was not seen"""
    shell = Shell(
        {
            ('-t', '359[]'): (0, {'359[].scheduler': PARENT_HISTORY}),
            ('359[1]', '359[2]'): (
                153,
                {'359[2].scheduler': history_entry(substate=91, exit_status=None)},
            ),
        }
    )
    qstat = OpenPBSQStat(context=Mock(), logger=LOGGER, shell=shell, converter=None)
    submitted, subjobs = qstat.get_array_subjobs('359[]')
    assert submitted == 2
    assert [found.job_id for found in subjobs] == ['359[2]']
    assert subjobs[0].terminated is True


def test_subjobs_are_still_read_if_qstat_refuses_t_with_history():
    shell = Shell(
        {
            ('-t', '359[]'): (2, None),
            ('359[]',): (0, {'359[].scheduler': PARENT_HISTORY}),
            ('359[1]', '359[2]'): (
                0,
                {
                    '359[1].scheduler': history_entry(exit_status=1, substate=93),
                    '359[2].scheduler': history_entry(),
                },
            ),
        }
    )
    qstat = OpenPBSQStat(context=Mock(), logger=LOGGER, shell=shell, converter=None)
    submitted, subjobs = qstat.get_array_subjobs('359[]')
    assert submitted == 2
    assert sorted(found.job_id for found in subjobs) == ['359[1]', '359[2]']


def test_a_failed_history_read_is_not_an_array_with_no_subjobs():
    shell = Shell({('-t', '359[]'): (1, None), ('359[]',): (1, None)})
    qstat = OpenPBSQStat(context=Mock(), logger=LOGGER, shell=shell, converter=None)
    with pytest.raises(Exception):
        qstat.get_array_subjobs('359[]')


def test_the_scheduler_the_app_builds_can_read_subjob_history():
    """the app talks to PBS through a wrapper; a method missing there stops it starting"""
    from ideascheduler.app.scheduler.openpbs import OpenPBSScheduler
    from ideascheduler.app.scheduler.soca_scheduler import SocaScheduler

    assert not SocaScheduler.__abstractmethods__
    assert not OpenPBSScheduler.__abstractmethods__
    wrapper = SocaScheduler.__new__(SocaScheduler)
    wrapper._scheduler = Mock()
    wrapper._scheduler.get_array_subjobs.return_value = (2, [])
    assert wrapper.get_array_subjobs('359[]') == (2, [])


def test_qselect_names_a_begun_array_by_its_own_letter():
    """the first letter of subjob_begun is S, which PBS reads as suspended"""
    select = OpenPBSQSelect(
        context=None,
        shell=Mock(),
        job_state=[
            SocaJobState.QUEUED,
            SocaJobState.HELD,
            SocaJobState.SUBJOB_BEGUN,
            SocaJobState.SUBJOB_EXPIRED,
            SocaJobState.SUSPENDED,
        ],
    )
    assert select.get_queue_select_state_filter() == 'QHBXS'


def test_an_array_and_its_subjobs_share_one_identity():
    from ideadatamodel.model_utils import ModelUtils

    assert ModelUtils.array_parent_id('357[1]') == '357[]'
    assert ModelUtils.array_parent_id('357[0]') == '357[]'
    for not_a_subjob in ('357[]', '357', '357[1].scheduler', '357[x]', '', None):
        assert ModelUtils.array_parent_id(not_a_subjob) is None
    assert ModelUtils.is_array_job_id('357[]')
    assert not ModelUtils.is_array_job_id('357[1]') and not ModelUtils.is_array_job_id(
        None
    )
    # the stack, its log group and the idea:JobId tag are the array's
    assert (
        ModelUtils.aws_job_id('357[1]') == ModelUtils.aws_job_id('357[]') == '357-array'
    )


# ------------------------------------------------------------------ the job cache


def test_the_job_cache_tracks_an_array_and_never_a_subjob(world):
    world.cache.sync(
        [array_job('7[]'), array_job('7[1]', SocaJobState.RUNNING), array_job('8')]
    )
    assert cached_ids(world.cache) == ['7[]', '8']


def test_a_begun_array_counts_against_the_running_job_limit(world):
    world.cache.sync([array_job('7[]', SocaJobState.SUBJOB_BEGUN)])
    assert world.cache.get_active_jobs('test') == 1


def test_execution_hosts_kept_under_a_subjob_id_are_dropped_at_start(tmp_path):
    cache = JobCache(FakeSchedulerContext(str(tmp_path)))
    for job_id in ('357[1]', '357[]', '358'):
        cache.log_job_execution(job_id, SocaJobExecutionHost(host='node-1'))
    cache.stop()
    reopened = JobCache(FakeSchedulerContext(str(tmp_path)))
    try:
        assert reopened.get_job_execution_hosts('357[1]') == []
        assert len(reopened.get_job_execution_hosts('357[]')) == 1
        assert len(reopened.get_job_execution_hosts('358')) == 1
    finally:
        reopened.stop()


# ------------------------------------------------------------------ run events


@pytest.fixture()
def monitor(world):
    context = MonitorContext()
    context.job_cache = world.cache
    context.scheduler = world.pbs
    context.metrics = Mock()
    context.job_notifications = Mock()
    job_monitor = JobMonitor(context=context)
    job_monitor._exit = Event()
    job_monitor._state = JobMonitorState(context=context)
    job_monitor.context = context
    return job_monitor


def run_event(index) -> SocaJob:
    """a runjob event names the subjob and carries none of the array's resources"""
    return SocaJob(job_id=f'7[{index}]', queue='test', queue_type='test', owner='alice')


def sync_running(monitor):
    updates = monitor._state.get_updates()
    # updates wait a second so PBS has committed the change; none is needed here
    updates = monitor._state.running_jobs | updates.running
    monitor._state.running_jobs = set()
    monitor._sync_jobs(job_updates=updates, job_type='running')


def test_a_subjobs_run_event_refreshes_its_array(world, monitor):
    world.cache.sync([array_job('7[]')])
    world.pbs.jobs['7[]'] = array_job('7[]', SocaJobState.SUBJOB_BEGUN)
    world.pbs.jobs['7[1]'] = array_job('7[1]', SocaJobState.RUNNING)

    monitor.job_running(run_event(1))
    sync_running(monitor)

    assert cached_ids(world.cache) == ['7[]']
    assert world.cache.get_job('7[]').state == SocaJobState.SUBJOB_BEGUN


def test_an_array_is_announced_as_started_once(world, monitor):
    world.cache.sync(
        [array_job('7[]', notifications={'started': True, 'completed': True})]
    )
    world.pbs.jobs['7[]'] = array_job('7[]', SocaJobState.SUBJOB_BEGUN)
    notifications = monitor.context.job_notifications

    monitor.job_running(run_event(1))
    monitor.job_running(run_event(2))
    assert notifications.job_started.call_count == 1
    started = notifications.job_started.call_args.kwargs['job']
    assert (started.job_id, started.owner, started.job_uid) == ('7[]', 'alice', UID)
    assert monitor.context.metrics.jobs_running.call_count == 1

    sync_running(monitor)
    monitor.job_running(run_event(3))
    assert notifications.job_started.call_count == 1


def test_subjob_hook_events_are_kept_as_the_arrays_one_run_on_the_host(world, monitor):
    def event(index, start=None, end=None):
        run = SocaJobExecutionRun(run_id='1', start=start, end=end, exit_code=index)
        return SocaJob(
            job_id=f'7[{index}]',
            execution_hosts=[
                SocaJobExecutionHost(
                    host='node-1',
                    instance_type='t3.medium',
                    execution=SocaJobExecution(runs=[run]),
                )
            ],
        )

    at = [START.shift(seconds=seconds).datetime for seconds in (0, 30, 40, 240)]
    monitor.job_status_update(event(1, start=at[0]))
    monitor.job_status_update(event(1, start=at[0], end=at[1]))
    monitor.job_status_update(event(2, start=at[2]))
    monitor.job_status_update(event(2, start=at[2], end=at[3]))

    assert world.cache.get_job_execution_hosts('7[1]') == []
    assert world.cache.get_job_execution_hosts('7[2]') == []
    [host] = world.cache.get_job_execution_hosts('7[]')
    [run] = host.execution.runs
    assert (run.start, run.end) == (at[0], at[3])
    assert run.exit_code is None, 'a subjob result comes from PBS history, not the hook'
    assert host.instance_type == 't3.medium'


# ------------------------------------------------------------------ the poll cycle


def finish_array(world, parent_exit_status=0, subjobs=None, submitted=2):
    world.pbs.active = []
    world.pbs.jobs['7[]'] = array_job(
        '7[]',
        SocaJobState.FINISHED,
        exit_status=parent_exit_status,
        start_time=START.datetime,
        end_time=START.shift(seconds=423).datetime,
        notifications={'completed': True},
    )
    if subjobs is None:
        subjobs = [subjob(2, start=30, seconds=200), subjob(1, exit_status=1)]
    world.pbs.subjobs['7[]'] = (submitted, subjobs)


def test_poll_cycles_of_an_array_with_two_subjobs_sharing_one_job_uid(world):
    """
    The array and two subjob rows, as a cache from an earlier build can hold them. While PBS
    lists the array nothing is finished: plain qselect leaves subjobs out, which is not their
    end. When the array is over it gets one record, one email, and its row goes.
    """
    world.cache.sync([array_job('7[]', licenses=None)])
    world.cache.add_active_licenses(
        [array_job('7[]', params=SocaJobParams(licenses=[{'name': 'x', 'count': 2}]))]
    )
    seed_row(world.cache, array_job('7[1]', SocaJobState.RUNNING))
    seed_row(world.cache, array_job('7[2]', SocaJobState.RUNNING))
    world.pbs.active = ['7[]']
    # qstat -x answers for a live subjob too
    world.pbs.jobs['7[1]'] = array_job(
        '7[1]', SocaJobState.RUNNING, start_time=START.datetime
    )

    poll(world)
    assert records(world) == []
    assert world.context.job_notifications.completed == []
    assert cached_ids(world.cache) == ['7[]']
    assert world.cache.get_active_license_count('x') == 2

    poll(world)
    assert records(world) == []

    finish_array(world)
    poll(world)
    [record] = records(world)
    assert (record.job_id, record.job_uid) == ('7[]', UID)
    assert record.state == SocaJobState.FINISHED
    assert record.exit_status == 1, 'PBS kept the last subjob status, 0, on the array'
    assert record.disposition == 'failed'
    assert [found.job_id for found in record.subjobs] == ['7[1]', '7[2]']
    assert record.total_time_secs == 423
    assert PricingSpy.calls == [('7[]', 423)]
    assert world.context.job_notifications.completed == ['7[]']
    assert world.context.lifecycle_events.dispositions == [('7[]', 'failed')]
    assert cached_ids(world.cache) == []
    assert world.cache.get_active_license_count('x') == 0

    poll(world)
    assert len(records(world)) == 1
    assert world.context.job_notifications.completed == ['7[]']


def test_a_leftover_subjob_row_brings_its_array_back_into_the_cache(world):
    seed_row(world.cache, array_job('7[1]', SocaJobState.RUNNING))
    world.pbs.active = ['7[]']
    world.pbs.jobs['7[]'] = array_job('7[]', SocaJobState.SUBJOB_BEGUN)
    poll(world)
    assert cached_ids(world.cache) == ['7[]']
    assert records(world) == []


def test_a_leftover_subjob_row_of_an_array_that_is_over_goes_with_no_record(world):
    seed_row(world.cache, array_job('7[1]', SocaJobState.RUNNING))
    world.pbs.jobs['7[1]'] = array_job('7[1]', SocaJobState.FINISHED)
    poll(world)
    assert cached_ids(world.cache) == []
    assert records(world) == []
    assert world.context.job_notifications.completed == []


@pytest.mark.parametrize(
    'state',
    [SocaJobState.RUNNING, SocaJobState.SUBJOB_BEGUN, SocaJobState.EXIT, None],
)
def test_a_job_pbs_still_has_live_is_never_recorded_as_finished(world, state):
    """absent from qselect and answered by qstat -x in a state that is not finished"""
    world.cache.sync([array_job('41', SocaJobState.RUNNING)])
    world.pbs.jobs['41'] = array_job('41', state, start_time=START.datetime)
    poll(world)
    assert records(world) == []
    assert world.context.job_notifications.completed == []
    assert cached_ids(world.cache) == ['41']


def test_a_job_cached_after_pbs_was_asked_is_not_taken_as_gone(world):
    """the hook thread caches a new job between the qselect and the read of the table"""
    world.pbs.on_qselect = lambda: world.cache.sync(
        [array_job('50', provisioned=False, job_uid='new-job')]
    )
    poll(world)
    assert records(world) == []
    assert cached_ids(world.cache) == ['50']


@pytest.mark.parametrize('done', [SocaJobState.FINISHED, SocaJobState.SUBJOB_EXPIRED])
def test_an_array_all_of_whose_subjobs_exited_0_succeeded(world, done):
    world.cache.sync([array_job('7[]', SocaJobState.SUBJOB_BEGUN)])
    finish_array(world, subjobs=[subjob(1), subjob(2, start=30, state=done)])
    poll(world)
    [record] = records(world)
    assert record.exit_status == 0
    assert record.disposition == 'ran'
    assert job_succeeded(record)


@pytest.mark.parametrize(
    'subjobs, submitted',
    [
        # one index has no history
        ([subjob(1)], 2),
        # PBS did not say how many there were
        ([subjob(1), subjob(2)], None),
        # one was deleted before it ran
        ([subjob(1), subjob(2, exit_status=None, terminated=True)], 2),
        # one is somehow not finished
        ([subjob(1), subjob(2, state=SocaJobState.RUNNING)], 2),
        ([], 2),
    ],
)
def test_an_array_is_not_a_success_unless_every_subjob_is_shown_to_be(
    world, subjobs, submitted
):
    world.cache.sync([array_job('7[]', SocaJobState.SUBJOB_BEGUN)])
    finish_array(world, parent_exit_status=0, subjobs=subjobs, submitted=submitted)
    poll(world)
    [record] = records(world)
    assert record.exit_status is None
    assert not job_succeeded(record)
    assert cached_ids(world.cache) == []


def test_an_array_is_still_recorded_when_its_subjob_history_cannot_be_read(world):
    """its row holds its licenses and its nodes; a record with no status releases both"""
    world.cache.sync([array_job('7[]', SocaJobState.SUBJOB_BEGUN)])
    finish_array(world)
    world.pbs.subjob_error = RuntimeError('qstat failed')
    poll(world)
    [record] = records(world)
    assert (record.exit_status, record.subjobs) == (None, None)
    assert not job_succeeded(record)
    assert world.context.job_notifications.completed == ['7[]']
    assert cached_ids(world.cache) == []


def test_an_array_gone_from_pbs_history_keeps_no_status_from_its_cached_row(world):
    """the cached row holds the last subjob status PBS had when it was read"""
    world.cache.sync(
        [
            array_job(
                '7[]',
                SocaJobState.SUBJOB_BEGUN,
                exit_status=0,
                start_time=START.datetime,
            )
        ]
    )
    poll(world)
    [record] = records(world)
    assert (record.exit_status, record.subjobs) == (None, None)
    assert not job_succeeded(record)


def test_an_array_deleted_after_losing_its_capacity_keeps_its_subjobs_outcomes(world):
    """reset to no stack after 357[1] ran, then deleted: the folder 357[1] has an outcome"""
    world.cache.sync(
        [
            array_job(
                '7[]',
                SocaJobState.SUBJOB_BEGUN,
                provisioned=False,
                start_time=START.datetime,
            )
        ]
    )
    world.pbs.subjobs['7[]'] = (2, [subjob(1, exit_status=271, terminated=True)])
    poll(world)
    [record] = records(world)
    assert [found.job_id for found in record.subjobs] == ['7[1]']
    assert record.exit_status == 271
    assert cached_ids(world.cache) == []


def test_a_record_filed_under_another_id_does_not_stand_in_for_this_job(world):
    """
    One job_uid, two ids: an earlier build could file a subjob's record under its array's
    uid. The array is then recorded under its own id, and its row leaves the cache.
    """
    world.cache.add_finished_job(array_job('7[1]', SocaJobState.FINISHED))
    world.cache.sync([array_job('7[]', SocaJobState.SUBJOB_BEGUN)])
    finish_array(world)

    poll(world)

    assert cached_ids(world.cache) == []
    [record] = records(world)
    assert record.job_id == '7[]'
    assert world.context.job_notifications.completed == ['7[]']


def test_two_cached_jobs_sharing_a_job_uid_both_leave_the_cache(world):
    """the id handed back for deletion is the one asked about, not the stored record's"""
    for job_id in ('70', '71'):
        world.pbs.jobs[job_id] = array_job(
            job_id,
            SocaJobState.FINISHED,
            exit_status=0,
            start_time=START.datetime,
            end_time=START.shift(seconds=60).datetime,
        )
    cached = [array_job('70', SocaJobState.RUNNING), array_job('71')]
    poller = build_poller(world.context)
    assert poller._process_finished_jobs(cached) == ['70', '71']
    assert poller._process_finished_jobs(cached) == ['70', '71']


def test_every_cached_job_with_a_record_on_file_is_dropped_by_its_own_id(world):
    """a restored cache: the index already holds the job, under the id asked about"""
    stored = array_job('60', SocaJobState.FINISHED, job_uid='restored')
    world.context.document_store.jobs_by_uid['restored'] = stored
    cached = array_job('60', SocaJobState.RUNNING, job_uid='restored')
    unprovisioned = array_job('61', job_uid='restored-2', provisioned=False)
    world.context.document_store.jobs_by_uid['restored-2'] = array_job(
        '61', SocaJobState.FINISHED, job_uid='restored-2'
    )
    poller = build_poller(world.context)
    assert poller._process_finished_jobs([cached]) == ['60']
    assert poller._process_unprovisioned_jobs([unprovisioned]) == ['61']
    assert world.context.job_notifications.completed == []


def test_an_arrays_emails_do_not_carry_its_subjob_list():
    """
    The email is one SQS message. 2,000 subjobs on the record are already more than a
    message may hold, and the send was refused: no completed email for a wide array.
    """
    client = Mock()
    config = Mock(get_string=lambda key, required=False: 'job-completed')
    job = array_job(
        '7[]',
        SocaJobState.FINISHED,
        notifications={'started': True, 'completed': True},
        subjobs=[
            SocaJobSubjob(
                job_id=f'7[{index}]',
                state=SocaJobState.FINISHED,
                exit_status=0,
                terminated=False,
                start_time=START.datetime,
                end_time=START.shift(seconds=90).datetime,
            )
            for index in range(1, 10001)
        ],
    )
    notifications = JobNotifications(
        context=Mock(config=lambda: config), notifications_client=client
    )

    notifications.job_completed(job)
    notifications.job_started(job)

    assert client.send_notification.call_count == 2
    for call in client.send_notification.call_args_list:
        sent = call.kwargs['notification'].params['job']
        assert 'subjobs' not in sent
        assert (sent['job_id'], sent['job_uid']) == ('7[]', UID)
        assert len(json.dumps(sent)) < 16 * 1024
    # the record itself keeps the list
    assert len(job.subjobs) == 10000


# ------------------------------------------------------------------ nodes


def array_instance(terminate_when_idle=0) -> EC2Instance:
    return EC2Instance(
        {
            'InstanceId': 'i-0123456789abcdef0',
            'PrivateDnsName': 'ip-10-0-0-1.ec2.internal',
            'Tags': [
                {
                    'Key': constants.IDEA_TAG_NODE_TYPE,
                    'Value': constants.NODE_TYPE_COMPUTE,
                },
                {'Key': constants.IDEA_TAG_JOB_ID, 'Value': '7-array'},
                {'Key': constants.IDEA_TAG_JOB_GROUP, 'Value': 'job-group-1'},
                {'Key': constants.IDEA_TAG_QUEUE_TYPE, 'Value': 'test'},
                {
                    'Key': constants.IDEA_TAG_TERMINATE_WHEN_IDLE,
                    'Value': str(terminate_when_idle),
                },
                {'Key': constants.IDEA_TAG_KEEP_FOREVER, 'Value': 'false'},
            ],
        }
    )


def idle_node() -> SocaComputeNode:
    return SocaComputeNode(
        host='ip-10-0-0-1',
        instance_id='i-0123456789abcdef0',
        queue_type='test',
        job_group='job-group-1',
        last_used_time=arrow.utcnow().shift(minutes=-20).datetime,
    )


def can_terminate(context) -> bool:
    session = NodeHouseKeepingSession(context=context, logger=LOGGER)
    return session._can_terminate(instance=array_instance(), node=idle_node())


def test_an_idle_node_of_a_begun_array_is_kept_between_subjobs(context, world):
    context.job_cache = world.cache
    world.cache.sync([array_job('7[]', SocaJobState.SUBJOB_BEGUN)])
    assert can_terminate(context) is False
    world.cache.delete_jobs(['7[]'])
    assert can_terminate(context) is True


def test_a_batch_node_waits_for_the_rest_of_a_begun_array(context, monkeypatch):
    asked = []

    def count(select):
        asked.append(select.get_queue_select_state_filter())
        return 1

    monkeypatch.setattr(OpenPBSQSelect, 'get_count', count)
    session = NodeHouseKeepingSession(context=context, logger=LOGGER)
    assert not session._can_terminate(
        instance=array_instance(terminate_when_idle=5), node=idle_node()
    )
    assert asked == ['QHB']


def test_a_scheduler_restart_in_the_middle_of_an_array(context, world, monitor):
    """
    At start every job is read from PBS, and the array reads begun. Its idle node stays, no
    second start is announced, the next subjob keeps the one row, and nothing is finished.
    """
    restarted = monitor.context
    restarted.queue_profiles.queue_profiles = [
        SimpleNamespace(enabled=True, queues=['test'], name='test')
    ]
    restarted.job_cache.get_job_provisioning_retry_count = lambda job_id: 0
    begun = array_job(
        '7[]',
        SocaJobState.SUBJOB_BEGUN,
        start_time=START.datetime,
        notifications={'started': True},
    )
    world.pbs.jobs['7[]'] = begun
    world.pbs.active = ['7[]']
    world.pbs.list_jobs = lambda queue=None, job_ids=None, **_: [begun]

    monitor._sync_all_jobs()
    assert world.cache.get_job('7[]').state == SocaJobState.SUBJOB_BEGUN

    context.job_cache = world.cache
    assert can_terminate(context) is False

    monitor.job_running(run_event(2))
    sync_running(monitor)
    restarted.job_notifications.job_started.assert_not_called()
    assert cached_ids(world.cache) == ['7[]']

    poll(world)
    assert records(world) == []
    assert cached_ids(world.cache) == ['7[]']


# ------------------------------------------------------------------ scratch folders


def scratch_records(jobs, forms):
    context = Mock()
    context.job_cache.list_completed_jobs.side_effect = lambda job_id: [
        job for job in jobs if job.job_id == job_id
    ]
    context.document_store.is_enabled.return_value = False
    found = ScratchJobState(context, Mock()).finished_records(forms)
    return {record.job_id: record for record in found}


def finished_array(**fields) -> SocaJob:
    values = dict(
        exit_status=1,
        disposition='failed',
        start_time=START.datetime,
        end_time=START.shift(seconds=423).datetime,
        subjobs=[subjob(1, exit_status=1), subjob(2, start=30, seconds=200)],
    )
    values.update(fields)
    return array_job('7[]', SocaJobState.FINISHED, **values)


def test_a_subjob_folder_is_judged_by_its_own_entry_on_the_arrays_record():
    found = scratch_records([finished_array()], {'7[1]', '7[]'})
    assert sorted(found) == ['7[1]', '7[]']
    assert found['7[1]'].finished and not found['7[1]'].succeeded
    assert found['7[1]'].job_uid == UID and found['7[1]'].owner == 'alice'
    assert found['7[1]'].end_time == START.shift(seconds=30).datetime

    found = scratch_records([finished_array()], {'7[2]', '7[]'})
    assert sorted(found) == ['7[2]', '7[]']
    assert found['7[2]'].succeeded
    assert found['7[2]'].start_time == START.shift(seconds=30).datetime
    assert found['7[2]'].end_time == START.shift(seconds=230).datetime
    assert not found['7[]'].succeeded


@pytest.mark.parametrize(
    'fields',
    [
        dict(exit_status=None),
        dict(terminated=True),
        dict(terminated=None),
        dict(state=SocaJobState.RUNNING),
    ],
)
def test_a_subjob_is_a_success_only_when_shown(fields):
    array = finished_array(subjobs=[subjob(1, **fields)])
    assert not scratch_records([array], {'7[1]', '7[]'})['7[1]'].succeeded


def test_a_subjob_with_no_entry_has_only_its_arrays_record():
    array = finished_array(subjobs=[subjob(2)])
    assert sorted(scratch_records([array], {'7[1]', '7[]'})) == ['7[]']


def test_an_array_record_with_no_subjob_outcomes_is_never_a_success():
    """
    what a release candidate wrote for 359[]: exit status 0, ran, after 359[1] exited 1 and
    359[2] exited 0. PBS's "and failed" comment does not read as stopped.
    """
    earlier = finished_array(
        exit_status=0,
        disposition='ran',
        terminated=False,
        subjobs=None,
        comment='Job Array Began at Mon Nov 09 at 22:12 and failed',
    )
    assert not job_succeeded(earlier)
    assert job_succeeded(
        finished_array(exit_status=0, disposition='ran', subjobs=[subjob(1), subjob(2)])
    )
    # a plain job is unchanged
    plain = finished_array(exit_status=0, disposition='ran', subjobs=None)
    plain.job_id = '7'
    assert job_succeeded(plain)
