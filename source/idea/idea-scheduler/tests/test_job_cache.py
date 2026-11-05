"""
Test Cases for JobCache / JobsDB

exercises the sqlite-backed job cache against a temporary database file,
including adversarial identifiers (embedded quotes, injection-shaped values)
to prove all raw SQL statements use parameter binding.
"""

import glob
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import arrow
import pytest
from ideasdk.config.soca_config import SocaConfig

from ideadatamodel import (
    SocaJob,
    SocaJobParams,
    SocaJobLicenseAsk,
    SocaJobState,
    SocaJobExecution,
    SocaJobExecutionHost,
    SocaJobExecutionRun,
)
from ideascheduler.app.provisioning.job_monitor.job_cache import JobCache


class FakeSchedulerContext:
    """
    minimal stand-in for ideascheduler.AppContext: JobsDB/JobCache only use
    logger(), config(), and get_scheduler_app_deploy_dir().
    """

    def __init__(
        self, deploy_dir: str, ecs_enabled=False, db_dir=None, backup_interval=None
    ):
        self._deploy_dir = deploy_dir
        self._config = SocaConfig(
            {
                'ecs.enabled': ecs_enabled,
                'scheduler.job_cache.db_dir': db_dir,
                'scheduler.job_cache.backup_interval_seconds': backup_interval,
            }
        )

    def config(self):
        return self._config

    def logger(self, name: str = None):
        return logging.getLogger(name or 'test-job-cache')

    def get_scheduler_app_deploy_dir(self) -> str:
        return self._deploy_dir


@pytest.fixture()
def job_cache(tmp_path) -> JobCache:
    return JobCache(context=FakeSchedulerContext(deploy_dir=str(tmp_path)))


def build_job(
    job_id: str,
    job_group: str = 'group-1',
    queue_profile: str = 'compute',
    state: SocaJobState = SocaJobState.QUEUED,
    provisioned: bool = False,
    nodes: int = 1,
    cpus: int = 1,
    licenses=None,
    job_uid: str = None,
    name: str = None,
    total_time_secs: int = None,
) -> SocaJob:
    return SocaJob(
        job_id=job_id,
        job_uid=f'uid-{job_id}' if job_uid is None else job_uid,
        name=name,
        job_group=job_group,
        queue='normal',
        queue_type=queue_profile,
        owner='testuser',
        state=state,
        provisioned=provisioned,
        total_time_secs=total_time_secs,
        params=SocaJobParams(nodes=nodes, cpus=cpus, licenses=licenses),
    )


def test_host_job_cache_keeps_deploy_directory_default(tmp_path, monkeypatch):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    cache = JobCache(context=FakeSchedulerContext(str(tmp_path)))
    assert (
        Path(cache.get_connection().engine.url.database)
        == tmp_path / 'db/job-cache-v3.db'
    )
    assert (
        list(cache.get_connection().query('PRAGMA journal_mode'))[0]['journal_mode']
        == 'wal'
    )


def test_container_job_cache_lives_on_local_disk_and_is_copied_to_pbs_home(
    tmp_path, monkeypatch
):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    cache = JobCache(context=FakeSchedulerContext(str(tmp_path), ecs_enabled=True))
    db = cache._jobs_db
    assert Path(db._db_file) == tmp_path / 'db/job-cache-v3.db'
    assert Path(db._backup_file) == tmp_path / 'pbs/idea/db/job-cache-v3.db'
    assert db._backup_interval == 300
    assert db._backup_thread.is_alive()
    assert (
        list(cache.get_connection().query('PRAGMA journal_mode'))[0]['journal_mode']
        == 'wal'
    )
    cache.stop()
    assert not db._backup_thread.is_alive()


def test_an_operator_set_backup_interval_is_kept(tmp_path, monkeypatch):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    cache = JobCache(
        context=FakeSchedulerContext(
            str(tmp_path), ecs_enabled=True, backup_interval=45
        )
    )
    assert cache._jobs_db._backup_interval == 45
    cache.stop()


