"""The owner's locked Wayland display stays visible without unlocking or typing."""

import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_dcv_host_ami_builder_bootstrap import IDEA_BOOTSTRAP_DIR
from test_image_bake_bootstrap import render


@pytest.fixture
def lock():
    path = Path(IDEA_BOOTSTRAP_DIR) / '_templates/linux/dcv_wayland_lock_helper.jinja2'
    loader = importlib.machinery.SourceFileLoader('dcv_wayland_lock', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def log_messages(lock, monkeypatch):
    messages = []
    # Observe the helper directly; SDK tests can reconfigure root logging.
    monkeypatch.setattr(
        lock.LOG, 'warning', lambda message, *args: messages.append(message % args)
    )
    return messages


class Remote:
    def __init__(self):
        self.locked = False
        self.calls = []

    def check(self, path):
        pass

    def close(self):
        pass

    def is_locked(self):
        return self.locked

    def call(self, path, method, signature=None, args=()):
        self.calls.append((method, args))
        return ('/session',) if method == 'CreateSession' else ()


def test_input_is_locked_only_net_zero_and_session_is_released(lock):
    remote = Remote()
    keeper = lock.Keeper(remote)
    keeper.tick()
    assert remote.calls == []
    remote.locked = True
    keeper.tick()
    keeper.tick()
    assert remote.calls == [
        ('CreateSession', ()),
        ('Start', ()),
        ('NotifyPointerMotionRelative', (1.0, 0.0)),
        ('NotifyPointerMotionRelative', (-1.0, 0.0)),
        ('NotifyPointerMotionRelative', (1.0, 0.0)),
        ('NotifyPointerMotionRelative', (-1.0, 0.0)),
    ]
    remote.locked = False
    keeper.tick()
    keeper.tick()
    assert remote.calls[-1] == ('Stop', ())
    assert len(remote.calls) == 7
    remote.locked = True
    keeper.tick()
    assert remote.calls[7:9] == [('CreateSession', ()), ('Start', ())]


def test_unlock_during_start_prevents_input(lock, monkeypatch):
    remote = Remote()
    remote.locked = True
    original = remote.call

    def call(path, method, signature=None, args=()):
        result = original(path, method, signature, args)
        if method == 'Start':
            remote.locked = False
        return result

    monkeypatch.setattr(remote, 'call', call)
    lock.Keeper(remote).tick()
    assert [c[0] for c in remote.calls] == ['CreateSession', 'Start', 'Stop']


def test_lock_read_failure_never_injects(lock, monkeypatch):
    remote = Remote()
    remote.locked = True
    keeper = lock.Keeper(remote)
    keeper.tick()
    remote.calls.clear()

    def failed():
        raise RuntimeError('logind unavailable')

    monkeypatch.setattr(remote, 'is_locked', failed)
    assert keeper.tick() is False
    assert keeper.disabled
    assert remote.calls == []


@pytest.mark.parametrize('change', ['owner', 'user', 'type', 'second', 'missing'])
def test_owner_selection_rejects_wrong_or_ambiguous_session(lock, change):
    session = dict(
        owner='alice', user='alice', type='virtual', **{'xdg-session-id': '7'}
    )
    assert lock.owner_session([session], 'alice') == '7'
    sessions = [session]
    if change in ('owner', 'user', 'type'):
        session[change] = 'other'
    elif change == 'second':
        sessions.append(session.copy())
    else:
        sessions = []
    with pytest.raises(ValueError):
        lock.owner_session(sessions, 'alice')


@pytest.mark.parametrize(
    'field,value',
    [
        ('User', (999, '/user')),
        ('Class', 'greeter'),
        ('Type', 'x11'),
        ('Remote', False),
        ('Seat', ('seat0', '/seat')),
        ('State', 'closing'),
    ],
)
def test_lock_check_rejects_non_owner_or_non_remote_session(
    lock, monkeypatch, field, value
):
    props = dict(
        User=(5000, '/user'),
        Class='user',
        Type='wayland',
        Remote=True,
        Seat=('', '/'),
        State='active',
        LockedHint=True,
    )
    remote = object.__new__(lock.Remote)
    remote.system, remote.path = object(), '/session'
    monkeypatch.setattr(lock.os, 'getuid', lambda: 5000)
    monkeypatch.setattr(remote, 'invoke', lambda *args: (props,))
    assert remote.is_locked()
    props[field] = value
    with pytest.raises(ValueError):
        remote.is_locked()


@pytest.mark.parametrize('base_os', ['rhel10', 'rocky10', 'ubuntu2604'])
def test_installed_for_fresh_and_baked_wayland_sessions(base_os):
    for template in (
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
    ):
        text = render(template, base_os)
        assert '/usr/local/libexec/idea-dcv-lock-visible' in text
        assert '/etc/systemd/user/gnome-session.target.wants' in text
        assert 'PartOf=graphical-session.target' in text
        assert 'NotifyPointerMotionRelative' in text
        assert 'NotifyKeyboard' not in text
        assert 'ScreenCast' not in text


@pytest.mark.parametrize('base_os', ['rocky9', 'ubuntu2404', 'amazonlinux2023'])
def test_x11_does_not_install_lock_service(base_os):
    assert 'idea-dcv-lock-visible' not in render(
        '_templates/linux/dcv_server.jinja2', base_os
    )


MANAGER_XML = """<node><interface name="org.gnome.Mutter.RemoteDesktop">
<method name="CreateSession"><arg type="o" direction="out"/></method>
</interface></node>"""
SESSION_XML = """<node><interface name="org.gnome.Mutter.RemoteDesktop.Session">
<method name="Start"/><method name="Stop"/>
<method name="NotifyPointerMotionRelative"><arg type="d"/><arg type="d"/></method>
</interface></node>"""


@pytest.mark.parametrize(
    'method', ['CreateSession', 'Start', 'Stop', 'NotifyPointerMotionRelative']
)
@pytest.mark.parametrize('change', ['missing', 'signature', 'interface'])
def test_introspection_rejects_missing_or_changed_contract(lock, method, change):
    manager = method == 'CreateSession'
    xml = MANAGER_XML if manager else SESSION_XML
    interface = lock.REMOTE if manager else lock.REMOTE + '.Session'
    lock.validate_interface(xml, interface)
    if change == 'missing':
        xml = xml.replace(f'name="{method}"', 'name="Renamed"')
    elif change == 'interface':
        xml = xml.replace(interface, 'org.example.Other')
    else:
        xml = xml.replace(
            f'<method name="{method}"/>',
            f'<method name="{method}"><arg type="s"/></method>',
        )
        xml = xml.replace(
            f'<method name="{method}">', f'<method name="{method}"><arg type="s"/>'
        )
    with pytest.raises(
        ValueError, match=interface if change == 'interface' else method
    ):
        lock.validate_interface(xml, interface)


def test_missing_create_session_warns_once_and_never_calls_or_polls(
    lock, monkeypatch, log_messages
):
    remote = Remote()
    monkeypatch.setattr(lock, 'desktop_version', lambda: 'GNOME 48; mutter 48.2-test')
    remote.check = lambda path: lock.validate_interface(
        MANAGER_XML.replace('CreateSession', 'Renamed'), lock.REMOTE
    )
    remote.close = lambda: None
    keeper = lock.Keeper(remote)
    keeper.startup()
    # A disabled keeper must not even ask logind whether the screen is locked.
    remote.is_locked = lambda: pytest.fail('polled after disabling')
    for _ in range(5):
        assert keeper.tick() is False
    keeper.stop()
    assert remote.calls == []
    assert len(log_messages) == 1
    assert 'CreateSession' in log_messages[0]
    assert 'mutter 48.2-test' in log_messages[0]
    assert 'disabled for this session' in log_messages[0]


@pytest.mark.parametrize(
    'method', ['CreateSession', 'Start', 'NotifyPointerMotionRelative', 'Stop']
)
@pytest.mark.parametrize('error', ['UnknownInterface', 'UnknownMethod', 'InvalidArgs'])
def test_call_failure_permanently_disables(
    lock, monkeypatch, log_messages, method, error
):
    remote = Remote()
    remote.locked = True
    original = remote.call
    closed = []

    def call(path, called, signature=None, args=()):
        if called == method:
            remote.calls.append((called, args))
            raise RuntimeError(f'org.freedesktop.DBus.Error.{error}: {called}')
        return original(path, called, signature, args)

    remote.call = call
    remote.close = lambda: closed.append(True)
    monkeypatch.setattr(lock, 'desktop_version', lambda: 'mutter test-version')
    keeper = lock.Keeper(remote)
    keeper.tick()
    remote.locked = False
    keeper.tick()
    assert keeper.disabled
    calls = list(remote.calls)
    for _ in range(5):
        assert keeper.tick() is False
        keeper.stop()
    assert remote.calls == calls
    assert closed == [True]
    assert len(log_messages) == 1
    assert error in log_messages[0] and method in log_messages[0]


@pytest.mark.parametrize('base_os', ['rhel10', 'rocky10', 'ubuntu2604'])
def test_lock_service_cannot_restart_after_degradation(base_os):
    text = render('_templates/linux/dcv_wayland_login.jinja2', base_os)
    unit = text.split("<<'EOF_LOCK_SERVICE'", 1)[1].split('EOF_LOCK_SERVICE', 1)[0]
    assert 'Restart=no' in unit
    assert 'StandardError=journal' in unit
    assert 'Type=simple' in unit
    assert 'RequiredBy=' not in unit and 'Requires=' not in unit
    assert 'Before=' not in unit
    assert 'TimeoutStopSec=10' in unit


def test_initialization_failure_is_soft(lock, monkeypatch, log_messages):
    def failed():
        raise RuntimeError('session bus unavailable')

    monkeypatch.setattr(lock, 'Remote', failed)
    monkeypatch.setattr(lock, 'desktop_version', lambda: 'mutter test-version')
    assert lock.main() == 0
    assert len(log_messages) == 1
    assert 'session bus unavailable' in log_messages[0]


def test_event_loop_failure_is_soft_and_closes_bus(lock, monkeypatch, log_messages):
    remote = Remote()
    closed = []

    def failed():
        raise RuntimeError('event loop unavailable')

    remote.GLib = SimpleNamespace(MainLoop=failed)
    remote.close = lambda: closed.append(True)
    monkeypatch.setattr(lock, 'Remote', lambda: remote)
    monkeypatch.setattr(lock, 'desktop_version', lambda: 'mutter test-version')
    assert lock.main() == 0
    assert closed == [True]
    assert len(log_messages) == 1


def test_closing_bus_does_not_exit_the_idle_helper(lock):
    calls = []
    remote = object.__new__(lock.Remote)
    remote.bus = SimpleNamespace(
        set_exit_on_close=lambda value: calls.append(('exit_on_close', value)),
        close_sync=lambda value: calls.append(('close', value)),
    )
    remote.close()
    assert calls == [('exit_on_close', False), ('close', None)]


@pytest.mark.parametrize('method', ['Start', 'NotifyPointerMotionRelative', 'Stop'])
def test_missing_session_method_never_starts_or_sends_input(
    lock, monkeypatch, log_messages, method
):
    remote = Remote()
    remote.locked = True
    closed = []

    def check(path):
        if path != lock.ROOT:
            lock.validate_interface(
                SESSION_XML.replace(f'name="{method}"', 'name="Renamed"'),
                lock.REMOTE + '.Session',
            )

    remote.check = check
    remote.close = lambda: closed.append(True)
    monkeypatch.setattr(lock, 'desktop_version', lambda: 'mutter test-version')
    keeper = lock.Keeper(remote)
    for _ in range(5):
        assert keeper.tick() is False
    keeper.stop()
    # Session introspection is possible only after CreateSession gives us its path.
    assert remote.calls == [('CreateSession', ())]
    assert closed == [True]
    assert len(log_messages) == 1
    assert method in log_messages[0]


def test_rechecks_contract_before_each_new_session(lock):
    remote = Remote()
    checked = []
    remote.check = checked.append
    keeper = lock.Keeper(remote)
    keeper.startup()
    assert checked == [lock.ROOT]
    remote.locked = True
    keeper.tick()
    keeper.tick()
    assert checked == [lock.ROOT, lock.ROOT, '/session']
    remote.locked = False
    keeper.tick()
    remote.locked = True
    keeper.tick()
    assert checked == [lock.ROOT, lock.ROOT, '/session', lock.ROOT, '/session']


def test_remote_checks_live_introspection(lock, monkeypatch):
    remote = object.__new__(lock.Remote)
    remote.bus = object()
    calls = []

    def invoke(*args):
        calls.append(args)
        return (MANAGER_XML if args[2] == lock.ROOT else SESSION_XML,)

    monkeypatch.setattr(remote, 'invoke', invoke)
    for path in (lock.ROOT, '/session'):
        remote.check(path)
    assert calls == [
        (
            remote.bus,
            lock.REMOTE,
            path,
            'org.freedesktop.DBus.Introspectable',
            'Introspect',
        )
        for path in (lock.ROOT, '/session')
    ]


@pytest.mark.parametrize('failure_at', ['startup', 'timer', 'signal'])
def test_main_stays_idle_without_retrying_or_polling(
    lock, monkeypatch, log_messages, failure_at
):
    remote = Remote()
    timers, removed, signals = [], [], []
    remote.path = '/login-session'
    remote.Gio = SimpleNamespace(DBusSignalFlags=SimpleNamespace(NONE=0))
    remote.system = SimpleNamespace(
        signal_subscribe=lambda *args: signals.append(args[-1])
    )

    def incompatible(path):
        raise ValueError('CreateSession is missing')

    def run():
        if failure_at == 'startup':
            assert timers == []
        else:
            remote.locked = True
            remote.check = incompatible
            if failure_at == 'timer':
                assert timers[0]() is False
            else:
                signals[0](None, None, None, None, None, parameters)
            assert removed == [1]
        remote.is_locked = lambda: pytest.fail('disabled helper polled logind')
        for _ in range(5):
            signals[0](None, None, None, None, None, parameters)

    parameters = SimpleNamespace(
        unpack=lambda: (lock.LOGIN + '.Session', {'LockedHint': True}, [])
    )
    remote.GLib = SimpleNamespace(
        MainLoop=lambda: SimpleNamespace(run=run, quit=lambda: None),
        PRIORITY_DEFAULT=0,
        unix_signal_add=lambda *args: None,
        timeout_add_seconds=lambda interval, callback: timers.append(callback) or 1,
        source_remove=removed.append,
    )
    if failure_at == 'startup':
        remote.check = incompatible
    monkeypatch.setattr(lock, 'Remote', lambda: remote)
    monkeypatch.setattr(lock, 'desktop_version', lambda: 'mutter test-version')
    assert lock.main() == 0
    assert remote.calls == []
    assert len(log_messages) == 1


@pytest.mark.parametrize('rpm', [True, False])
def test_warning_version_queries_installed_desktop_packages(lock, monkeypatch, rpm):
    commands = []
    monkeypatch.setattr(
        lock.shutil, 'which', lambda name: '/usr/bin/rpm' if rpm else None
    )

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs['timeout'] == 5
        return SimpleNamespace(stdout='gnome-shell 48.2\nmutter 48.2-1\n')

    monkeypatch.setattr(lock.subprocess, 'run', run)
    assert lock.desktop_version() == 'gnome-shell 48.2; mutter 48.2-1'
    assert commands[0] == (
        ['rpm', '-q', 'gnome-shell', 'mutter']
        if rpm
        else [
            'dpkg-query',
            '-W',
            '-f=${Package} ${Version}\\n',
            'gnome-shell',
            'mutter-common',
        ]
    )


def test_helper_does_not_change_wallpapers():
    path = Path(IDEA_BOOTSTRAP_DIR) / '_templates/linux/dcv_wayland_lock_helper.jinja2'
    text = path.read_text()
    assert 'reset_missing_wallpapers' not in text
    assert 'gsettings' not in text
