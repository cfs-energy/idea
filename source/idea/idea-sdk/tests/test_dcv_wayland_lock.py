"""The owner's locked Wayland display stays visible without unlocking or typing."""

import importlib.machinery
import importlib.util
from pathlib import Path

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


class Remote:
    def __init__(self):
        self.locked = False
        self.calls = []

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
    with pytest.raises(RuntimeError):
        keeper.tick()
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