def test_stop_flushes_a_final_copy_without_waiting_out_the_default_interval(
    tmp_path, monkeypatch
):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    cache = JobCache(context=FakeSchedulerContext(str(tmp_path), ecs_enabled=True))
    copy = Path(cache._jobs_db._backup_file)
    cache.add_finished_job(
        build_job('41', job_uid='finished', state=SocaJobState.FINISHED)
    )
    assert not copy.exists()  # the first interval has not passed
    started = time.monotonic()
    cache.stop()
    assert time.monotonic() - started < 5
    with sqlite3.connect(copy) as connection:
        assert (
            connection.execute(
                'SELECT count(*) FROM finished_jobs WHERE job_uid = ?', ('finished',)
            ).fetchone()[0]
            == 1
        )


def test_host_job_cache_has_no_backup(tmp_path):
    cache = JobCache(context=FakeSchedulerContext(str(tmp_path)))
    assert cache._jobs_db._backup_file is None
    assert cache._jobs_db._backup_thread is None
    assert cache._jobs_db.backup(force=True) is False
    cache.stop()


def test_backup_copies_only_when_the_cache_changed(tmp_path, monkeypatch):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    cache = JobCache(
        context=FakeSchedulerContext(
            str(tmp_path), ecs_enabled=True, backup_interval=3600
        )
    )
    db = cache._jobs_db
    copy = Path(db._backup_file)
    assert db.backup() is True  # the first copy, whatever the state
    assert copy.exists()
    assert db.backup() is False  # nothing changed
    cache.add_finished_job(
        build_job('41', job_uid='finished', state=SocaJobState.FINISHED)
    )
    assert db.backup() is True
    assert db.backup() is False
    with sqlite3.connect(copy) as connection:
        assert (
            connection.execute(
                'SELECT count(*) FROM finished_jobs WHERE job_uid = ?', ('finished',)
            ).fetchone()[0]
            == 1
        )
    assert [p.name for p in copy.parent.iterdir()] == ['job-cache-v3.db']
    cache.stop()


def test_backup_runs_on_the_interval_and_on_stop(tmp_path, monkeypatch):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    cache = JobCache(
        context=FakeSchedulerContext(
            str(tmp_path), ecs_enabled=True, backup_interval=0.05
        )
    )
    copy = Path(cache._jobs_db._backup_file)
    deadline = time.monotonic() + 5
    while not copy.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert copy.exists(), 'the backup thread did not copy the cache'
    cache.add_finished_job(
        build_job('41', job_uid='finished', state=SocaJobState.FINISHED)
    )
    cache.stop()
    with sqlite3.connect(copy) as connection:
        assert (
            connection.execute(
                'SELECT count(*) FROM finished_jobs WHERE job_uid = ?', ('finished',)
            ).fetchone()[0]
            == 1
        )


def test_container_job_cache_survives_replacement(tmp_path, monkeypatch):
    pbs_home = tmp_path / 'persistent-pbs'
    monkeypatch.setenv('PBS_HOME', str(pbs_home))
    cache = JobCache(
        context=FakeSchedulerContext(str(tmp_path / 'task-1'), ecs_enabled=True)
    )
    cache.add_finished_job(
        build_job('41', job_uid='finished', state=SocaJobState.FINISHED)
    )
    cache.sync([build_job('42')])
    cache.get_connection()['execution_hosts'].insert(
        {'job_id': '42', 'instance_id': 'instance-42'}
    )
    # committed records are still in the live file's WAL when the task stops
    assert Path(f'{cache._jobs_db._db_file}-wal').stat().st_size > 0
    cache.stop()

    replacement = JobCache(
        context=FakeSchedulerContext(str(tmp_path / 'task-2'), ecs_enabled=True)
    )
    assert (
        Path(replacement.get_connection().engine.url.database)
        == tmp_path / 'task-2/db/job-cache-v3.db'
    )
    assert replacement.get_completed_job_by_uid('finished') is not None
    assert [job.job_uid for job in replacement.list_pending_index_jobs()] == [
        'finished'
    ]
    assert replacement.get_job('42') is not None
    assert (
        replacement.get_connection()['execution_hosts'].find_one(job_id='42')[
            'instance_id'
        ]
        == 'instance-42'
    )
    assert list(replacement.get_connection().query('PRAGMA integrity_check')) == [
        {'integrity_check': 'ok'}
    ]
    assert (
        list(replacement.get_connection().query('PRAGMA busy_timeout'))[0]['timeout']
        == 30000
    )
    replacement.stop()


