"""Bootstrap must release cloud-final's completion socket before requesting shutdown."""

import os
from pathlib import Path
import re
import subprocess

import pytest

from test_image_bake_bootstrap import render

BOOTSTRAP = Path(__file__).resolve().parents[2] / 'idea-bootstrap'
COMMON = BOOTSTRAP / 'common/bootstrap_common.sh'


@pytest.mark.parametrize('schedule_status', [0, 1])
def test_reboot_is_queued_after_cloud_final_without_blocking(tmp_path, schedule_status):
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'''source "{COMMON}"
timeout() {{ shift; "$@"; }}
fail_bootstrap() {{ exit 1; }}
systemd-run() {{ printf '%s\\n' "$@" > "$CALLS"; return {schedule_status}; }}
reboot() {{ echo immediate-reboot; exit 99; }}
reboot_after_cloud_init
echo bootstrap-returned
''',
        ],
        env=dict(os.environ, CALLS=str(tmp_path / 'calls')),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == schedule_status, result.stdout + result.stderr
    assert 'immediate-reboot' not in result.stdout
    assert ('bootstrap-returned' in result.stdout) == (schedule_status == 0)
    args = (tmp_path / 'calls').read_text().splitlines()
    # The worker, not a dependency that can wait forever, gates completion.
    assert '--property=After=cloud-final.service' not in args
    assert '--on-active=5s' in args
    assert '--no-block' in args
    assert '--wait' not in args and '--scope' not in args
    assert 'complete_bootstrap_reboot' in args[-3]
    assert args[-1] == str(COMMON)


def test_no_linux_userdata_path_reboots_directly():
    direct = re.compile(
        r'^\s*(?:(?:sudo|exec|command)\s+)?(?:/\S*/)?(?:reboot|shutdown|systemctl\s+reboot)\b',
        re.M,
    )
    for path in BOOTSTRAP.rglob('*.jinja2'):
        assert not direct.search(path.read_text()), path.relative_to(BOOTSTRAP)


@pytest.mark.parametrize('base_os', ['ubuntu2604', 'rocky9'])
@pytest.mark.parametrize('history_count,schedule_status', [(0, 0), (0, 1), (9, 0)])
def test_desktop_reboot_preserves_continuation_and_loop_guard(
    tmp_path, base_os, history_count, schedule_status
):
    text = render('virtual-desktop-host-linux/configure_dcv_host.sh.jinja2', base_os)
    tail = text[
        text.index('set_reboot_required "Reboot after DCV host configuration"') :
    ]
    tail = tail.replace('/root/bootstrap/', f'{tmp_path}/')
    (tmp_path / 'reboot_history.log').write_text('earlier reboot\n' * history_count)
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'''source "{COMMON}"
BOOTSTRAP_DIR="{tmp_path}"
SCRIPT_DIR="{tmp_path}"
crontab() {{ if [[ "$1" == - ]]; then cat > "$CRON"; fi; }}
timeout() {{ shift; "$@"; }}
fail_bootstrap() {{ exit 1; }}
systemd-run() {{ echo queued > "$QUEUED"; return {schedule_status}; }}
reboot() {{ echo immediate-reboot; exit 99; }}
{tail}
echo fell-through
''',
        ],
        env=dict(
            os.environ, CRON=str(tmp_path / 'cron'), QUEUED=str(tmp_path / 'queued')
        ),
        capture_output=True,
        text=True,
        timeout=5,
    )
    loop = history_count == 9
    assert result.returncode == (1 if loop else schedule_status), (
        result.stdout + result.stderr
    )
    assert (
        'immediate-reboot' not in result.stdout and 'fell-through' not in result.stdout
    )
    assert (tmp_path / 'cron').read_text().strip() == (
        f'@reboot /bin/bash {tmp_path}/configure_dcv_host_post_reboot.sh crontab'
    )
    assert (tmp_path / 'queued').exists() == (not loop)
    assert (
        len((tmp_path / 'reboot_history.log').read_text().splitlines())
        == history_count + 1
    )
    assert (tmp_path / 'reboot_required.txt').read_text() == ('no' if loop else 'yes')


def test_reboot_schedule_failure_is_tagged(tmp_path):
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'''source "{COMMON}"
BOOTSTRAP_DIR="{tmp_path}"
systemd-run() {{ return 1; }}
curl() {{ echo i-test; }}
aws() {{ printf '%s\\n' "$*" >> "$CALLS"; }}
timeout() {{ shift; "$@"; }}
export AWS_REGION=us-east-2
reboot_after_cloud_init
echo continued
''',
        ],
        env=dict(os.environ, CALLS=str(tmp_path / 'tags')),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'continued' not in result.stdout
    assert (tmp_path / 'bootstrap_failed.txt').exists()
    assert (
        'Key=idea:BootstrapStatus,Value=reboot-failed'
        in (tmp_path / 'tags').read_text()
    )


@pytest.mark.parametrize(
    'scenario',
    ['finishes', 'stuck', 'pending', 'status-error', 'reboot-error', 'no-shutdown'],
)
def test_reboot_worker_wait_is_bounded_and_failures_are_visible(tmp_path, scenario):
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'''source "{COMMON}"
BOOTSTRAP_DIR="{tmp_path}"
attempt=0
systemctl() {{
  attempt=$(( $(cat "$COUNT" 2>/dev/null || echo 0) + 1 ))
  echo "$attempt" > "$COUNT"
  [[ "$SCENARIO" != status-error ]] || return 1
  if [[ "$SCENARIO" == stuck || "$attempt" -eq 1 ]]; then
    printf 'ActiveState=activating\\nSubState=start\\nJob=123\\n'
  elif [[ "$SCENARIO" == pending ]]; then
    printf 'ActiveState=inactive\\nSubState=dead\\nJob=123\\n'
  else
    printf 'ActiveState=active\\nSubState=exited\\nJob=0\\n'
  fi
}}
sleep() {{ :; }}
curl() {{ echo i-test; }}
aws() {{ printf '%s\\n' "$*" >> "$CALLS"; }}
timeout() {{
  shift
  if [[ "$1" == /sbin/reboot ]]; then
    echo reboot >> "$REBOOTS"
    [[ "$SCENARIO" != reboot-error ]] || return 1
    [[ "$SCENARIO" != finishes ]] || exit 0
    return 0
  fi
  "$@"
}}
export AWS_REGION=us-east-2
complete_bootstrap_reboot
echo continued
''',
        ],
        env=dict(
            os.environ,
            SCENARIO=scenario,
            CALLS=str(tmp_path / 'tags'),
            COUNT=str(tmp_path / 'count'),
            REBOOTS=str(tmp_path / 'reboots'),
        ),
        capture_output=True,
        text=True,
        timeout=5,
    )
    success = scenario == 'finishes'
    assert result.returncode == (0 if success else 1), result.stdout + result.stderr
    assert 'continued' not in result.stdout
    count = int((tmp_path / 'count').read_text())
    assert 1 <= count <= 60
    assert (tmp_path / 'reboots').exists() == (
        scenario in ('finishes', 'reboot-error', 'no-shutdown')
    )
    assert (tmp_path / 'bootstrap_failed.txt').exists() == (not success)
    if not success:
        assert (
            'Key=idea:BootstrapStatus,Value=reboot-failed'
            in (tmp_path / 'tags').read_text()
        )
