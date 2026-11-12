"""
One job that cannot be read must not hold up the others. Seen on a cluster: a queue profile
deleted in the same second as its job's qdel. Every listing that named the job failed with
SCHEDULER_QUEUE_PROFILE_NOT_FOUND, so no job that had capacity was recorded as finished and
none left the job cache until the profile was made again.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ideadatamodel import SocaJobState, errorcodes, exceptions
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSJob
from ideascheduler.app.scheduler.openpbs.openpbs_qstat import OpenPBSQStat

import test_array_jobs
from test_array_jobs import array_job, cached_ids, poll, records

LOGGER = logging.getLogger('test_job_without_queue_profile')
# the job cache, PBS and poll cycle the array tests run against
world = test_array_jobs.world


def queue_profiles(known=('test',)):
    def get_queue_profile(queue_name=None, **_):
        if queue_name not in known:
            raise exceptions.soca_exception(
                error_code=errorcodes.SCHEDULER_QUEUE_PROFILE_NOT_FOUND,
                message=f'queue profile not found for queue: {queue_name}',
            )
        return SimpleNamespace(name=queue_name)

    return SimpleNamespace(get_queue_profile=get_queue_profile)


def qstat(jobs, context):
    stdout = json.dumps({'Jobs': jobs})
    shell = SimpleNamespace(
        invoke=lambda cmd, **_: SimpleNamespace(returncode=0, stdout=stdout, stderr='')
    )
    return OpenPBSQStat(
        context=context,
        logger=LOGGER,
        shell=shell,
        converter=None,
        job_ids=[job_id.split('.')[0] for job_id in jobs],
        job_state=SocaJobState.FINISHED,
    )


def test_a_listing_leaves_out_a_job_whose_queue_has_no_profile():
    jobs = {
        '393.scheduler': {'job_state': 'F', 'queue': 'iv-s4orphan0001'},
        '394.scheduler': {'job_state': 'F', 'queue': 'test'},
    }
    context = Mock(queue_profiles=queue_profiles())
    with patch.object(OpenPBSJob, 'as_soca_job', lambda self, **_: self.id):
        assert qstat(jobs, context).list_jobs() == ['394.scheduler']
        assert qstat(jobs, context).get_job() == '394.scheduler'


def test_a_queue_profile_that_cannot_be_looked_up_still_fails_the_listing():
    """only a profile that is gone is skipped: a failed lookup is not an answer"""
    context = Mock()
    context.queue_profiles.get_queue_profile.side_effect = exceptions.soca_exception(
        error_code=errorcodes.GENERAL_ERROR, message='table not reachable'
    )
    jobs = {'394.scheduler': {'job_state': 'F', 'queue': 'test'}}
    try:
        qstat(jobs, context).list_jobs()
    except exceptions.SocaException as e:
        assert e.error_code == errorcodes.GENERAL_ERROR
    else:
        raise AssertionError('the listing hid a failed lookup')


def test_a_job_that_fails_the_listing_does_not_stop_the_rest(world):
    """whatever makes one job unreadable, the rest are read one by one and recorded"""
    world.cache.sync(
        [
            array_job('41', SocaJobState.RUNNING, job_uid='bad'),
            array_job('42', SocaJobState.RUNNING, job_uid='good'),
            array_job('43', provisioned=False, job_uid='never-ran'),
        ]
    )
    for job_id, uid in (('41', 'bad'), ('42', 'good')):
        world.pbs.jobs[job_id] = array_job(job_id, SocaJobState.FINISHED, job_uid=uid)
    read = world.pbs.list_jobs

    def list_jobs(job_ids=None, **kwargs):
        if '41' in (job_ids or []):
            raise RuntimeError('job 41 cannot be read')
        return read(job_ids=job_ids, **kwargs)

    world.pbs.list_jobs = list_jobs
    poll(world)
    assert sorted(record.job_id for record in records(world)) == ['42', '43']
    assert cached_ids(world.cache) == ['41']
    # readable again: the one that waited is recorded, nothing twice
    world.pbs.list_jobs = read
    poll(world)
    assert sorted(record.job_id for record in records(world)) == ['41', '42', '43']
    assert cached_ids(world.cache) == []
