"""
Test Cases for JobCache / JobsDB

exercises the sqlite-backed job cache against a temporary database file,
including adversarial identifiers (embedded quotes, injection-shaped values)
to prove all raw SQL statements use parameter binding.
"""

import glob
import logging
import sqlite3

import pytest

from ideadatamodel import (
    SocaJob,
    SocaJobParams,
    SocaJobLicenseAsk,
    SocaJobState,
)
from ideascheduler.app.provisioning.job_monitor.job_cache import JobCache


class FakeSchedulerContext:
    """
    minimal stand-in for ideascheduler.AppContext: JobsDB/JobCache only use
    logger() and get_scheduler_app_deploy_dir().
    """

    def __init__(self, deploy_dir: str):
        self._deploy_dir = deploy_dir

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
