"""Execute the generated Linux command with host activity fixtures."""

import json
import os
import shutil
import subprocess

import pytest

from ideadatamodel import VirtualDesktopBaseOS

LINUX = [
    'amazonlinux2023',
    'rhel8',
    'rhel9',
    'rhel10',
    'rocky8',
    'rocky9',
    'rocky10',
    'ubuntu2204',
    'ubuntu2404',
    'ubuntu2604',
]


@pytest.fixture
def command_host(tmp_path, refresh):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for command in [
        'awk',
        'grep',
        'sort',
        'tail',
        'jq',
        'install',
        'cat',
        'touch',
        'rm',
    ]:
        executable = shutil.which(command)
        if executable is None:
            pytest.skip(f'{command} is required for the generated shell command')
        (bin_dir / command).symlink_to(executable)

    def stub(name, body):
        path = bin_dir / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o700)

    stub(
        'dcv',
        """if [ "$1" = list-sessions ]; then
  echo '[{"id":"session"}]'
else
  echo '{"num-of-connections":0,"creation-time":"2026-01-01T00:00:00.000000Z","last-disconnection-time":""}'
fi
""",
    )
    stub('top', "echo '%Cpu(s): 1.0 us, 0.0 sy'\n")
    stub('date', 'printf "%s\\n" "$SAMPLE_TIME"\n')
    stub(
        'ss', 'test "${FAIL_SIGNAL:-}" != ss || exit 1\nprintf "%s\\n" "${SOCKETS:-}"\n'
    )
    stub(
        'loginctl',
        """test "${FAIL_SIGNAL:-}" != loginctl || exit 1
if [ "$1" = list-sessions ]; then
  printf '1 1000 someuser seat0\\n2 1000 someuser\\n3 1000 someuser\\n'
else
  test "${FAIL_SIGNAL:-}" != show-session || exit 1
  case "$2" in
    1) printf 'Service=gdm-password\\nRemote=no\\nState=active\\n' ;;
    2) printf 'Service=sshd\\nRemote=yes\\nState=%s\\n' "${SSH_STATE:-closing}" ;;
    3) printf 'Service=dcv\\nRemote=yes\\nState=active\\n' ;;
  esac
fi
""",
    )

    def run(base_os='ubuntu2604', **variables):
        refresh.command_utils.submit_ssm_command_to_get_cpu_utilization(
            'i-test', 'session', 'someuser', VirtualDesktopBaseOS(base_os)
        )
        request = refresh.ssm.requests[-1]
        assert request['DocumentName'] == 'AWS-RunShellScript'
        script = '\n'.join(request['Parameters']['commands'])
        script = script.replace('/run/idea-idle-detection', str(tmp_path / 'activity'))
        return subprocess.run(
            ['/bin/sh', '-c', script],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                'PATH': str(bin_dir),
                'SAMPLE_TIME': '2026-10-01T12:00:00.000000Z',
                **variables,
            },
        )

    return run


@pytest.mark.parametrize('base_os', LINUX)
@pytest.mark.parametrize(
    'state, expected', [('active', 1), ('online', 1), ('closing', 0)]
)
def test_ssh_sessions_without_last_exclude_graphical_sessions(
    command_host, base_os, state, expected
):
    result = command_host(base_os, SSH_STATE=state)
    assert result.returncode == 0, result.stderr
    sample = json.loads(result.stdout)
    assert sample['SSH_Connection_Count'] == expected
    assert sample['DCV']['num-of-connections'] == expected
    assert sample['CPUAveragePerformanceLast10Secs'] == 1.0
    assert 'last: not found' not in result.stderr


def test_tunnel_without_pam_session_is_counted(command_host):
    result = command_host(SOCKETS='0 0 127.0.0.1:22 127.0.0.1:12345')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['SSH_Connection_Count'] == 1


def test_logout_starts_grace_period_and_quiet_samples_preserve_it(command_host):
    command_host(SSH_STATE='active')
    closed = command_host(SAMPLE_TIME='2026-10-01T12:05:00.000000Z')
    quiet = command_host(SAMPLE_TIME='2026-10-01T12:10:00.000000Z')
    for result in [closed, quiet]:
        assert result.returncode == 0, result.stderr
        sample = json.loads(result.stdout)
        assert sample['SSH_Connection_Count'] == 0
        assert sample['SSH_Last_Disconnect_ISO'] == '2026-10-01T12:05:00.000000Z'


@pytest.mark.parametrize('signal', ['loginctl', 'show-session', 'ss'])
def test_failed_activity_probe_produces_no_idle_sample(command_host, signal):
    result = command_host(FAIL_SIGNAL=signal)
    assert result.returncode != 0
    assert result.stdout == ''
