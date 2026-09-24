from pathlib import Path
import shutil
import subprocess

import pytest

from ideadatamodel import VirtualDesktopBaseOS, VirtualDesktopSessionState
from ideasdk.utils import Jinja2Utils
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    BOOTSTRAP_REFRESH_LINUX_BASE_OS,
)
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
    BOOTSTRAP_REFRESH_VERSION,
    BOOTSTRAP_REFRESH_TEMPLATES,
)
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_db import (
    VirtualDesktopSSMCommandType,
)


def test_sweep_selects_ready_linux_unmarked_servers_and_caps_at_five(refresh):
    for state in VirtualDesktopSessionState:
        if state != VirtualDesktopSessionState.READY:
            refresh.add_session(state=state)
    for base_os in VirtualDesktopBaseOS:
        if base_os not in BOOTSTRAP_REFRESH_LINUX_BASE_OS:
            refresh.add_session(base_os=base_os)
    refresh.add_session(version=BOOTSTRAP_REFRESH_VERSION)
    refresh.add_session().server = None
    refresh.add_session().server.instance_id = None
    missing = refresh.add_session()
    del refresh.servers.rows[missing.server.instance_id]
    eligible = [refresh.add_session() for _ in range(7)]

    assert refresh.sweep.refresh_bootstrap() == 5
    assert [request['InstanceIds'][0] for request in refresh.ssm.requests] == [
        session.server.instance_id for session in eligible[:5]
    ]
    assert len(refresh.session_db.cursors) > 1
    assert refresh.sweep.refresh_bootstrap() == 2
    assert [request['InstanceIds'][0] for request in refresh.ssm.requests[5:]] == [
        session.server.instance_id for session in eligible[5:]
    ]


@pytest.mark.parametrize(
    'base_os', sorted(BOOTSTRAP_REFRESH_LINUX_BASE_OS | set(VirtualDesktopBaseOS))
)
def test_linux_selection_matches_bootstrap_template(refresh, base_os):
    env = Jinja2Utils.env_using_file_system_loader(refresh.context.get_bootstrap_dir())
    block = env.get_template('_templates/linux/ssh_post_quantum_kex.jinja2').render(
        context={'base_os': base_os}
    )
    assert ('/etc/ssh/sshd_config.d/10-idea-kex.conf' in block) == (
        base_os in BOOTSTRAP_REFRESH_LINUX_BASE_OS
    )


@pytest.mark.parametrize(
    'base_os',
    [os for os in VirtualDesktopBaseOS if os in BOOTSTRAP_REFRESH_LINUX_BASE_OS],
)
def test_command_renders_shared_template_and_records_completion_metadata(
    refresh, base_os
):
    session = refresh.add_session(base_os=base_os)
    command_id = refresh.command_utils.submit_ssm_command_to_refresh_bootstrap(session)
    request = refresh.ssm.requests[0]
    payload = request['Parameters']['commands'][0]
    env = Jinja2Utils.env_using_file_system_loader(refresh.context.get_bootstrap_dir())
    expected = '\n'.join(
        env.get_template(template).render(context={'base_os': base_os})
        for template in BOOTSTRAP_REFRESH_TEMPLATES
    )
    assert payload == (
        'log_info() { echo "$@"; }\nlog_warning() { echo "$@"; }\n' + expected
    )
    assert '/etc/krb5.conf.d/00-idea-file-ccache' in payload
    assert 'default_ccache_name = FILE:/tmp/krb5cc_%%{uid}' in payload
    assert 'rm -f /etc/dconf/db/local.d/00-idea-online-accounts' in payload
    assert '/etc/ssh/sshd_config.d/10-idea-kex.conf' in payload
    assert 'if sshd -t 2>/dev/null; then' in payload
    assert 'systemctl restart sshd || systemctl restart ssh || true' in payload
    subprocess.run(['bash', '-n'], input=payload, text=True, check=True)
    assert request['InstanceIds'] == [session.server.instance_id]
    assert request['DocumentName'] == 'AWS-RunShellScript'
    assert request['NotificationConfig']['NotificationType'] == 'Invocation'
    assert request['NotificationConfig']['NotificationEvents'] == ['All']
    record = refresh.monitor._ssm_commands_db.get(command_id)
    assert record.command_type == VirtualDesktopSSMCommandType.REFRESH_BOOTSTRAP
    assert record.additional_payload == {
        'idea_session_id': session.idea_session_id,
        'idea_session_owner': session.owner,
        'instance_id': session.server.instance_id,
        'refresh_version': BOOTSTRAP_REFRESH_VERSION,
    }


