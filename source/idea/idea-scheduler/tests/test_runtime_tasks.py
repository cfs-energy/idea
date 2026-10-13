"""Offline container startup and archive controls; no PBS or AWS calls."""

import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

from botocore.exceptions import ClientError
import pytest

from ideascheduler.app import runtime_tasks as tasks

ROOT = Path(__file__).resolve().parents[4]
ROLE = ROOT / 'deployment/ecr/idea-control-plane/roles/scheduler.sh'
SCRIPT = ROOT / 'source/idea/idea-scheduler/resources/scripts/send_logs_s3.sh'


@pytest.fixture
def pbs_home(tmp_path):
    (tmp_path / 'server_priv').mkdir()
    (tmp_path / 'sched_priv').mkdir()
    (tmp_path / 'server_priv/resourcedef').write_text('site_resource type=string\n')
    (tmp_path / 'sched_priv/sched_config').write_text(
        '# resources: "example"\nresources: "ncpus, mem, site_resource"\n'
        'strict_ordering: true\nserver_dyn_res: "site_resource !/bin/site"\n'
    )
    return tmp_path


def license(name='app_lic_one', command='/bin/check --seats'):
    return {'name': name, 'availability_check_cmd': command}


def test_restore_licenses_paginates_and_repeated_start_is_idempotent(pbs_home):
    table = Mock()
    table.scan.side_effect = [
        {'Items': [license()], 'LastEvaluatedKey': {'name': 'app_lic_one'}},
        {'Items': [license('app_lic_two')]},
    ]
    tasks.configure_licenses(table, pbs_home)
    assert table.scan.call_args_list[1].kwargs == {
        'ConsistentRead': True,
        'ExclusiveStartKey': {'name': 'app_lic_one'},
    }
    files = [pbs_home / 'server_priv/resourcedef', pbs_home / 'sched_priv/sched_config']
    first = [file.read_text() for file in files]
    table.scan.side_effect = None
    table.scan.return_value = {'Items': [license(), license('app_lic_two')]}
    tasks.configure_licenses(table, pbs_home)
    assert [file.read_text() for file in files] == first
    assert 'app_lic_one type=long' in first[0]
    assert 'app_lic_two type=long' in first[0]
    assert (
        'resources: "ncpus, mem, site_resource, compute_node, app_lic_one, app_lic_two"'
        in first[1]
    )
    assert 'server_dyn_res: "app_lic_one !/bin/check --seats"' in first[1]
    assert 'server_dyn_res: "site_resource !/bin/site"' in first[1]
    assert 'strict_ordering: true' in first[1]
    table.scan.return_value = {'Items': [license(command='/bin/new-check')]}
    tasks.configure_licenses(table, pbs_home)
    assert 'server_dyn_res: "app_lic_one !/bin/new-check"' in files[1].read_text()
    assert files[1].read_text().count('server_dyn_res: "app_lic_one ') == 1


@pytest.mark.parametrize(
    'code',
    ['ResourceNotFoundException', 'AccessDeniedException', 'ThrottlingException'],
)
def test_only_absent_license_table_is_deferred(pbs_home, code):
    table = Mock()
    table.scan.side_effect = ClientError({'Error': {'Code': code}}, 'Scan')
    if code == 'ResourceNotFoundException':
        tasks.configure_licenses(table, pbs_home)
    else:
        with pytest.raises(ClientError):
            tasks.configure_licenses(table, pbs_home)
    assert (
        pbs_home / 'server_priv/resourcedef'
    ).read_text() == 'site_resource type=string\n'


@pytest.mark.parametrize(
    'item',
    [
        license('bad;name'),
        license(command=''),
        license(command='check\nresources: bad'),
    ],
)
def test_invalid_license_config_is_not_written(pbs_home, item):
    table = Mock()
    table.scan.return_value = {'Items': [item]}
    before = (pbs_home / 'sched_priv/sched_config').read_text()
    with pytest.raises(ValueError):
        tasks.configure_licenses(table, pbs_home)
    assert (pbs_home / 'sched_priv/sched_config').read_text() == before


def test_conflicting_resource_type_fails_loudly(pbs_home):
    file = pbs_home / 'server_priv/resourcedef'
    file.write_text('app_lic_one type=string\n')
    table = Mock()
    table.scan.return_value = {'Items': [license()]}
    with pytest.raises(ValueError, match='Conflicting'):
        tasks.configure_licenses(table, pbs_home)
    assert file.read_text() == 'app_lic_one type=string\n'