def test_an_existing_local_cache_is_not_replaced_by_the_copy(tmp_path, monkeypatch):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    first = JobCache(context=FakeSchedulerContext(str(tmp_path), ecs_enabled=True))
    first.add_finished_job(build_job('41', job_uid='copied'))
    first.stop()
    local = JobCache(context=FakeSchedulerContext(str(tmp_path), ecs_enabled=True))
    local.add_finished_job(build_job('42', job_uid='local'))
    local.get_connection().close()
    local._jobs_db._stop.set()

    reopened = JobCache(context=FakeSchedulerContext(str(tmp_path), ecs_enabled=True))
    assert reopened.get_completed_job_by_uid('local') is not None
    assert reopened.get_completed_job_by_uid('copied') is not None
    reopened.stop()


def test_an_unreadable_copy_starts_an_empty_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    copy = tmp_path / 'pbs/idea/db/job-cache-v3.db'
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b'not a sqlite database')
    cache = JobCache(context=FakeSchedulerContext(str(tmp_path), ecs_enabled=True))
    assert cache.get_completed_job_by_uid('anything') is None
    assert copy.read_bytes() == b'not a sqlite database'
    cache.add_finished_job(build_job('41', job_uid='new'))
    cache.stop()
    with sqlite3.connect(copy) as connection:
        assert (
            connection.execute('SELECT count(*) FROM finished_jobs').fetchone()[0] == 1
        )


@pytest.mark.parametrize('ecs_enabled', [False, True])
def test_job_cache_directory_can_be_configured(tmp_path, ecs_enabled):
    db_dir = tmp_path / 'configured'
    cache = JobCache(
        context=FakeSchedulerContext(str(tmp_path), ecs_enabled, str(db_dir))
    )
    assert (
        Path(cache.get_connection().engine.url.database) == db_dir / 'job-cache-v3.db'
    )


def test_job_cache_round_trip_job_id_with_quote(job_cache):
    """
    a job id containing a single quote must be stored and retrieved as data
    """
    job_id = "1'; drop table jobs--.scheduler"
    job_cache.sync([build_job(job_id=job_id)])

    found = job_cache.get_job(job_id=job_id)
    assert found is not None
    assert found.job_id == job_id


def test_get_desired_capacity_binds_job_group(job_cache):
    """
    job_group containing a quote must be bound, not interpolated: interpolated, the
    dangling quote raises sqlite3.OperationalError
    """
    job_group = "g'roup--1"
    job_cache.sync(
        [
            build_job(job_id='1.host', job_group=job_group, nodes=2, cpus=3),
            build_job(job_id='2.host', job_group=job_group, nodes=1, cpus=4),
            build_job(job_id='3.host', job_group='other-group', nodes=5, cpus=5),
        ]
    )

    assert job_cache.get_desired_capacity(job_group=job_group) == 10
    # injection-shaped input is treated as data and matches nothing
    assert job_cache.get_desired_capacity(job_group="x' or '1'='1") == 0


def test_get_active_jobs_binds_queue_profile(job_cache):
    queue_profile = "cpu'profile"
    job_cache.sync(
        [
            build_job(job_id='4.host', queue_profile=queue_profile, provisioned=True),
            build_job(job_id='5.host', queue_profile=queue_profile, provisioned=False),
            build_job(job_id='6.host', queue_profile='other', provisioned=True),
        ]
    )

    assert job_cache.get_active_jobs(queue_profile=queue_profile) == 1
    assert job_cache.get_active_jobs(queue_profile="x' or '1'='1") == 0