def test_success_marks_server_and_second_sweep_submits_nothing(refresh):
    session = refresh.add_session()
    assert refresh.sweep.refresh_bootstrap() == 1
    before = dict(refresh.servers.rows[session.server.instance_id])
    refresh.complete('Success')
    server = refresh.server_db.get(session.server.instance_id)
    assert server.bootstrap_refresh_version == BOOTSTRAP_REFRESH_VERSION
    assert refresh.servers.rows[server.instance_id] == {
        **before,
        'bootstrap_refresh_version': server.bootstrap_refresh_version,
    }
    assert session.server.bootstrap_refresh_version is None
    assert refresh.sweep.refresh_bootstrap() == 0
    assert len(refresh.ssm.requests) == 1
    assert refresh.commands.rows == {}
    row = refresh.server_db.convert_server_object_to_db_dict(server)
    assert row['bootstrap_refresh_version'] == server.bootstrap_refresh_version
    update = refresh.servers.updates[0]
    assert update['UpdateExpression'] == ('SET #version = :version')
    assert update['ConditionExpression'] == (
        'attribute_exists(#instance_id) AND '
        '(attribute_not_exists(#version) OR #version < :version)'
    )


@pytest.mark.parametrize('status', ['Failed', 'Cancelled', 'TimedOut'])
def test_failure_warns_once_leaves_server_unmarked_and_allows_retry(refresh, status):
    session = refresh.add_session()
    refresh.sweep.refresh_bootstrap()
    refresh.complete(status)
    assert (
        refresh.server_db.get(session.server.instance_id).bootstrap_refresh_version
        is None
    )
    assert refresh.servers.updates == []
    refresh.context.logger().warning.assert_called_once_with(
        f'Bootstrap refresh failed for session {session.idea_session_id}: {status}'
    )
    assert refresh.commands.rows == {}
    assert refresh.sweep.refresh_bootstrap() == 1


@pytest.mark.parametrize('status', ['Pending', 'InProgress', 'Delayed'])
def test_nonterminal_notifications_do_not_mark_or_discard_command(refresh, status):
    session = refresh.add_session()
    refresh.sweep.refresh_bootstrap()
    refresh.complete(status)
    assert (
        refresh.server_db.get(session.server.instance_id).bootstrap_refresh_version
        is None
    )
    assert 'command-1' in refresh.commands.rows
    refresh.context.logger().warning.assert_not_called()
    refresh.complete('Success')
    assert (
        refresh.server_db.get(session.server.instance_id).bootstrap_refresh_version
        is not None
    )


def test_submission_failures_are_capped_and_do_not_starve_later_sessions(refresh):
    sessions = [refresh.add_session() for _ in range(7)]
    refresh.ssm.failure = RuntimeError('unavailable\nextra detail')
    assert refresh.sweep.refresh_bootstrap() == 0
    assert len(refresh.ssm.requests) == 5
    warnings = refresh.context.logger().warning.call_args_list
    assert [call.args[0] for call in warnings] == [
        f'Bootstrap refresh failed for session '
        f'{session.idea_session_id}: unavailable extra detail'
        for session in sessions[:5]
    ]
    refresh.ssm.failure = None
    assert refresh.sweep.refresh_bootstrap() == 2


def test_session_becoming_ready_is_picked_up_next_pass(refresh):
    session = refresh.add_session(state=VirtualDesktopSessionState.PROVISIONING)
    assert refresh.sweep.refresh_bootstrap() == 0
    session.state = VirtualDesktopSessionState.READY
    assert refresh.sweep.refresh_bootstrap() == 1


def test_background_sweep_runs_at_start_and_every_six_hours(refresh_loop):
    app, clock = refresh_loop
    calls = []
    app._bootstrap_session_utils.refresh_bootstrap.side_effect = lambda: calls.append(
        clock.now
    )
    app._refresh_bootstrap_loop()
    assert calls == [0, 21600, 43200]
    assert set(app._bootstrap_exit.waits) == {60}


