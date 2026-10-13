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
    # each check runs from its own script, so the command is not quoted into the config
    script = pbs_home / 'idea/licenses/app_lic_one'
    assert f'server_dyn_res: "app_lic_one !{script}"' in first[1]
    assert script.read_text() == '#!/bin/bash\n/bin/check --seats\n'
    assert script.stat().st_mode & 0o755 == 0o755
    assert 'server_dyn_res: "site_resource !/bin/site"' in first[1]
    assert 'strict_ordering: true' in first[1]
    table.scan.return_value = {'Items': [license(command='/bin/new-check')]}
    tasks.configure_licenses(table, pbs_home)
    assert script.read_text() == '#!/bin/bash\n/bin/new-check\n'
    assert files[1].read_text().count('server_dyn_res: "app_lic_one ') == 1
    # app_lic_two left the table: its declarations, check script and resources entry go
    assert 'app_lic_two' not in files[0].read_text()
    assert 'app_lic_two' not in files[1].read_text()
    assert not (pbs_home / 'idea/licenses/app_lic_two').exists()
    assert 'server_dyn_res: "site_resource !/bin/site"' in files[1].read_text()


@pytest.mark.parametrize(
    'code',
    ['ResourceNotFoundException', 'AccessDeniedException', 'ThrottlingException'],
)
def test_a_table_that_cannot_be_read_leaves_everything_as_it_is(pbs_home, code):
    # a throttle or a denial must not crash-loop the scheduler, nor drop its licenses
    table = Mock()
    table.scan.return_value = {'Items': [license()]}
    tasks.configure_licenses(table, pbs_home)
    files = [pbs_home / 'server_priv/resourcedef', pbs_home / 'sched_priv/sched_config']
    before = [f.read_text() for f in files]
    table.scan.side_effect = ClientError({'Error': {'Code': code}}, 'Scan')
    tasks.configure_licenses(table, pbs_home)
    assert [f.read_text() for f in files] == before
    assert 'app_lic_one type=long' in before[0]


def test_only_licenses_idea_restored_are_removed_when_they_leave_the_table(pbs_home):
    # a *_lic_* resource an operator declared by hand has no row and is not IDEA's to remove
    resourcedef = pbs_home / 'server_priv/resourcedef'
    config = pbs_home / 'sched_priv/sched_config'
    resourcedef.write_text(resourcedef.read_text() + 'hand_lic_made type=long\n')
    config.write_text(
        config.read_text().replace('site_resource"', 'site_resource, hand_lic_made"')
        + 'server_dyn_res: "hand_lic_made !/opt/check"\n'
    )
    table = Mock()
    table.scan.return_value = {'Items': [license(), license('app_lic_two')]}
    tasks.configure_licenses(table, pbs_home)
    table.scan.return_value = {'Items': [license()]}
    tasks.configure_licenses(table, pbs_home)
    assert 'app_lic_two' not in resourcedef.read_text()
    assert 'app_lic_two' not in config.read_text()
    assert 'hand_lic_made type=long' in resourcedef.read_text()
    assert 'server_dyn_res: "hand_lic_made !/opt/check"' in config.read_text()
    assert 'hand_lic_made' in config.read_text().split('server_dyn_res')[0]
    assert (pbs_home / 'idea/licenses/.managed').read_text() == 'app_lic_one\n'


@pytest.mark.parametrize(
    'item',
    [
        license('bad;name'),
        license('app_lic_one/../x'),
        license(command=''),
        license(command='check\x00'),
        {'name': 'app_lic_two'},
    ],
)
def test_a_bad_row_is_skipped_and_the_rest_restored(pbs_home, item):
    # one bad row must not keep the scheduler down
    table = Mock()
    table.scan.return_value = {'Items': [item, license('app_lic_good')]}
    tasks.configure_licenses(table, pbs_home)
    config = (pbs_home / 'sched_priv/sched_config').read_text()
    assert 'app_lic_good !' in config
    assert config.count('server_dyn_res:') == 2  # site_resource and the good one
    assert [
        p.name for p in (pbs_home / 'idea/licenses').iterdir() if p.name != '.managed'
    ] == ['app_lic_good']


def test_a_multi_line_command_runs_from_its_script(pbs_home):
    table = Mock()
    table.scan.return_value = {
        'Items': [license(command='cd /opt/tool\n./check "two words" \'and more\'')]
    }
    tasks.configure_licenses(table, pbs_home)
    script = pbs_home / 'idea/licenses/app_lic_one'
    assert (
        script.read_text()
        == '#!/bin/bash\ncd /opt/tool\n./check "two words" \'and more\'\n'
    )
    config = (pbs_home / 'sched_priv/sched_config').read_text()
    assert f'server_dyn_res: "app_lic_one !{script}"' in config
    assert 'two words' not in config


