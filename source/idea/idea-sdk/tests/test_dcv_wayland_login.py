"""Wayland login must stay on the broker-authorized owner's remote GDM display."""

import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace

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
def login(monkeypatch, tmp_path):
    # the helper ships as a .jinja2 template, so name its loader explicitly
    spec = importlib.util.spec_from_loader(
        'dcv_wayland_login',
        importlib.machinery.SourceFileLoader('dcv_wayland_login', str(SCRIPT)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'FIRST_LOGIN', tmp_path / 'first-login', raising=False)
    accounts = {
        42: SimpleNamespace(pw_name='gdm', pw_uid=42, pw_gid=43),
        60578: SimpleNamespace(pw_name='gdm-greeter', pw_uid=60578, pw_gid=43),
        60579: SimpleNamespace(pw_name='gdm-greeter-2', pw_uid=60579, pw_gid=43),
        99: SimpleNamespace(pw_name='mallory', pw_uid=99, pw_gid=43),
    }

    def getpwnam(name):
        for account in accounts.values():
            if account.pw_name == name:
                return account
        raise KeyError(name)

    monkeypatch.setattr(module.pwd, 'getpwuid', accounts.__getitem__)
    monkeypatch.setattr(module.pwd, 'getpwnam', getpwnam)
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
    def caller_session(self, pid, uid):
        return {'Id': 'c2'}

    def owner(self, session_id, uid):
        assert (session_id, uid) == ('c2', 42)
        return ('alice', 'test-session', 'today')


class DynamicSystem:
    def __init__(self, login, uid=60578):
        self.login = login
        self.sessions, self.logins, self.displays = fixture_state()
        self.logins[0].update(User=uid, Name='gdm-greeter')
        self.logins.append(
            dict(
                self.logins[0],
                Id='c3',
                User=60579,
                Name='gdm-greeter-2',
                Seat='seat0',
                Remote=False,
            )
        )

    def caller_session(self, pid, uid):
        return self.logins[pid - 100]

    def owner(self, session_id, uid):
        return self.login.authorized_owner(
            session_id, uid, self.sessions, self.logins, self.displays
        )


@pytest.mark.parametrize('gdm_name', ['gdm', 'Debian-gdm'])
@pytest.mark.parametrize('greeter_name', ['gdm-greeter', 'gdm-greeter-3'])
def test_dynamic_greeter_can_issue_and_redeem_for_own_remote_session(
    login, monkeypatch, gdm_name, greeter_name
):
    login.pwd.getpwuid(42).pw_name = gdm_name
    login.pwd.getpwuid(60578).pw_name = greeter_name
    monkeypatch.setattr(login, 'pam_worker', lambda pid: pid == 200)
    grants = login.Grants(DynamicSystem(login))
    issued = grants.handle(100, 60578, {'op': 'issue'})
    assert issued['owner'] == 'alice'
    redeem = dict(op='redeem', user='alice', remote='0.0.0.0', token=issued['token'])
    assert grants.handle(200, 0, redeem) == {'ok': True}
    with pytest.raises(ValueError):
        grants.handle(200, 0, redeem)


def test_dynamic_seat0_greeter_cannot_request_remote_owner(login):
    system = DynamicSystem(login)
    with pytest.raises(ValueError):
        login.Grants(system).handle(101, 60579, {'op': 'issue'})
    with pytest.raises(ValueError):
        system.owner('c3', 60579)
    with pytest.raises(ValueError):
        system.owner('c2', 60579)


@pytest.mark.parametrize(
    'name,gid',
    [
        ('mallory', 43),
        ('gdm-greeter-extra', 43),
        ('gdm-greeter-2-extra', 43),
        ('gdm-greeter', 44),
        ('gdm-greeter-3', 44),
    ],
)
def test_gdm_group_member_is_not_a_greeter(login, name, gid):
    login.pwd.getpwuid(99).pw_name = name
    login.pwd.getpwuid(99).pw_gid = gid
    system = DynamicSystem(login, uid=99)
    with pytest.raises(ValueError):
        login.Grants(system).handle(100, 99, {'op': 'issue'})
    with pytest.raises(ValueError):
        system.owner('c2', 99)


@pytest.mark.parametrize('gdm_name', ['gdm', 'Debian-gdm'])
def test_static_gdm_account_can_request_owner(login, gdm_name):
    login.pwd.getpwuid(42).pw_name = gdm_name
    issued = login.Grants(System()).handle(100, 42, {'op': 'issue'})
    assert issued['owner'] == 'alice'
    assert login.authorized_owner('c2', 42, *fixture_state())[0] == 'alice'


@pytest.mark.parametrize('uid,expected', [(60578, 'c2'), (60579, 'c3')])
def test_dynamic_user_service_resolves_only_its_own_greeter(login, uid, expected):
    state = DynamicSystem(login)
    manager = dict(state.logins[0], Id='manager', Class='manager-early', Remote=False)
    sessions = state.logins + [manager]
    system = object.__new__(login.System)
    system.call = lambda *args: (
        [(s['Id'], s['User'], s['Name'], s['Seat'], s['Id']) for s in sessions],
    )
    system.properties = lambda path: next(s for s in sessions if s['Id'] == path)
    assert system.caller_session(100, uid)['Id'] == expected


@pytest.mark.parametrize('count', [0, 2])
def test_dynamic_user_service_rejects_missing_or_ambiguous_greeter(login, count):
    system = object.__new__(login.System)
    system.call = lambda *args: (
        [('c2', 60578, 'gdm-greeter', '', '/session')] * count,
    )
    system.properties = lambda path: dict(fixture_state()[1][0], User=60578)
    with pytest.raises(ValueError):
        system.caller_session(100, 60578)


@pytest.mark.parametrize('uid', [42, 99])
def test_static_or_untrusted_uid_cannot_select_a_session_by_uid(login, uid):
    system = object.__new__(login.System)

    def call(name, path, interface, method, signature, args):
        assert method == 'GetSessionByPID'
        assert args == (100,)
        return ('/session',)

    system.call = call
    system.properties = lambda path: fixture_state()[1][0]
    assert system.caller_session(100, uid)['Id'] == 'c2'


@pytest.mark.parametrize('change', ['uid', 'gid'])
def test_dynamic_greeter_identity_is_rechecked_at_redeem(login, monkeypatch, change):
    monkeypatch.setattr(login, 'pam_worker', lambda pid: pid == 200)
    system = DynamicSystem(login)
    grants = login.Grants(system)
    issued = grants.handle(100, 60578, {'op': 'issue'})
    if change == 'uid':
        system.logins[0]['User'] = 60579
    else:
        login.pwd.getpwuid(60578).pw_gid = 44
    with pytest.raises(ValueError):
        grants.handle(
            200,
            0,
            dict(op='redeem', user='alice', remote='0.0.0.0', token=issued['token']),
        )


def test_credentials_are_one_use_and_bound_to_owner(login, monkeypatch):
    monkeypatch.setattr(login, 'pam_worker', lambda pid: pid == 200)
    grants = login.Grants(System())
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
    grants = login.Grants(system)
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
    assert not login.FIRST_LOGIN.exists()


@pytest.mark.parametrize('uid', [0, 1000])
def test_only_gdm_can_request_an_owner_credential(login, uid):
    with pytest.raises(ValueError):
        login.Grants(System()).handle(100, uid, {'op': 'issue'})


def test_first_login_privilege_is_consumed_at_validated_pam_redemption(
    login, monkeypatch
):
    monkeypatch.setattr(login, 'pam_worker', lambda pid: 123 if pid == 200 else None)
    grants = login.Grants(System())
    issued = grants.handle(100, 42, {'op': 'issue'})
    pam = {'user': 'alice', 'remote': '0.0.0.0', 'token': issued['token']}
    assert grants.handle(200, 0, dict(pam, op='redeem')) == {'ok': True}
    assert login.FIRST_LOGIN.stat().st_mode & 0o777 == 0o600
    # Recreating both service and DCV session must not grant a second automatic login.
    system = System()
    system.owner = lambda *args: ('alice', 'new-session', 'tomorrow')
    with pytest.raises(ValueError, match='first login'):
        login.Grants(system).handle(100, 42, {'op': 'issue'})


def test_outstanding_credential_cannot_bypass_completed_first_login(login, monkeypatch):
    monkeypatch.setattr(login, 'pam_worker', lambda pid: 123)
    grants = login.Grants(System())
    issued = grants.handle(100, 42, {'op': 'issue'})
    login.FIRST_LOGIN.write_text('completed')
    with pytest.raises(ValueError, match='first login'):
        grants.handle(
            200,
            0,
            dict(op='redeem', user='alice', remote='remote', token=issued['token']),
        )


def test_first_login_fails_closed_if_state_cannot_be_persisted(
    login, monkeypatch, tmp_path
):
    monkeypatch.setattr(login, 'pam_worker', lambda pid: pid == 200)
    monkeypatch.setattr(login, 'FIRST_LOGIN', tmp_path / 'missing' / 'marker')
    grants = login.Grants(System())
    issued = grants.handle(100, 42, {'op': 'issue'})
    with pytest.raises(OSError):
        grants.handle(
            200,
            0,
            dict(op='redeem', user='alice', remote='remote', token=issued['token']),
        )


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
    assert 'idea-dcv-login' not in render(
        'dcv-host-ami-builder/image_scrub.sh.jinja2', base_os
    )


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
    user_service = tmp_path / 'etc/systemd/user/idea-dcv-login.service'
    assert user_service.exists() == (base_os == 'ubuntu2604')
    if user_service.exists():
        assert (
            'ConditionEnvironment=XDG_SESSION_CLASS=greeter' in user_service.read_text()
        )
        assert (
            'ExecStart=/usr/local/libexec/idea-dcv-login login'.replace(
                '/usr/local/libexec', str(tmp_path) + '/usr/local/libexec'
            )
            in user_service.read_text()
        )
        wants = (
            tmp_path
            / 'etc/systemd/user/gnome-session@gnome-login.target.wants/idea-dcv-login.service'
        )
        assert wants.resolve() == user_service
    scrub = render('dcv-host-ami-builder/image_scrub.sh.jinja2', base_os)
    assert 'rm -rf /var/lib/idea-dcv-login' in scrub
    assert 'StateDirectoryMode=0700' in text


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
