"""
U03: job_uid and job_group are checked at submission, but a job queued before that check
(an earlier release) carries values never checked. read back from PBS they name the job
directory and the bootstrap package rendered into the node's root bootstrap, so they are
checked again before any of that is built.
"""

from typing import List
from unittest.mock import Mock

import pytest

from ideadatamodel import HpcQueueProfile, SocaJob, SocaJobState, exceptions
from ideascheduler.app.provisioning.job_provisioner.job_provisioner import (
    ProvisionJobs,
    ProvisionJobsResult,
)
from ideascheduler.app.scheduler.openpbs import OpenPBSJob
from test_held_job_provisioning import make_provisioner
from test_job_param_builder import JOB_UID_ACCEPTED, JOB_UID_REFUSED


def read_back(context, job_id: str, **resources) -> SocaJob:
    """the job as a read of PBS returns it: the conversion qstat's output takes"""
    return OpenPBSJob(
        id=f'{job_id}.scheduler',
        Job_Owner='mockuser@scheduler',
        queue='normal',
        job_state='Q',
        qtime='1785542400',
        Resource_List={
            'select': '1:ncpus=1:compute_node=tbd',
            'nodect': 1,
            'ncpus': 1,
            'instance_type': 't3.micro',
            'stack_id': 'tbd',
            **resources,
        },
    ).as_soca_job(context=context, queue_profile=HpcQueueProfile(name='compute'))


class Scheduler:
    def __init__(self):
        self.held: List[str] = []

    def hold_job(self, job_id: str):
        self.held.append(job_id)


@pytest.fixture()
def provisioner(context, monkeypatch):
    provisioned: List[List[str]] = []

    def invoke(_self) -> ProvisionJobsResult:
        provisioned.append([job.job_id for job in _self.jobs])
        return ProvisionJobsResult(status=True)

    monkeypatch.setattr(ProvisionJobs, 'invoke', invoke)
    scheduler = Scheduler()
    monkeypatch.setattr(context, 'scheduler', scheduler)
    errors = {}
    monkeypatch.setattr(
        context,
        'job_cache',
        Mock(
            set_job_provisioning_error=lambda job_id, error_code, message: (
                errors.update({job_id: message})
            )
        ),
    )
    provisioner = make_provisioner(context)
    # every job is live and queued: what the hold must stop is not a state question
    monkeypatch.setattr(provisioner, '_is_job_provisionable', lambda job: True)
    monkeypatch.setattr(context, 'lifecycle_events', None, raising=False)
    monkeypatch.setattr(provisioner, '_set_provisioning_comment', lambda **_: None)
    return provisioner, scheduler, errors, provisioned


@pytest.mark.parametrize(
    'resources,field',
    [
        ({'job_uid': 'a$(id)'}, 'job_uid'),
        ({'job_uid': 'goodUid1', 'job_group': 'custom-x;reboot'}, 'job_group'),
    ],
)
def test_a_job_queued_before_the_check_is_held_and_the_rest_provisioned(
    context, provisioner, resources, field
):
    provisioner, scheduler, errors, provisioned = provisioner
    bad = read_back(context, '101', **resources)
    good = read_back(context, '102', job_uid='goodUid2')

    result = provisioner._provision_with_retry_backoff(jobs=[bad, good])

    assert result.status is True
    assert provisioned == [['102']]
    assert scheduler.held == ['101']
    assert bad.state == SocaJobState.HELD
    assert errors['101'].startswith(f'Job held: {field}')
    assert ',' not in errors['101'], 'qalter splits on commas'


def test_a_batch_whose_only_job_fails_the_check_provisions_nothing(
    context, provisioner
):
    provisioner, scheduler, _, provisioned = provisioner
    bad = read_back(context, '103', job_uid='bad uid')
    assert provisioner._provision_with_retry_backoff(jobs=[bad]).status is True
    assert provisioned == [] and scheduler.held == ['103']


def test_a_failed_hold_still_provisions_nothing_and_does_not_stop_the_others(
    context, provisioner, monkeypatch
):
    provisioner, scheduler, _, provisioned = provisioner

    def refuse(job_id):
        raise exceptions.general_exception('qhold failed')

    monkeypatch.setattr(scheduler, 'hold_job', refuse)
    bad = read_back(context, '104', job_uid='a`b`')
    good = read_back(context, '105', job_uid='goodUid5')
    provisioner._provision_with_retry_backoff(jobs=[bad, good])
    assert provisioned == [['105']]


@pytest.mark.parametrize(
    'resources',
    [{'job_uid': 'a"b'}, {'job_uid': 'goodUid6', 'job_group': 'custom-$(x)'}],
)
def test_the_job_directory_is_refused_for_a_read_back_value(context, resources):
    # the admin "provision" action builds a stack without the provisioner loop
    job = read_back(context, '106', **resources)
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.get_job_dir(job)
    assert 'Invalid Job' in exc_info.value.message


def test_computed_and_custom_groups_still_pass(context):
    assert 'jobs' in context.get_job_dir(read_back(context, '107', job_uid='uid7'))
    assert context.get_job_dir(
        read_back(context, '108', job_uid='uid8', job_group='custom-abc')
    )


@pytest.mark.parametrize(
    'job_uid,accepted',
    [(uid, True) for uid in JOB_UID_ACCEPTED]
    + [(uid, False) for uid in JOB_UID_REFUSED],
)
def test_the_read_back_check_takes_every_written_job_uid_form(
    context, provisioner, job_uid, accepted
):
    # signoff2 verify-hosts #5: a submitter's job_uid with an underscore, queued before the
    # upgrade, was held although every use of the value accepts it
    provisioner, scheduler, _, provisioned = provisioner
    job = read_back(context, '120', job_uid=job_uid)
    provisioner._provision_with_retry_backoff(jobs=[job])
    assert (provisioned == [['120']]) is accepted
    assert (scheduler.held == ['120']) is not accepted


def test_a_keep_forever_pool_whose_stack_uuid_has_an_underscore_gets_its_job_dir(
    context,
):
    # verify-scheduler (signoff2) #3b: the always-on action uses the profile's stack_uuid
    # as job_id and job_uid; one written through the API with an underscore was refused
    from ideadatamodel import SocaJobParams, SocaJobProvisioningOptions

    pool = SocaJob(
        job_id='pool_west',
        job_uid='pool_west',
        params=SocaJobParams(job_group='custom-west'),
        provisioning_options=SocaJobProvisioningOptions(keep_forever=True),
    )
    assert context.get_job_dir(pool).endswith('/jobs/custom-west')