def test_bastion_discovery_uses_interface_addresses_and_checks_dns(monkeypatch):
    ec2 = Mock()
    paginator = ec2.get_paginator.return_value
    paginator.paginate.return_value = [
        {'NetworkInterfaces': [{'PrivateIpAddress': '192.0.2.1'}]},
        {'NetworkInterfaces': [{'PrivateIpAddress': '192.0.2.2'}]},
    ]
    monkeypatch.setattr(
        tasks.socket, 'gethostbyaddr', lambda ip: (f'host-{ip}', [], [ip])
    )
    monkeypatch.setattr(
        tasks.socket, 'gethostbyname_ex', lambda host: (host, [], [host[5:]])
    )
    assert tasks.bastion_hosts(ec2, 'sg-bastion') == [
        'host-192.0.2.1',
        'host-192.0.2.2',
    ]
    assert paginator.paginate.call_args.kwargs['Filters'] == [
        {'Name': 'group-id', 'Values': ['sg-bastion']},
        {'Name': 'interface-type', 'Values': ['interface']},
        {'Name': 'status', 'Values': ['in-use']},
    ]
    monkeypatch.setattr(
        tasks.socket, 'gethostbyname_ex', lambda host: (host, [], ['192.0.2.9'])
    )
    with pytest.raises(ValueError, match='forward-confirm'):
        tasks.bastion_hosts(ec2, 'sg-bastion')


def test_missing_bastion_is_loud():
    ec2 = Mock()
    ec2.get_paginator.return_value.paginate.return_value = [{'NetworkInterfaces': []}]
    with pytest.raises(ValueError, match='No active bastion'):
        tasks.bastion_hosts(ec2, 'sg-bastion')


@pytest.mark.parametrize(
    'failure',
    [
        None,
        subprocess.CalledProcessError(1, 'bash'),
        subprocess.TimeoutExpired('bash', 3600),
    ],
)
def test_archive_runs_now_then_daily_and_survives_failure(monkeypatch, failure):
    run = Mock(side_effect=[failure, None])
    monkeypatch.setattr(tasks, 'run_archive', run)
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.side_effect = [False, True]
    tasks.archive_logs('/image/send_logs_s3.sh', stop)
    assert run.call_count == 2
    run.assert_called_with('/image/send_logs_s3.sh')
    assert [call.args for call in stop.wait.call_args_list] == [(86400,), (86400,)]


def test_archive_stopped_before_start_does_nothing(monkeypatch):
    run = Mock()
    monkeypatch.setattr(tasks, 'run_archive', run)
    stop = Mock()
    stop.is_set.return_value = True
    tasks.archive_logs('/image/send_logs_s3.sh', stop)
    run.assert_not_called()


def hook_harness(tmp_path, existing='', fail=''):
    source = ROLE.read_text()
    block = source[
        source.index('# Reconcile image-owned hooks') : source.index('# The NLB name')
    ]
    log = tmp_path / 'qmgr.log'
    harness = """set -euo pipefail
log() { :; }
qmgr() {
  printf '%s\\n' "$2" >> "$CALLS"
  [[ "$2" != "$FAIL" ]] || return 42
  if [[ "$2" == 'list hook' ]]; then printf '%s\\n' "$EXISTING"; fi
}
""" + block.replace('/opt/pbs/bin/qmgr', 'qmgr')
    result = subprocess.run(
        ['bash', '-c', harness],
        env={
            **os.environ,
            'CALLS': str(log),
            'EXISTING': existing,
            'FAIL': fail,
            'IDEA_APP_DEPLOY_DIR': '/image',
        },
        capture_output=True,
        text=True,
    )
    return result, log.read_text().splitlines()


@pytest.mark.parametrize(
    'existing', ['', 'Hook validate_job\nHook job_status\nHook calculate_ncpus\n']
)
def test_hooks_created_only_if_absent_but_always_reimported(tmp_path, existing):
    result, calls = hook_harness(tmp_path, existing)
    assert result.returncode == 0, result.stderr
    assert sum(command.startswith('create hook') for command in calls) == (
        0 if existing else 3
    )
    assert sum(command.startswith('import hook') for command in calls) == 3
    assert "set hook validate_job event='queuejob,modifyjob,movejob'" in calls
    assert "set hook job_status event='runjob,execjob_begin,execjob_end'" in calls
    assert 'set hook calculate_ncpus enabled=true' in calls
    assert 'set hook calculate_ncpus order=2' in calls


