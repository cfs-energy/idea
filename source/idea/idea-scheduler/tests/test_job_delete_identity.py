"""
Test Cases for how a job is deleted for its owner

qdel runs as the job owner, without a shell, with the batch client's settings and none of
the scheduler's own environment. this walks the real delete path and the real command
runner; qdel is a stand-in that records what it was given.
"""

import getpass
import stat

import pytest

from ideadatamodel import SocaJob
from ideascheduler.app.scheduler.openpbs import openpbs_constants
from ideascheduler.app.scheduler.openpbs.openpbs_scheduler import OpenPBSScheduler

SERVICE_ONLY = (
    'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
    'ECS_CONTAINER_METADATA_URI_V4',
    'IDEA_CLUSTER_NAME',
)
JOB_ID = '1234; marker'


@pytest.fixture()
def scheduler(context, monkeypatch, tmp_path):
    record = tmp_path / 'record'
    record.mkdir()
    qdel = tmp_path / 'qdel'
    qdel.write_text(
        '#!/bin/sh\n'
        f'for arg in "$@"; do printf "%s\\n" "$arg"; done > {record}/argv\n'
        f'env > {record}/env\n'
    )
    qdel.chmod(qdel.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(openpbs_constants, 'QDEL', str(qdel))
    for name in SERVICE_ONLY:
        monkeypatch.setenv(name, 'service-value')
    monkeypatch.setenv('PBS_CONF_FILE', '/etc/pbs.conf')

    scheduler = OpenPBSScheduler(context)
    monkeypatch.setattr(
        scheduler,
        'get_job',
        lambda job_id: SocaJob(job_id=job_id, owner=getpass.getuser()),
    )
    scheduler.record = record
    return scheduler


def test_qdel_runs_as_the_owner_with_only_the_user_environment(scheduler):
    scheduler.delete_job(JOB_ID)

    assert (scheduler.record / 'argv').read_text().splitlines() == [JOB_ID]
    env = dict(
        line.split('=', 1)
        for line in (scheduler.record / 'env').read_text().splitlines()
        if '=' in line
    )
    for name in SERVICE_ONLY:
        assert name not in env
    assert env['PBS_CONF_FILE'] == '/etc/pbs.conf'
    assert env['USER'] == getpass.getuser()


def test_a_job_whose_owner_no_longer_resolves_is_deleted_by_the_service(
    scheduler, monkeypatch
):
    # the user sync drops a disabled owner before the accounts guard deletes the job
    monkeypatch.setattr(
        scheduler,
        'get_job',
        lambda job_id: SocaJob(job_id=job_id, owner='no-such-account-marker'),
    )
    scheduler.delete_job(JOB_ID)
    assert (scheduler.record / 'argv').read_text().splitlines() == [JOB_ID]


def test_a_root_owned_job_is_deleted_by_the_service(scheduler, monkeypatch):
    # `su root -c qdel` worked before; root is never a user commands are run for
    monkeypatch.setattr(
        scheduler, 'get_job', lambda job_id: SocaJob(job_id=job_id, owner='root')
    )
    scheduler.delete_job(JOB_ID)
    assert (scheduler.record / 'argv').read_text().splitlines() == [JOB_ID]