def test_a_conflicting_resource_type_is_skipped_not_fatal(pbs_home):
    file = pbs_home / 'server_priv/resourcedef'
    file.write_text('app_lic_one type=string\n')
    table = Mock()
    table.scan.return_value = {'Items': [license(), license('app_lic_two')]}
    tasks.configure_licenses(table, pbs_home)
    assert file.read_text() == 'app_lic_one type=string\napp_lic_two type=long\n'
    config = (pbs_home / 'sched_priv/sched_config').read_text()
    assert 'app_lic_two !' in config and 'app_lic_one !' not in config


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


HOOKS_DIR = ROOT / 'source/idea/idea-scheduler/resources/openpbs/hooks'


def hook_harness(tmp_path, existing='', fail='', hooks_dir=None):
    source = ROLE.read_text()
    block = source[
        source.index('# Reconcile image-owned hooks') : source.index('# The NLB name')
    ]
    log = tmp_path / 'qmgr.log'
    log.write_text('')
    harness = """set -euo pipefail
log() { :; }
qmgr() {
  printf '%s\\n' "$2" >> "$CALLS"
  [[ "$2" != "$FAIL" ]] || return 42
  if [[ "$2" == 'list hook' ]]; then printf '%s\\n' "$EXISTING"; fi
}
""" + block.replace('/opt/pbs/bin/qmgr', 'qmgr').replace(
        '${IDEA_APP_DEPLOY_DIR}/scheduler/resources/openpbs/hooks',
        str(hooks_dir or HOOKS_DIR),
    )
    result = subprocess.run(
        ['bash', '-c', harness],
        env={
            **os.environ,
            'CALLS': str(log),
            'EXISTING': existing,
            'FAIL': fail,
            'PBS_HOME': str(tmp_path / 'pbs'),
        },
        capture_output=True,
        text=True,
    )
    return result, log.read_text().splitlines()


ALL_HOOKS = 'Hook validate_job\nHook job_status\n'


@pytest.mark.parametrize('existing', ['', ALL_HOOKS])
def test_hooks_are_created_when_absent_and_imported_when_unrecorded(tmp_path, existing):
    result, calls = hook_harness(tmp_path, existing)
    assert result.returncode == 0, result.stderr
    assert sum(command.startswith('create hook') for command in calls) == (
        0 if existing else 2
    )
    assert sum(command.startswith('import hook') for command in calls) == 2
    assert "set hook validate_job event='queuejob,modifyjob,movejob'" in calls
    assert "set hook job_status event='runjob,execjob_begin,execjob_end'" in calls
    assert 'set hook job_status enabled=true' in calls
    assert not any('calculate_ncpus' in command for command in calls)
    recorded = sorted(p.name for p in (tmp_path / 'pbs/.idea-hooks').iterdir())
    assert recorded == ['job_status.sha256', 'validate_job.sha256']


def test_an_unchanged_hook_is_left_as_the_operator_configured_it(tmp_path):
    # an operator who disabled a hook as a mitigation keeps it disabled until a new image
    # carries a different file; only that reimport re-enables it
    hook_harness(tmp_path, ALL_HOOKS)
    result, calls = hook_harness(tmp_path, ALL_HOOKS)
    assert result.returncode == 0, result.stderr
    assert calls == ['list hook']
    image = tmp_path / 'image'
    image.mkdir()
    for file in HOOKS_DIR.iterdir():
        (image / file.name).write_bytes(file.read_bytes())
    (image / 'openpbs_hook_handler.py').write_text('# a new hook\n')
    result, calls = hook_harness(tmp_path, ALL_HOOKS, hooks_dir=image)
    assert result.returncode == 0, result.stderr
    assert [c for c in calls if c.startswith('import hook')] == [
        f'import hook {name} application/x-python default {image}/openpbs_hook_handler.py'
        for name in ('validate_job', 'job_status')
    ]
    assert 'set hook validate_job enabled=true' in calls
    assert not any(c.startswith('create hook') for c in calls)


def test_the_retired_ncpus_hook_is_deleted_when_present(tmp_path):
    # the hook body PBS keeps from an earlier release would go on rewriting ncpus
    (tmp_path / 'pbs/.idea-hooks').mkdir(parents=True)
    (tmp_path / 'pbs/.idea-hooks/calculate_ncpus.sha256').write_text('old\n')
    result, calls = hook_harness(tmp_path, ALL_HOOKS + 'Hook calculate_ncpus\n')
    assert result.returncode == 0, result.stderr
    assert 'delete hook calculate_ncpus' in calls
    assert not any(
        c.startswith(('import hook calculate', 'create hook calculate')) for c in calls
    )
    assert not (tmp_path / 'pbs/.idea-hooks/calculate_ncpus.sha256').exists()
    result, calls = hook_harness(tmp_path, ALL_HOOKS)
    assert calls == ['list hook']