@pytest.mark.parametrize(
    'fail',
    [
        'list hook',
        'create hook validate_job',
        'import hook validate_job application/x-python default /image/scheduler/resources/openpbs/hooks/openpbs_hook_handler.py',
    ],
)
def test_hook_failure_aborts_startup(tmp_path, fail):
    result, calls = hook_harness(tmp_path, fail=fail)
    assert result.returncode == 42
    assert calls[-1] == fail


def test_reconciliation_is_outside_first_create_guard_and_before_scheduler():
    source = ROLE.read_text()
    # Bash execution tests above cover the body; check its placement in the actual startup.
    before = source[: source.index('# Reconcile image-owned hooks')]
    assert before.rstrip().endswith('fi')
    assert source.index('runtime_tasks licenses') < source.index('pbs_init.d start')
    assert source.index('> "${PBS_HOME}/pbs_environment"') < source.index(
        'pbs_init.d start'
    )
    assert source.index('pbs server is answering') < source.index(
        '# Reconcile image-owned hooks'
    )
    assert source.index('set hook calculate_ncpus order=2') < source.index(
        'log "starting pbs_sched"'
    )
    assert 'wait -n "${IDEA_PID}" "${ARCHIVE_PID}" "${SYNC_PID}"' in source


@pytest.mark.parametrize(
    'container,existing,expected',
    [
        (True, 'root@old-host', ['root@task-host']),
        (True, 'root@task-host', []),
        (False, 'root@old-host', ['root@legacy-host']),
    ],
)
def test_manager_grants_are_current_and_do_not_duplicate(
    tmp_path, container, existing, expected
):
    source = ROLE.read_text()
    block = source[
        source.index('# The NLB name') : source.index('log "starting pbs_sched"')
    ]
    log = tmp_path / 'qmgr.log'
    harness = """set -euo pipefail
log() { :; }
setting() {
  case "$1" in
    bastion-host.service_name) printf '%s' "$SERVICE" ;;
    cluster.network.security_groups.bastion-host) echo sg-bastion ;;
    bastion-host.private_dns_name) echo legacy-host ;;
  esac
}
python3.13() {
  [[ "$*" == '-m ideascheduler.app.runtime_tasks bastion-hosts sg-bastion' ]] || return 99
  echo task-host
}
qmgr() {
  if [[ "$2" == 'list server managers' ]]; then
    printf 'Server scheduler\\n    managers = %s\\n' "$EXISTING"
  else
    printf '%s\\n' "$2" >> "$CALLS"
  fi
}
""" + block.replace('/opt/pbs/bin/qmgr', 'qmgr')
    log.touch()
    result = subprocess.run(
        ['bash', '-c', harness],
        env={
            **os.environ,
            'CALLS': str(log),
            'EXISTING': existing,
            'SERVICE': 'cluster-bastion' if container else '',
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        f'set server managers += {host}' for host in expected
    ]


@pytest.mark.parametrize('fail_upload', [False, True])
def test_archive_script_uploads_correct_paths_and_prunes_only_after_success(
    tmp_path, fail_upload
):
    import time

    # Replace external commands only; run the real script, directory checks and find.
    pbs = tmp_path / 'pbs'
    compute = tmp_path / 'apps/scheduler/compute_node/logs'
    directories = [
        pbs / 'server_logs',
        pbs / 'sched_logs',
        pbs / 'server_priv/accounting',
        compute,
    ]
    old_files = []
    for directory in directories:
        directory.mkdir(parents=True)
        old = directory / 'old.log'
        old.write_text('archivable')
        os.utime(old, (time.time() - 12 * 86400,) * 2)
        old_files.append(old)
    old_compute_dir = compute / 'old-node'
    old_compute_dir.mkdir()
    (old_compute_dir / 'log').write_text('old compute log')
    os.utime(old_compute_dir, (time.time() - 12 * 86400,) * 2)
    current = pbs / 'server_logs/current.log'
    current.write_text('current')
    calls = tmp_path / 'aws.log'
    harness = """aws() { printf '%s\\n' "$*" >> "$CALLS"; return "$FAIL"; }
flock() { return 0; }
""" + SCRIPT.read_text()
    env = {
        **os.environ,
        'PBS_HOME': str(pbs),
        'IDEA_CLUSTER_HOME': str(tmp_path / 'apps'),
        'IDEA_MODULE_ID': 'scheduler',
        'IDEA_CLUSTER_S3_BUCKET': 'cluster-bucket',
        'CALLS': str(calls),
        'FAIL': '1' if fail_upload else '0',
    }
    result = subprocess.run(
        ['bash', '-c', harness], env=env, capture_output=True, text=True
    )
    assert result.returncode == (1 if fail_upload else 0), result.stderr
    assert current.exists()
    assert all(file.exists() == fail_upload for file in old_files[:3])
    assert old_compute_dir.exists() == fail_upload
    if not fail_upload:
        assert (
            f's3 sync {compute} s3://cluster-bucket/logs/scheduler/compute_nodes/'
            in calls.read_text()
        )
        # A second pass on already-pruned directories is harmless.
        assert (
            subprocess.run(
                ['bash', '-c', harness], env=env, capture_output=True
            ).returncode
            == 0
        )


def test_archive_script_skips_overlap(tmp_path):
    harness = """flock() { return 1; }
aws() { echo 'must not upload' >&2; return 99; }
""" + SCRIPT.read_text()
    result = subprocess.run(
        ['bash', '-c', harness],
        env={
            **os.environ,
            'PBS_HOME': str(tmp_path),
            'IDEA_CLUSTER_HOME': str(tmp_path),
            'IDEA_MODULE_ID': 'scheduler',
            'IDEA_CLUSTER_S3_BUCKET': 'cluster-bucket',
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert not result.stderr


@pytest.mark.parametrize(
    'outcome', [0, 1, subprocess.TimeoutExpired('bash', 3600), SystemExit(0)]
)
def test_archive_cleans_up_process_group_even_on_shutdown(monkeypatch, outcome):
    popen = Mock()
    process = popen.return_value.__enter__ = Mock()
    child = process.return_value
    child.pid = 12345
    child.returncode = outcome if isinstance(outcome, int) else None
    child.wait.side_effect = [outcome]
    popen.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(tasks.subprocess, 'Popen', popen)
    killpg = Mock()
    monkeypatch.setattr(tasks.os, 'killpg', killpg)
    if outcome == 0:
        tasks.run_archive('/image/script.sh')
    else:
        error = subprocess.CalledProcessError if outcome == 1 else type(outcome)
        with pytest.raises(error):
            tasks.run_archive('/image/script.sh')
    popen.assert_called_once_with(
        ['/bin/bash', '/image/script.sh'], start_new_session=True
    )
    child.wait.assert_called_once_with(timeout=3600)
    killpg.assert_called_once_with(12345, tasks.signal.SIGKILL)


def test_restored_files_pass_existing_license_validation(pbs_home, monkeypatch):
    from ideadatamodel import HpcLicenseResource
    from ideascheduler.app.licenses import license_service

    table = Mock()
    table.scan.return_value = {'Items': [license()]}
    tasks.configure_licenses(table, pbs_home)
    monkeypatch.setattr(
        license_service,
        'CONFIG_FILE_RESOURCE_DEF',
        pbs_home / 'server_priv/resourcedef',
    )
    monkeypatch.setattr(
        license_service,
        'CONFIG_FILE_SCHED_CONFIG',
        pbs_home / 'sched_priv/sched_config',
    )
    service = object.__new__(license_service.LicenseService)
    service._check_license_availability = Mock(
        return_value=(5, Mock(returncode=0), None)
    )
    service.validate_license_resource(HpcLicenseResource(title='License', **license()))


def test_single_quoted_config_and_double_quoted_command(pbs_home):
    file = pbs_home / 'sched_priv/sched_config'
    file.write_text(
        "resources: 'ncpus, mem'\nserver_dyn_res: 'app_lic_one !/bin/old'\n"
    )
    table = Mock()
    table.scan.return_value = {'Items': [license(command='/bin/check "two words"')]}
    tasks.configure_licenses(table, pbs_home)
    first = file.read_text()
    tasks.configure_licenses(table, pbs_home)
    assert file.read_text() == first
    assert "resources: 'ncpus, mem, compute_node, app_lic_one'" in first
    assert """server_dyn_res: 'app_lic_one !/bin/check "two words"' """.strip() in first
    assert '/bin/old' not in first
