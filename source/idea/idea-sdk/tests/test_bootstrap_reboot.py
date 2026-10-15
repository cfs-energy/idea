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
    # A delay alone races slow final modules; ordering belongs on the service, not the timer.
    assert '--property=After=cloud-final.service' in args
    assert '--on-active=5s' in args
    assert '--no-block' in args
    assert '--wait' not in args and '--scope' not in args
    assert args[-1] == '/sbin/reboot'


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
