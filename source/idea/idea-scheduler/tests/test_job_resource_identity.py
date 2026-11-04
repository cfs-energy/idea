from unittest.mock import Mock

import pytest

from ideadatamodel import (
    SocaFSxLustreConfig,
    SocaJob,
    SocaJobParams,
    SocaJobProvisioningOptions,
)
from ideascheduler.app.provisioning.job_provisioner import (
    cloudformation_stack_builder as module,
)


def job(uid, shared=False):
    return SocaJob(
        job_id='42',
        job_uid=uid,
        job_group='shared-group',
        owner='testuser',
        params=SocaJobParams(base_os='amazonlinux2023', instance_types=['c5.large']),
        provisioning_options=SocaJobProvisioningOptions(
            terminate_when_idle=10 if shared else 0
        ),
    )


def resources(context, monkeypatch, current_job):
    builder = object.__new__(module.CloudFormationStackBuilder)
    builder.context, builder.job, builder.logger = context, current_job, Mock()
    context.config().put('cluster.cloudwatch_logs.enabled', True)
    context.config().put('scheduler.cloudwatch_logs.enabled', True)
    captured = {}

    class Captured(Exception):
        pass

    def capture(**kwargs):
        captured.update(kwargs)
        raise Captured

    monkeypatch.setattr(
        module.BootstrapUtils,
        'check_and_attach_cloudwatch_logging_and_metrics',
        capture,
    )
    with pytest.raises(Captured):
        builder.build_user_data()
    return (
        captured['bootstrap_context'].vars.job_directory,
        captured['metrics_namespace'],
        captured['log_files'][0].log_group_name,
    )


def test_reused_pbs_id_has_distinct_directory_metrics_and_logs(
    context, monkeypatch, tmp_path
):
    monkeypatch.setattr(context, 'get_scheduler_dir', lambda: str(tmp_path))
    first = resources(context, monkeypatch, job('unique-first'))
    second = resources(context, monkeypatch, job('unique-second'))
    for old, new in zip(first, second):
        assert old != new
        assert '42-unique-first' in old
        assert '42-unique-second' in new


def test_new_provisioning_leaves_legacy_running_job_files_in_place(
    context, monkeypatch, tmp_path
):
    monkeypatch.setattr(context, 'get_scheduler_dir', lambda: str(tmp_path))
    legacy = tmp_path / 'jobs' / '42'
    legacy.mkdir(parents=True)
    log = legacy / 'running.log'
    log.write_text('running before upgrade\n')
    directory, _, _ = resources(context, monkeypatch, job('after-upgrade'))
    assert directory == str(tmp_path / 'jobs' / '42-after-upgrade')
    assert log.read_text() == 'running before upgrade\n'
    # Existing bootstrap scripts keep their embedded path and can still append there.
    with log.open('a') as stream:
        stream.write('running after upgrade\n')
    assert log.read_text().endswith('running after upgrade\n')


def test_shared_capacity_keeps_group_resource_names(context, monkeypatch, tmp_path):
    monkeypatch.setattr(context, 'get_scheduler_dir', lambda: str(tmp_path))
    first = resources(context, monkeypatch, job('first', shared=True))
    second = resources(context, monkeypatch, job('second', shared=True))
    assert first == second
    assert first[0] == str(tmp_path / 'jobs' / 'shared-group')
    assert first[1].endswith('/job-shared-group')
    assert first[2].endswith('/job-shared-group')


def test_ephemeral_resources_refuse_a_missing_unique_id(context):
    from ideadatamodel.exceptions import SocaException

    with pytest.raises(SocaException, match='job_uid is required'):
        context.get_job_dir(job(None))


@pytest.mark.parametrize('shared', [False, True])
def test_fsx_export_identity_and_explicit_override(shared):
    paths = []
    for uid in ('first', 'second'):
        current = job(uid, shared=shared)
        current.cluster_name = 'cluster'
        current.params.fsx_lustre = SocaFSxLustreConfig(
            enabled=True, s3_backend='s3://results'
        )
        path = current.get_fsx_lustre_export_path()
        paths.append(path)
        suffix = 'shared-group' if shared else f'42-{uid}'
        assert path == f's3://results/cluster-fsxoutput/job-{suffix}'
        current.params.fsx_lustre.export_path = 's3://explicit/output'
        assert current.get_fsx_lustre_export_path() == 's3://explicit/output'
    assert (paths[0] == paths[1]) is shared


def test_fsx_ephemeral_export_requires_unique_id():
    from ideadatamodel.exceptions import SocaException

    current = job(None)
    current.params.fsx_lustre = SocaFSxLustreConfig(
        enabled=True, s3_backend='s3://results'
    )
    with pytest.raises(SocaException, match='job_uid is required'):
        current.get_fsx_lustre_export_path()
    current.params.fsx_lustre.export_path = 's3://explicit/output'
    assert current.get_fsx_lustre_export_path() == 's3://explicit/output'