@pytest.mark.parametrize(
    'fail',
    [
        'list hook',
        'create hook validate_job',
        f'import hook validate_job application/x-python default {HOOKS_DIR}/openpbs_hook_handler.py',
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
    # the previous file is kept, dated, before the rewrite, as the host bootstrap did
    assert source.index(
        'cp -p "${PBS_HOME}/pbs_environment" "${PBS_HOME}/pbs_environment.$(date'
    ) < source.index('> "${PBS_HOME}/pbs_environment"')
    assert source.index('pbs server is answering') < source.index(
        '# Reconcile image-owned hooks'
    )
    assert source.index('delete hook calculate_ncpus') < source.index(
        'log "starting pbs_sched"'
    )
    assert 'wait -n "${IDEA_PID}" "${ARCHIVE_PID}" "${SYNC_PID}"' in source


STALE = 'root@ip-10-7-0-9.us-east-2.compute.internal'
HOST_BASTION = 'ip-10-0-0-5.us-east-2.compute.internal'


def run_managers(tmp_path, container, existing, bastion=None, qmgr_fails=False):
    source = ROLE.read_text()
    block = source[
        source.index('# The NLB name') : source.index('log "starting pbs_sched"')
    ]
    calls = tmp_path / 'qmgr.log'
    calls.touch()
    harness = (
        """set -euo pipefail
log() { echo "$*" >> "$LOG"; }
setting() {
  case "$1" in
    bastion-host.service_name) printf '%s' "$SERVICE" ;;
    bastion-host.private_dns_name) printf '%s' "$BASTION" ;;
    *) printf '%s' "$2" ;;
  esac
}
qmgr() {
  [[ -z "$QMGR_FAILS" ]] || return 1
  if [[ "$2" == 'list server managers' ]]; then
    printf 'Server scheduler\\n    managers = %s\\n' "$EXISTING"
  else
    printf '%s\\n' "$2" >> "$CALLS"
  fi
}
"""
        + block.replace('/opt/pbs/bin/qmgr', 'qmgr')
        + 'echo reached-start >> "$LOG"\n'
    )
    result = subprocess.run(
        ['bash', '-c', harness],
        env={
            **os.environ,
            'CALLS': str(calls),
            'LOG': str(tmp_path / 'entrypoint.log'),
            'EXISTING': existing,
            'SERVICE': 'cluster-bastion' if container else '',
            'BASTION': bastion
            if bastion is not None
            else ('nlb.elb.amazonaws.com' if container else HOST_BASTION),
            'QMGR_FAILS': '1' if qmgr_fails else '',
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return calls.read_text().splitlines(), (
        tmp_path / 'entrypoint.log'
    ).read_text().splitlines()


@pytest.mark.parametrize(
    'container,existing,expected',
    [
        # a container bastion: no grant, and the host-era grant goes
        (True, f'{STALE},root@admin.example', [f'set server managers -= {STALE}']),
        (True, 'root@admin.example', []),
        (True, '', []),
        # a host bastion: its own grant stays or is made, others of the kind go
        (
            False,
            STALE,
            [
                f'set server managers -= {STALE}',
                f'set server managers += root@{HOST_BASTION}',
            ],
        ),
        (False, f'root@{HOST_BASTION}', []),
    ],
)
def test_managers_prune_host_era_grants_and_grant_only_a_host_bastion(
    tmp_path, container, existing, expected
):
    calls, log = run_managers(tmp_path, container, existing)
    assert calls == expected
    assert log[-1] == 'reached-start'
    if STALE in existing:
        assert f'removing stale manager grant {STALE}' in log


def test_a_failing_manager_step_does_not_stop_the_start(tmp_path):
    calls, log = run_managers(tmp_path, True, '', qmgr_fails=True)
    assert calls == []
    assert log == ['could not list managers; leaving them', 'reached-start']


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


def test_the_config_keeps_its_quote_style_and_drops_the_old_entry(pbs_home):
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
    script = pbs_home / 'idea/licenses/app_lic_one'
    assert f"server_dyn_res: 'app_lic_one !{script}'" in first
    assert '/bin/old' not in first
    assert script.read_text() == '#!/bin/bash\n/bin/check "two words"\n'
