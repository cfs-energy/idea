"""
unit tests for the shell snippet that picks the last ssh logout time out of wtmp
"""

import ast
import inspect
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from ideavirtualdesktopcontroller.app.ssm_commands import (
    virtual_desktop_ssm_commands_utils,
)

# a wtmp listing where the most recent logout is not the alphabetically largest month name
LAST_OUTPUT_MIXED_MONTHS = """ec2-user pts/2        10.0.1.99        Tue Aug 18 08:00:00 2026 - Tue Aug 18 11:59:00 2026  (03:59)
ec2-user pts/0        10.0.1.23        Wed Feb 11 09:00:00 2026 - Wed Feb 11 10:00:00 2026  (01:00)
ec2-user pts/3        10.0.1.23        Sun Sep 28 22:03:11 2025 - Sun Sep 28 23:10:00 2025  (01:07)
reboot   system boot  0.0.0.0          Mon Aug 17 09:00:00 2026   still running

wtmp begins Sun Sep 28 22:03:11 2025
"""

# no record carries a logout time at all
LAST_OUTPUT_NO_LOGOUTS = """reboot   system boot  0.0.0.0          Mon Aug 17 09:00:00 2026   still running
ec2-user pts/8        10.0.1.5         Mon Aug 17 07:00:00 2026   still logged in
ec2-user pts/9        10.0.1.5         Tue Aug 18 07:00:00 2026 - gone - no logout

wtmp begins Sun Sep 28 22:03:11 2025
"""


def _gnu_date_available() -> bool:
    if shutil.which('date') is None:
        return False
    result = subprocess.run(
        ['date', '--version'], capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and 'GNU' in result.stdout


requires_shell = pytest.mark.skipif(
    shutil.which('bash') is None or shutil.which('awk') is None,
    reason='bash and awk are required to exercise the ssm shell snippet',
)


def _shell_assignment(prefix: str) -> str:
    """
    Pull the shipped shell line out of the ssm command list, so the test runs the
    snippet the controller actually sends rather than a copy of it.
    """
    module = ast.parse(inspect.getsource(virtual_desktop_ssm_commands_utils))
    matches = [
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(prefix)
    ]
    assert len(matches) == 1, f'expected exactly one shell line starting {prefix}'
    return matches[0]


def _run_snippet(tmp_path: Path, last_output: str) -> str:
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    fixture = tmp_path / 'last.txt'
    fixture.write_text(last_output)
    fake_last = fake_bin / 'last'
    fake_last.write_text(f'#!/bin/sh\ncat {fixture}\n')
    fake_last.chmod(fake_last.stat().st_mode | stat.S_IXUSR)

    script = '\n'.join(
        [
            _shell_assignment('SSH_Last_Disconnect_Time='),
            'printf %s "$SSH_Last_Disconnect_Time"',
        ]
    )
    env = dict(os.environ)
    env['PATH'] = f'{fake_bin}{os.pathsep}{env["PATH"]}'
    result = subprocess.run(
        ['bash', '-c', script], capture_output=True, text=True, env=env, check=True
    )
    return result.stdout


@requires_shell
def test_latest_logout_wins_over_an_older_alphabetically_larger_month(tmp_path):
    assert _run_snippet(tmp_path, LAST_OUTPUT_MIXED_MONTHS) == 'Aug 18 11:59:00 2026'


@requires_shell
def test_records_without_a_logout_time_produce_no_timestamp(tmp_path):
    assert _run_snippet(tmp_path, LAST_OUTPUT_NO_LOGOUTS) == ''


@requires_shell
@pytest.mark.skipif(
    not _gnu_date_available(), reason='the iso conversion step needs GNU date'
)
def test_selected_logout_converts_to_the_expected_iso_timestamp(tmp_path):
    selected = _run_snippet(tmp_path, LAST_OUTPUT_MIXED_MONTHS)
    result = subprocess.run(
        ['date', '-u', '-d', selected, '+%Y-%m-%dT%H:%M:%S'],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, 'TZ': 'UTC'},
    )
    assert result.stdout.strip() == '2026-08-18T11:59:00'