def test_background_sweep_waits_for_leadership_and_survives_failure(refresh_loop):
    app, clock = refresh_loop
    app.context.is_leader.side_effect = lambda: clock.now >= 60
    app._bootstrap_session_utils.refresh_bootstrap.side_effect = RuntimeError(
        'unavailable'
    )
    app._refresh_bootstrap_loop()
    assert app._bootstrap_session_utils.refresh_bootstrap.call_count == 2
    assert app.context.logger().warning.call_count == 2


def test_sweep_resumes_after_running_out_of_time(refresh):
    refresh.add_session()
    assert refresh.sweep.refresh_bootstrap(time_budget_ms=0) == 0
    assert refresh.sweep.refresh_bootstrap() == 1


def test_completion_does_not_recreate_a_deleted_server(refresh):
    session = refresh.add_session()
    refresh.sweep.refresh_bootstrap()
    del refresh.servers.rows[session.server.instance_id]
    refresh.complete('Success')
    assert refresh.servers.rows == {}
    assert refresh.servers.updates == []
    assert refresh.commands.rows == {}


def test_app_starts_and_stops_refresh_in_background(refresh_app):
    refresh_app.app_start()
    refresh_app._bootstrap_thread.start.assert_called_once_with()
    refresh_app._bootstrap_session_utils.refresh_bootstrap.assert_not_called()
    refresh_app.app_stop()
    refresh_app._bootstrap_exit.set.assert_called_once_with()
    refresh_app._bootstrap_thread.join.assert_called_once_with()


def test_initialize_dbs_keeps_the_objects_the_sweep_needs(refresh_loop, monkeypatch):
    from ideavirtualdesktopcontroller.app import (
        virtual_desktop_controller_app as app_module,
    )

    app, _ = refresh_loop
    app.context.get_resources_dir.return_value = str(
        Path(__file__).resolve().parents[1] / 'resources'
    )
    initialized = []
    for name in (
        'VirtualDesktopSessionCounterDB',
        'VirtualDesktopSSMCommandsDB',
        'VirtualDesktopServerDB',
        'VirtualDesktopSoftwareStackDB',
        'VirtualDesktopScheduleDB',
        'VirtualDesktopSessionDB',
        'VirtualDesktopPermissionProfileDB',
        'VirtualDesktopSessionPermissionDB',
    ):
        monkeypatch.setattr(
            getattr(app_module, name),
            'initialize',
            lambda self: initialized.append(type(self).__name__),
        )
    app._initialize_image_builds = lambda: None
    app._initialize_dbs()
    assert app._session_db.schedule_db is app._schedule_db
    assert app._session_db.server_db is app._server_db
    assert len(initialized) == 8


@pytest.mark.parametrize('version', [None, BOOTSTRAP_REFRESH_VERSION - 1])
def test_missing_or_older_version_is_refreshed_even_with_legacy_marker(
    refresh, version
):
    session = refresh.add_session(version=version, legacy_marked=True)
    assert refresh.sweep.refresh_bootstrap() == 1
    refresh.complete('Success')
    assert (
        refresh.server_db.get(session.server.instance_id).bootstrap_refresh_version
        == BOOTSTRAP_REFRESH_VERSION
    )
    assert refresh.sweep.refresh_bootstrap() == 0


@pytest.mark.parametrize(
    'version', [BOOTSTRAP_REFRESH_VERSION, BOOTSTRAP_REFRESH_VERSION + 1]
)
def test_current_or_newer_version_is_not_refreshed(refresh, version):
    refresh.add_session(version=version)
    assert refresh.sweep.refresh_bootstrap() == 0
    assert refresh.ssm.requests == []


def test_completion_records_submitted_version_and_cannot_downgrade(refresh):
    session = refresh.add_session()
    refresh.sweep.refresh_bootstrap()
    payload = refresh.commands.rows['command-1']['additional_payload']
    payload['refresh_version'] = BOOTSTRAP_REFRESH_VERSION - 1
    refresh.complete('Success')
    assert (
        refresh.server_db.get(session.server.instance_id).bootstrap_refresh_version
        == BOOTSTRAP_REFRESH_VERSION - 1
    )
    assert refresh.sweep.refresh_bootstrap() == 1
    refresh.servers.rows[session.server.instance_id]['bootstrap_refresh_version'] = (
        BOOTSTRAP_REFRESH_VERSION + 1
    )
    refresh.complete('Success', command_id='command-2')
    assert (
        refresh.server_db.get(session.server.instance_id).bootstrap_refresh_version
        == BOOTSTRAP_REFRESH_VERSION + 1
    )
    assert refresh.commands.rows == {}