def test_finished_jobs_with_a_reused_job_id_are_both_kept(job_cache):
    """
    the scheduler restarts job ids from zero when its host is replaced, so two different
    jobs can be recorded under the same id. keyed on job_id alone, the later one would
    overwrite the earlier job's record - its name, its runtime and the costs derived from it.
    """
    job_cache.add_finished_job(
        build_job(
            job_id='41',
            job_uid='uid-first',
            name='first-job',
            total_time_secs=60,
            state=SocaJobState.FINISHED,
        )
    )
    job_cache.add_finished_job(
        build_job(
            job_id='41',
            job_uid='uid-second',
            name='second-job',
            total_time_secs=7200,
            state=SocaJobState.FINISHED,
        )
    )

    recorded = job_cache.list_completed_jobs(owner='testuser')
    assert {job.job_uid for job in recorded} == {'uid-first', 'uid-second'}

    # each record keeps its own name and its own priced runtime
    first = job_cache.get_completed_job_by_uid('uid-first')
    assert (first.name, first.total_time_secs) == ('first-job', 60)
    second = job_cache.get_completed_job_by_uid('uid-second')
    assert (second.name, second.total_time_secs) == ('second-job', 7200)

    # the id alone cannot name one of them: the most recent record answers for it
    assert job_cache.get_completed_job('41').job_uid == 'uid-second'


def test_finished_job_with_no_uid_keeps_the_job_id_key(job_cache):
    """
    a job carrying no uid has no other identity, so it stays keyed on job_id: two such
    jobs are separate records and re-recording one updates it in place.
    """
    job_cache.add_finished_job(build_job(job_id='7', job_uid='', name='job-7'))
    job_cache.add_finished_job(build_job(job_id='8', job_uid='', name='job-8'))
    job_cache.add_finished_job(build_job(job_id='8', job_uid='', name='job-8-again'))

    recorded = job_cache.list_completed_jobs(owner='testuser')
    assert len(recorded) == 2
    assert job_cache.get_completed_job('8').name == 'job-8-again'


def test_unique_job_id_index_from_an_earlier_schema_is_dropped(tmp_path):
    """
    a database written by an earlier release carries a unique index on
    finished_jobs.job_id, which rejects the second job to be given a reused id.
    """
    cache = JobCache(context=FakeSchedulerContext(deploy_dir=str(tmp_path)))
    # the row creates the columns the index needs
    cache.add_finished_job(build_job(job_id='41', job_uid='uid-first'))

    db_file = glob.glob(f'{tmp_path}/db/*.db')[0]
    connection = sqlite3.connect(db_file)
    try:
        connection.execute(
            'CREATE UNIQUE INDEX ix_finished_jobs_job_id ON finished_jobs (job_id)'
        )
        connection.commit()
    finally:
        connection.close()

    # opening the db again runs the index migration
    reopened = JobCache(context=FakeSchedulerContext(deploy_dir=str(tmp_path)))
    reopened.add_finished_job(build_job(job_id='41', job_uid='uid-second'))

    recorded = reopened.list_completed_jobs(owner='testuser')
    assert {job.job_uid for job in recorded} == {'uid-first', 'uid-second'}


def test_get_active_license_count_binds_license_name(job_cache):
    license_name = "app'_license"
    job_cache.add_active_licenses(
        [
            build_job(
                job_id='7.host',
                licenses=[SocaJobLicenseAsk(name=license_name, count=4)],
            ),
            build_job(
                job_id='8.host',
                licenses=[SocaJobLicenseAsk(name=license_name, count=3)],
            ),
            build_job(
                job_id='9.host',
                licenses=[SocaJobLicenseAsk(name='other_license', count=7)],
            ),
        ]
    )

    assert job_cache.get_active_license_count(license_name=license_name) == 7
    assert job_cache.get_active_license_count(license_name="x' or '1'='1") == 0


