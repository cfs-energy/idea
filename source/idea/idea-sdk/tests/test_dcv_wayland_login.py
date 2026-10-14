"""Wayland login must stay on the broker-authorized owner's remote GDM display."""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from test_dcv_host_ami_builder_bootstrap import IDEA_BOOTSTRAP_DIR
from test_image_bake_bootstrap import render

SCRIPT = Path(IDEA_BOOTSTRAP_DIR) / '_templates/linux/dcv_wayland_login_helper.jinja2'
WAYLAND = ['rhel10', 'rocky10', 'ubuntu2604']


@pytest.mark.parametrize('base_os', WAYLAND)
@pytest.mark.parametrize('instance_type', ['m7i.large', 'm7g.large'])
def test_login_is_installed_on_fresh_and_baked_desktops(base_os, instance_type):
    for template in (
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
    ):
        text = render(template, base_os, instance_type)
        assert 'idea-dcv-login.service' in text
        assert 'pam_exec.so expose_authtok' in text
        assert 'session include gdm-password' in text
        assert 'AutomaticLoginEnable' not in text
        assert text.index('# Begin: DCV Wayland owner login') > text.index(
            'Found nice-dcv-server pre-installed'
        )


@pytest.fixture
def login():
    # the helper ships as a .jinja2 template, so name its loader explicitly
    spec = importlib.util.spec_from_loader(
        'dcv_wayland_login',
        importlib.machinery.SourceFileLoader('dcv_wayland_login', str(SCRIPT)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_state():
    sid = 'test-session'
    dcv = {
        'id': sid,
        'owner': 'alice',
        'user': 'alice',
        'type': 'virtual',
        'creation-time': 'today',
        'xdg-session-id': 'c2',
    }
    greeter = {
        'Id': 'c2',
        'User': 42,
        'Name': 'gdm',
        'Class': 'greeter',
        'Remote': True,
        'Seat': '',
        'Type': 'wayland',
    }
    displays = {
        '/display': {
            'SessionId': 'c2',
            'RemoteId': '/com/amazon/dcv/' + sid.encode().hex().upper(),
        }
    }
    return [dcv], [greeter], displays


def test_owner_requires_matching_dcv_and_gdm_session(login):
    assert login.authorized_owner('c2', 42, *fixture_state()) == (
        'alice',
        'test-session',
        'today',
    )


@pytest.mark.parametrize(
    'change',
    [
        'console',
        'physical',
        'different-user',
        'different-greeter',
        'wrong-remote-id',
        'wrong-xdg-session',
        'second-dcv',
        'second-greeter',
        'user-session',
        'no-session',
    ],
)
def test_scope_fails_closed(login, change):
    sessions, logins, displays = fixture_state()
    if change == 'console':
        sessions[0]['type'] = 'console'
    if change == 'physical':
        logins[0]['Seat'] = 'seat0'
        logins[0]['Remote'] = False
    if change == 'different-user':
        sessions[0]['user'] = 'bob'
    if change == 'different-greeter':
        logins[0]['User'] = 99
    if change == 'wrong-remote-id':
        displays['/display']['RemoteId'] = '/other'
    if change == 'wrong-xdg-session':
        sessions[0]['xdg-session-id'] = 'c9'
    if change == 'second-dcv':
        sessions.append(dict(sessions[0], id='other'))
    if change == 'second-greeter':
        logins.append(dict(logins[0], Id='c9'))
    if change == 'user-session':
        logins[0]['Class'] = 'user'
    if change == 'no-session':
        sessions.clear()
    with pytest.raises(ValueError):
        login.authorized_owner('c2', 42, sessions, logins, displays)


class System:
    def caller_session(self, pid):
        return {'Id': 'c2'}

    def owner(self, session_id, uid):
        assert (session_id, uid) == ('c2', 42)
        return ('alice', 'test-session', 'today')


def test_credentials_are_one_use_and_bound_to_owner(login, monkeypatch):
    monkeypatch.setattr(login, 'pam_worker', lambda pid: pid == 200)
    grants = login.Grants(System(), 42)
    issued = grants.handle(100, 42, {'op': 'issue'})
    redeem = {
        'op': 'redeem',
        'user': 'alice',
        'remote': '0.0.0.0',
        'token': issued['token'],
    }
    assert grants.handle(200, 0, redeem) == {'ok': True}
    with pytest.raises(ValueError):
        grants.handle(200, 0, redeem)


@pytest.mark.parametrize(
    'change', ['user', 'remote', 'seat', 'tty', 'uid', 'worker', 'session', 'expired']
)
def test_credential_cannot_authorize_another_login(login, monkeypatch, change):
    monkeypatch.setattr(login, 'pam_worker', lambda pid: pid == 200)
    now = [10]
    monkeypatch.setattr(login.time, 'monotonic', lambda: now[0])
    system = System()
    grants = login.Grants(system, 42)
    issued = grants.handle(100, 42, {'op': 'issue'})
    redeem = {
        'op': 'redeem',
        'user': 'alice',
        'remote': '0.0.0.0',
        'token': issued['token'],
    }
    pid, uid = 200, 0
    if change == 'user':
        redeem['user'] = 'bob'
    if change == 'remote':
        redeem['remote'] = ''
    if change == 'seat':
        redeem['seat'] = 'seat0'
    if change == 'tty':
        redeem['tty'] = '/dev/tty1'
    if change == 'uid':
        uid = 1000
    if change == 'worker':
        pid = 201
    if change == 'session':
        system.owner = lambda *args: ('alice', 'test-session', 'tomorrow')
    if change == 'expired':
        now[0] = 41
    with pytest.raises(ValueError):
        grants.handle(pid, uid, redeem)


@pytest.mark.parametrize('uid', [0, 1000])
def test_only_gdm_can_request_an_owner_credential(login, uid):
    with pytest.raises(ValueError):
        login.Grants(System(), 42).handle(100, uid, {'op': 'issue'})


@pytest.mark.parametrize('base_os', WAYLAND + ['rocky9', 'ubuntu2404'])
def test_ad_account_policy_is_preserved(base_os):
    from jinja2 import Template
    from types import SimpleNamespace

    source = (
        Path(IDEA_BOOTSTRAP_DIR) / '_templates/linux/join_activedirectory.jinja2'
    ).read_text()
    source = source[
        source.index('access_provider = ad') : source.index('auth_provider = ad')
    ]
    text = Template(source).render(
        context=SimpleNamespace(
            base_os=base_os,
            module_name='virtual-desktop-controller',
            vars=SimpleNamespace(session=object()),
        )
    )
    assert ('ad_gpo_map_remote_interactive = +idea-dcv-login' in text) == (
        base_os in WAYLAND
    )
    assert 'ad_gpo_map_permit' not in text
    assert 'access_provider = ad' in text


@pytest.mark.parametrize('base_os', ['rocky9', 'ubuntu2404', 'amazonlinux2023'])
def test_x11_does_not_install_owner_login(base_os):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    assert 'idea-dcv-login' not in text


@pytest.mark.parametrize('base_os', WAYLAND)
def test_installation_is_idempotent_and_contains_no_baked_owner(tmp_path, base_os):
    import subprocess

    text = render('_templates/linux/dcv_wayland_login.jinja2', base_os)
    for path in ('/usr/local/libexec', '/usr/share/gdm', '/etc'):
        text = text.replace(path, str(tmp_path) + path)
    (tmp_path / 'etc/pam.d').mkdir(parents=True)
    (tmp_path / 'etc/systemd/system').mkdir(parents=True)
    stubs = 'package_transaction() { :; }; apt() { :; }; systemctl() { [[ "$1" != is-active ]]; };\n'
    snapshots = []
    for _ in range(2):
        result = subprocess.run(
            ['bash', '-c', stubs + text], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        snapshots.append(
            {
                str(p.relative_to(tmp_path)): (p.read_bytes(), p.stat().st_mode)
                for p in tmp_path.rglob('*')
                if p.is_file()
            }
        )
    assert snapshots[0] == snapshots[1]
    assert 'IDEA_SESSION_OWNER' not in text
    assert 'clusteradmin' not in text
    pam = (tmp_path / 'etc/pam.d/idea-dcv-login').read_text()
    assert pam.index('auth requisite pam_exec.so') < pam.index(
        'auth required pam_permit.so'
    )
    assert 'account required pam_nologin.so' in pam
    assert 'account include ' in pam
    scrub = render('dcv-host-ami-builder/image_scrub.sh.jinja2', base_os)
    assert 'idea-dcv-login' not in scrub


@pytest.mark.parametrize('module_name', ['scheduler', 'virtual-desktop-controller'])
def test_non_desktop_ad_join_has_no_login_mapping(module_name):
    from jinja2 import Template
    from types import SimpleNamespace

    source = (
        Path(IDEA_BOOTSTRAP_DIR) / '_templates/linux/join_activedirectory.jinja2'
    ).read_text()
    source = source[
        source.index('access_provider = ad') : source.index('auth_provider = ad')
    ]
    text = Template(source).render(
        context=SimpleNamespace(
            base_os='rocky10', module_name=module_name, vars=SimpleNamespace()
        )
    )
    assert 'ad_gpo_map_remote_interactive' not in text