def test_legacy_command_completion_does_not_mark_the_versioned_set(refresh):
    session = refresh.add_session()
    refresh.sweep.refresh_bootstrap()
    command = refresh.commands.rows['command-1']
    command['command_type'] = VirtualDesktopSSMCommandType.REFRESH_SSH_KEX
    del command['additional_payload']['refresh_version']
    refresh.complete('Success')
    assert (
        refresh.server_db.get(session.server.instance_id).bootstrap_refresh_version
        is None
    )
    assert refresh.commands.rows == {}


def _online_accounts_script(refresh, tmp_path):
    env = Jinja2Utils.env_using_file_system_loader(refresh.context.get_bootstrap_dir())
    block = env.get_template('_templates/linux/gnome_online_accounts.jinja2').render()
    etc = tmp_path / 'etc'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ['mkdir', 'grep', 'sed', 'rm', 'printf', 'cat']:
        found = shutil.which(name)
        if found:
            (bin_dir / name).symlink_to(found)
    dconf = bin_dir / 'dconf'
    dconf.write_text('#!/bin/sh\nexit 0\n')
    dconf.chmod(0o755)
    script = 'log_info() { echo "$@"; }\n' + block.replace('/etc/', f'{etc}/')
    return etc, bin_dir, script


def _run(script, bin_dir, check=True):
    return subprocess.run(
        ['/bin/bash'],
        input=script,
        text=True,
        check=check,
        env={'PATH': str(bin_dir)},
        capture_output=True,
    )


def test_red_hat_family_moves_tickets_to_files_idempotently(refresh, tmp_path):
    etc, bin_dir, script = _online_accounts_script(refresh, tmp_path)
    (etc / 'krb5.conf.d').mkdir(parents=True)
    (etc / 'krb5.conf').write_text(
        '[libdefaults]\n    default_ccache_name = KEYRING:persistent:%{uid}\n'
    )
    (etc / 'krb5.conf.d/kcm_default_ccache').write_text(
        '[libdefaults]\n    default_ccache_name = KCM:\n'
    )
    for _ in range(2):
        _run(script, bin_dir)
    assert '#    default_ccache_name = KEYRING' in (etc / 'krb5.conf').read_text()
    assert (etc / 'krb5.conf.d/kcm_default_ccache').read_text().count('#') == 1
    assert (etc / 'krb5.conf.d/00-idea-file-ccache').read_text() == (
        '[libdefaults]\n    default_ccache_name = FILE:/tmp/krb5cc_%{uid}\n'
    )


def test_ubuntu_style_config_is_left_alone(refresh, tmp_path):
    etc, bin_dir, script = _online_accounts_script(refresh, tmp_path)
    (etc / 'krb5.conf.d').mkdir(parents=True)
    original = '[libdefaults]\n    default_realm = IDEA.LOCAL\n'
    (etc / 'krb5.conf').write_text(original)
    _run(script, bin_dir)
    assert (etc / 'krb5.conf').read_text() == original
    assert not (etc / 'krb5.conf.d/00-idea-file-ccache').exists()


def test_the_earlier_online_accounts_lock_is_removed(refresh, tmp_path):
    etc, bin_dir, script = _online_accounts_script(refresh, tmp_path)
    locks = etc / 'dconf/db/local.d/locks'
    locks.mkdir(parents=True)
    (etc / 'dconf/db/local.d/00-idea-online-accounts').write_text(
        "[org/gnome/online-accounts]\nwhitelisted-providers=['']\n"
    )
    (locks / 'idea-online-accounts').write_text(
        '/org/gnome/online-accounts/whitelisted-providers\n'
    )
    _run(script, bin_dir)
    assert not (etc / 'dconf/db/local.d/00-idea-online-accounts').exists()
    assert not (locks / 'idea-online-accounts').exists()
    # A failed database update must fail the SSM command, leaving it eligible to retry.
    (etc / 'dconf/db/local.d/00-idea-online-accounts').write_text('x')
    (bin_dir / 'dconf').write_text('#!/bin/sh\nexit 1\n')
    assert _run(script, bin_dir, check=False).returncode == 1