def test_admin_deletion_reason_survives_cached_scheduler_refresh(job_cache):
    from ideascheduler.app.provisioning.lifecycle_events import ADMINISTRATOR_DELETION
    from ideascheduler.app.api.job_waiting_signals import build_status_reason

    job = build_job('42')
    job_cache.record_deleted_job(
        job, ADMINISTRATOR_DELETION, 'Deleted by an administrator.'
    )
    job_cache.sync([build_job('42')])
    stored = job_cache.get_job('42')
    assert stored.disposition == 'deleted'
    assert stored.reason_class == 'administrator'
    assert stored.status_reason == 'Deleted by an administrator.'
    assert build_status_reason(stored) == 'Deleted by an administrator.'


def test_existing_completed_records_count_as_indexed_and_new_ones_wait(tmp_path):
    """
    a database from an earlier release has no index marker. its records were indexed as
    they were recorded, so the migration must not queue the whole history for a replay;
    only records written after the migration wait for the index.
    """
    from ideasdk.utils import Utils

    db_dir = tmp_path / 'db'
    db_dir.mkdir()
    old = build_job('42')
    with sqlite3.connect(db_dir / 'job-cache-v3.db') as connection:
        connection.execute(
            'CREATE TABLE finished_jobs (id INTEGER PRIMARY KEY, job_uid TEXT, job_data TEXT)'
        )
        connection.execute(
            'INSERT INTO finished_jobs (job_uid, job_data) VALUES (?, ?)',
            (old.job_uid, Utils.to_json(old)),
        )
    context = FakeSchedulerContext(str(tmp_path))
    cache = JobCache(context)
    assert cache.list_pending_index_jobs() == []
    new = build_job('43')
    cache.add_finished_job(new)
    assert [j.job_uid for j in cache.list_pending_index_jobs()] == [new.job_uid]
    cache.mark_job_indexed(new)
    cache.get_connection().close()
    reopened = JobCache(context)
    assert reopened.list_pending_index_jobs() == []
    assert reopened.get_completed_job_by_uid(old.job_uid).job_id == old.job_id


def test_pending_index_listing_is_bounded_and_oldest_first(job_cache):
    for number in range(1, 8):
        job_cache.add_finished_job(build_job(str(number), job_uid=f'uid-{number}'))
    assert [j.job_uid for j in job_cache.list_pending_index_jobs(limit=3)] == [
        'uid-1',
        'uid-2',
        'uid-3',
    ]


@pytest.mark.parametrize('job_uid', [None, '', 'uid-first'])
@pytest.mark.parametrize('mark_first', [False, True])
def test_index_marker_tracks_completed_record_identity(job_cache, job_uid, mark_first):
    first = build_job('42')
    first.job_uid = job_uid
    second = build_job('42', job_uid='uid-second')
    job_cache.add_finished_job(first)
    job_cache.add_finished_job(second)
    job_cache.mark_job_indexed(first if mark_first else second)
    assert [j.job_uid for j in job_cache.list_pending_index_jobs()] == [
        second.job_uid if mark_first else first.job_uid
    ]


def test_a_new_job_cache_reports_ready_only_after_set_ready(tmp_path):
    # every PBS hook call asks is_ready(); a JobCache without _is_ready fails them all
    cache = JobCache(context=FakeSchedulerContext(str(tmp_path)))
    assert cache.is_ready() is False
    cache.set_ready()
    assert cache.is_ready() is True
    cache.stop()


def test_execution_end_does_not_borrow_start_from_another_run(job_cache):
    from datetime import datetime, timezone

    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for run in [
        SocaJobExecutionRun(run_id='1', start=start),
        SocaJobExecutionRun(run_id='2', end=start),
    ]:
        job_cache.log_job_execution(
            '42',
            SocaJobExecutionHost(
                host='compute', execution=SocaJobExecution(runs=[run])
            ),
        )
    runs = job_cache.get_job_execution_hosts('42')[0].execution.runs
    assert [run.run_id for run in runs] == ['1', '2']
    saved = runs[1]
    assert saved.run_id == '2'
    assert saved.start is None


