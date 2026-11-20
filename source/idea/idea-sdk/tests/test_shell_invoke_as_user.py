"""
a command the service runs for a user starts from a clean environment: the search path,
locale, time zone and batch client settings, and the user's own identity. nothing else of
the service process reaches it, and its arguments are passed as given, with no shell.
"""

import getpass
import os
import pwd

import pytest

from ideasdk.shell import ShellInvoker

SERVICE_ONLY = {
    'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI': 'service-value',
    'AWS_ACCESS_KEY_ID': 'service-value',
    'AWS_DEFAULT_REGION': 'us-east-1',
    'ECS_CONTAINER_METADATA_URI_V4': 'http://service/v4',
    'IDEA_CLUSTER_NAME': 'service-cluster',
    'DD_API_KEY': 'service-value',
    'BASH_ENV': '/service/bash_env',
}
BATCH_CLIENT = {
    'PBS_CONF_FILE': '/etc/pbs.conf',
    'PBS_EXEC': '/opt/pbs',
    'PBS_HOME': '/var/spool/pbs',
}


def environment_of(output: str) -> dict:
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


def test_a_user_command_gets_the_batch_settings_and_none_of_the_service_environment(
    monkeypatch, tmp_path
):
    for name, value in {**SERVICE_ONLY, **BATCH_CLIENT, 'LC_ALL': 'C'}.items():
        monkeypatch.setenv(name, value)
    account = pwd.getpwnam(getpass.getuser())

    result = ShellInvoker().invoke_as_user(
        account.pw_name, ['/usr/bin/env'], cwd=str(tmp_path)
    )

    assert result.returncode == 0
    env = environment_of(result.stdout)
    for name, value in BATCH_CLIENT.items():
        assert env.get(name) == value
    for name in SERVICE_ONLY:
        assert name not in env
    assert env['LC_ALL'] == 'C'
    assert env['PATH'] == os.environ['PATH']
    assert env['HOME'] == account.pw_dir
    assert env['USER'] == env['LOGNAME'] == account.pw_name


def test_a_user_command_receives_its_arguments_unchanged(tmp_path):
    value = 'my project; $HOME `x` "quoted"'

    result = ShellInvoker().invoke_as_user(
        getpass.getuser(), ['/usr/bin/printf', '%s', value], cwd=str(tmp_path)
    )

    assert result.returncode == 0
    assert result.stdout == value


def test_a_user_command_must_be_an_argument_list(tmp_path):
    with pytest.raises(ValueError):
        ShellInvoker().invoke_as_user(getpass.getuser(), 'env', cwd=str(tmp_path))


@pytest.mark.skipif(os.geteuid() == 0, reason='root may run as any account')
def test_a_command_for_another_account_is_refused_without_privilege(tmp_path):
    other = next(
        account.pw_name
        for account in pwd.getpwall()
        if account.pw_uid not in (0, os.geteuid())
    )
    with pytest.raises(PermissionError):
        ShellInvoker().invoke_as_user(other, ['/usr/bin/true'], cwd=str(tmp_path))


def test_the_working_folder_is_entered_by_the_command_after_the_identity_change(
    monkeypatch, tmp_path
):
    # subprocess enters cwd before it changes identity, so cwd must not be handed to it
    from ideasdk.shell import shell_invoker

    seen = {}
    real_run = shell_invoker.subprocess.run

    def recording_run(*args, **kwargs):
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(shell_invoker.subprocess, 'run', recording_run)

    result = ShellInvoker().invoke_as_user(
        getpass.getuser(), ['/bin/pwd'], cwd=str(tmp_path)
    )

    assert result.returncode == 0
    assert 'cwd' not in seen
    assert seen['args'][:4] == ['/usr/bin/env', '-C', str(tmp_path), '--']
    assert os.path.realpath(result.stdout) == os.path.realpath(tmp_path)


def test_a_working_folder_that_does_not_exist_fails_the_command(tmp_path):
    result = ShellInvoker().invoke_as_user(
        getpass.getuser(), ['/bin/pwd'], cwd=str(tmp_path / 'missing')
    )

    assert result.returncode != 0


def test_standard_output_can_be_discarded_while_errors_are_kept():
    import getpass

    result = ShellInvoker().invoke_as_user(
        getpass.getuser(),
        ['/bin/sh', '-c', 'echo out; echo err >&2'],
        capture_stdout=False,
    )
    assert result.returncode == 0
    assert result.stdout is None
    assert result.stderr == 'err'