@pytest.mark.parametrize('reverse_hooks', [False, True])
def test_same_host_requeue_preserves_real_pbs_run_history(job_cache, reverse_hooks):
    from ideascheduler.app.scheduler.openpbs.openpbs_model import (
        OpenPBSEvent,
        OpenPBSJob,
    )

    for count in (1, 2):
        hooks = [
            ('execjob_begin', 1785542400000 + count * 100000),
            ('execjob_end', 1785542400000 + count * 100000 + 10000),
        ]
        if reverse_hooks:
            hooks.reverse()
        for event_type, timestamp in hooks:
            event = OpenPBSEvent(
                type=event_type, timestamp=timestamp, node_name='compute'
            )
            pbs_job = OpenPBSJob(
                exec_host='compute/0', run_count=count, resources_used='cput=00:00:05'
            )
            host = pbs_job.get_soca_execution_hosts(event)[0]
            job_cache.log_job_execution('42', host)
            assert (
                len(job_cache.get_job_execution_hosts('42')[0].execution.runs) == count
            )
        runs = job_cache.get_job_execution_hosts('42')[0].execution.runs
        assert [run.run_id for run in runs] == [str(i) for i in range(1, count + 1)]
        for run in runs:
            assert (run.end - run.start).total_seconds() == 10
            assert run.resources_used.cpu_time_secs == 5


def add_finished_job_aged(cache, job_id: str, days: int, padding: str = None):
    job = build_job(
        job_id, job_uid=f'uid-{job_id}', state=SocaJobState.FINISHED, name=padding
    )
    job.end_time = arrow.utcnow().shift(days=-days).datetime
    cache.add_finished_job(job)


def finished_job_ids(cache):
    return {job.job_id for job in cache.list_completed_jobs()}


def test_prune_deletes_only_finished_jobs_past_the_retention(job_cache):
    add_finished_job_aged(job_cache, 'old', 401)
    add_finished_job_aged(job_cache, 'kept', 399)
    job_cache.sync([build_job('active')])

    assert job_cache._jobs_db.prune_finished_jobs() == 1

    assert finished_job_ids(job_cache) == {'kept'}
    assert job_cache.get_completed_jobs_count() == 1
    assert job_cache.get_job('active') is not None
    # what get_completed_job turns into JOB_NOT_FOUND
    assert job_cache.get_completed_job('old') is None
    assert job_cache.get_completed_job_by_uid('uid-old') is None


def test_prune_falls_back_to_queue_time_and_keeps_a_job_with_no_time(job_cache):
    never_ran = build_job('never-ran', state=SocaJobState.FINISHED)
    never_ran.queue_time = arrow.utcnow().shift(days=-500).datetime
    job_cache.add_finished_job(never_ran)
    job_cache.add_finished_job(build_job('undated', state=SocaJobState.FINISHED))
    add_finished_job_aged(job_cache, 'old', 500)

    assert job_cache._jobs_db.prune_finished_jobs() == 2

    assert finished_job_ids(job_cache) == {'undated'}


def test_prune_retention_is_configurable_and_zero_keeps_everything(tmp_path):
    context = FakeSchedulerContext(deploy_dir=str(tmp_path))
    cache = JobCache(context=context)
    add_finished_job_aged(cache, 'a', 40)
    add_finished_job_aged(cache, 'b', 20)

    context.config().put('scheduler.job_cache.finished_job_retention_days', 0)
    assert cache._jobs_db.prune_finished_jobs() == 0
    assert finished_job_ids(cache) == {'a', 'b'}

    # read on each pass: the setting is a runtime one
    context.config().put('scheduler.job_cache.finished_job_retention_days', 30)
    assert cache._jobs_db.prune_finished_jobs() == 1
    assert finished_job_ids(cache) == {'b'}


def test_a_large_backlog_is_pruned_over_several_bounded_passes(job_cache, monkeypatch):
    db = job_cache._jobs_db
    monkeypatch.setattr(db, 'PRUNE_BATCH_SIZE', 2)
    monkeypatch.setattr(db, 'PRUNE_MAX_BATCHES', 2)
    monkeypatch.setattr(db, 'PRUNE_BATCH_PAUSE_SECONDS', 0)
    for index in range(5):
        add_finished_job_aged(job_cache, f'old-{index}', 500)
    add_finished_job_aged(job_cache, 'kept', 1)

    assert [db.prune_finished_jobs() for _ in range(4)] == [4, 1, 0, 0]

    assert finished_job_ids(job_cache) == {'kept'}


def test_prune_does_not_hold_the_database_lock_between_batches(job_cache, monkeypatch):
    db = job_cache._jobs_db
    monkeypatch.setattr(db, 'PRUNE_BATCH_SIZE', 1)
    monkeypatch.setattr(db, 'PRUNE_BATCH_PAUSE_SECONDS', 0)
    add_finished_job_aged(job_cache, 'first', 500)
    add_finished_job_aged(job_cache, 'second', 500)
    lock_free_between_batches = []
    real_wait = db._stop.wait

    def wait(timeout=None):
        acquired = []

        def try_lock():
            acquired.append(db._db_lock.acquire(timeout=5))
            db._db_lock.release()

        thread = threading.Thread(target=try_lock)
        thread.start()
        thread.join()
        lock_free_between_batches.append(acquired == [True])
        return real_wait(0)

    monkeypatch.setattr(db._stop, 'wait', wait)

    assert db.prune_finished_jobs() == 2
    assert lock_free_between_batches == [True, True]


def test_a_pass_with_nothing_to_prune_does_not_trigger_a_backup(tmp_path, monkeypatch):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    cache = JobCache(context=FakeSchedulerContext(str(tmp_path), ecs_enabled=True))
    add_finished_job_aged(cache, 'kept', 1)
    db = cache._jobs_db
    assert db.backup() is True

    assert db.prune_finished_jobs() == 0

    assert db.backup() is False
    cache.stop()


def test_the_copy_shrinks_after_a_prune_and_restores_without_the_pruned_jobs(
    tmp_path, monkeypatch
):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    cache = JobCache(
        context=FakeSchedulerContext(str(tmp_path / 'task-1'), ecs_enabled=True)
    )
    db = cache._jobs_db
    for index in range(200):
        add_finished_job_aged(cache, f'old-{index}', 500, padding='x' * 2000)
    add_finished_job_aged(cache, 'kept', 1)
    assert db.backup() is True
    before = Path(db._backup_file).stat().st_size

    assert db.prune_finished_jobs() == 200
    assert db.backup() is True

    assert Path(db._backup_file).stat().st_size < before / 4
    cache.stop()
    with closing(sqlite3.connect(db._backup_file)) as copy:
        assert copy.execute('PRAGMA freelist_count').fetchone()[0] == 0
        assert copy.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'

    replacement = JobCache(
        context=FakeSchedulerContext(str(tmp_path / 'task-2'), ecs_enabled=True)
    )
    assert finished_job_ids(replacement) == {'kept'}
    replacement.stop()


def test_backup_does_not_block_a_writer_or_lose_its_commit(tmp_path, monkeypatch):
    monkeypatch.setenv('PBS_HOME', str(tmp_path / 'pbs'))
    cache = JobCache(context=FakeSchedulerContext(str(tmp_path), ecs_enabled=True))
    db = cache._jobs_db
    for index in range(100):
        add_finished_job_aged(cache, f'job-{index}', 1, padding='x' * 2000)
    writes = []

    def writer():
        for index in range(50):
            add_finished_job_aged(cache, f'new-{index}', 1)
            writes.append(index)

    thread = threading.Thread(target=writer)
    thread.start()
    assert db.backup(force=True) is True
    thread.join()
    assert len(writes) == 50
    assert db.backup() is True
    cache.stop()

    with closing(sqlite3.connect(db._backup_file)) as copy:
        assert copy.execute('SELECT count(*) FROM finished_jobs').fetchone()[0] == 150
