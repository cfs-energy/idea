import os

import pytest

from ideaclustermanager import AppContext
from ideadatamodel import Group, User

ROOT = os.geteuid() == 0


@pytest.fixture
def keyed_user(context: AppContext, monkeypatch, tmp_path):
    """an account whose home is a test folder, readable by its identity worker"""
    import tempfile

    accounts = context.accounts
    uid = 65534 if ROOT else os.getuid()
    gid = 65534 if ROOT else os.getgid()
    gids = [gid] if ROOT else sorted(set(os.getgroups()) | {gid})
    base = tempfile.mkdtemp(dir='/tmp')
    os.chmod(base, 0o755)
    home = os.path.join(base, 'home')
    os.makedirs(os.path.join(home, '.ssh'))
    user = accounts.create_user(user=User(username='keyuser9', email='k@example.com'))
    accounts.user_dao.update_user(
        {'username': user.username, 'uid': uid, 'gid': gid, 'home_dir': home}
    )
    groups = {str(g): Group(name=str(g), gid=g) for g in gids}
    real_get_user = accounts.get_user

    def get_user(username):
        found = real_get_user(username)
        if username == user.username:
            found.additional_groups = list(groups)
        return found

    monkeypatch.setattr(accounts, 'get_user', get_user)
    real_get_group = accounts.get_group
    monkeypatch.setattr(
        accounts,
        'get_group',
        lambda name: groups[name] if name in groups else real_get_group(name),
    )

    def write(name, text):
        path = os.path.join(home, '.ssh', name)
        with open(path, 'w') as f:
            f.write(text)
        if ROOT:
            os.chown(path, uid, gid)
        return path

    if ROOT:
        for path in (home, os.path.join(home, '.ssh')):
            os.chown(path, uid, gid)
    yield accounts, user.username, home, write
    accounts.enable_user(user.username)
    accounts.delete_user(user.username)


def keys(home):
    return sorted(os.listdir(os.path.join(home, '.ssh')))


def set_aside(home, name='authorized_keys'):
    """the name now holds an empty read-only placeholder, the keys wait beside it"""
    path = os.path.join(home, '.ssh', name)
    info = os.lstat(path)
    return (
        name + '.disabled' in keys(home)
        and info.st_size == 0
        and info.st_mode & 0o777 == 0o444
        and not os.path.islink(path)
    )


def test_disabling_sets_the_keys_aside_and_enabling_puts_them_back(keyed_user):
    accounts, name, home, write = keyed_user
    write(
        'authorized_keys',
        'ssh-ed25519 AAAA marker-own\nssh-ed25519 AAAA marker-added\n',
    )
    accounts.disable_user(name)
    assert set_aside(home)
    # twice is the same
    accounts.disable_user(name)
    assert set_aside(home)
    accounts.enable_user(name)
    assert keys(home) == ['authorized_keys']
    with open(os.path.join(home, '.ssh/authorized_keys')) as f:
        assert 'marker-added' in f.read()
    accounts.enable_user(name)
    assert keys(home) == ['authorized_keys']


def test_enabling_an_account_that_never_had_keys_changes_nothing(keyed_user):
    accounts, name, home, write = keyed_user
    accounts.disable_user(name)
    accounts.enable_user(name)
    assert keys(home) == []


def test_keys_given_while_disabled_are_kept_and_the_old_stay_aside(keyed_user):
    accounts, name, home, write = keyed_user
    write('authorized_keys', 'ssh-ed25519 AAAA marker-old\n')
    accounts.disable_user(name)
    # an administrator gives new keys while the account is disabled
    os.unlink(os.path.join(home, '.ssh/authorized_keys'))
    write('authorized_keys', 'ssh-ed25519 AAAA marker-new\n')
    accounts.enable_user(name)
    assert keys(home) == ['authorized_keys', 'authorized_keys.disabled']
    with open(os.path.join(home, '.ssh/authorized_keys')) as f:
        assert 'marker-new' in f.read()


def test_a_linked_key_file_is_set_aside_by_name_and_its_target_untouched(
    keyed_user, tmp_path
):
    accounts, name, home, write = keyed_user
    elsewhere = write('kept', 'ssh-ed25519 AAAA marker\n')
    os.link(elsewhere, os.path.join(home, '.ssh/authorized_keys'))
    outside = tmp_path / 'outside-keys'
    outside.write_text('ssh-ed25519 AAAA marker-outside\n')
    os.symlink(outside, os.path.join(home, '.ssh/authorized_keys2'))
    accounts.disable_user(name)
    assert set_aside(home) and set_aside(home, 'authorized_keys2')
    # the names moved; nothing was written through them
    assert os.path.islink(os.path.join(home, '.ssh/authorized_keys2.disabled'))
    assert outside.read_text() == 'ssh-ed25519 AAAA marker-outside\n'
    with open(elsewhere) as f:
        assert f.read() == 'ssh-ed25519 AAAA marker\n'
    accounts.enable_user(name)
    assert os.path.islink(os.path.join(home, '.ssh/authorized_keys2'))
    assert 'authorized_keys.disabled' not in keys(home)


def test_accounts_disabled_before_the_upgrade_have_their_keys_set_aside(keyed_user):
    accounts, name, home, write = keyed_user
    write('authorized_keys', 'ssh-ed25519 AAAA marker\n')
    # disabled by a release that left the keys in place
    accounts.user_dao.update_user({'username': name, 'enabled': False})
    accounts.set_aside_keys_of_disabled_users()
    assert set_aside(home)


def test_an_account_enabled_while_the_start_pass_runs_keeps_its_keys(
    keyed_user, monkeypatch
):
    accounts, name, home, write = keyed_user
    write('authorized_keys', 'ssh-ed25519 AAAA marker\n')
    accounts.user_dao.update_user({'username': name, 'enabled': False})
    real = accounts.user_dao.get_user

    def enabled_meanwhile(username):
        # the reconciler re-enables it after the pass listed it
        accounts.user_dao.update_user({'username': username, 'enabled': True})
        return real(username)

    monkeypatch.setattr(accounts.user_dao, 'get_user', enabled_meanwhile)
    accounts.set_aside_keys_of_disabled_users()
    assert keys(home) == ['authorized_keys']


def test_the_start_pass_stops_at_a_home_that_does_not_answer(keyed_user, monkeypatch):
    accounts, name, home, write = keyed_user
    accounts.user_dao.update_user({'username': name, 'enabled': False})
    tried = []

    def hung(user, aside, timeout=None):
        if aside:
            tried.append((user['username'], timeout))
        return None

    monkeypatch.setattr(accounts, '_ssh_keys', hung)
    monkeypatch.setattr(
        accounts.user_dao.table,
        'scan',
        lambda **_: {
            'Items': [
                {'username': name, 'enabled': False},
                {'username': name, 'enabled': False},
            ]
        },
    )
    accounts.set_aside_keys_of_disabled_users()
    assert tried == [(name, accounts.KEYS_PASS_ACCOUNT_SECONDS)]


def test_a_key_file_root_wrote_for_the_account_is_set_aside(keyed_user, monkeypatch):
    import stat as stat_module

    accounts, name, home, write = keyed_user
    write('authorized_keys', 'ssh-ed25519 AAAA marker\n')
    real_stat = os.stat

    def root_owned(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if str(path) == 'authorized_keys' and stat_module.S_ISREG(info.st_mode):
            values = list(info)
            values[stat_module.ST_UID] = 0
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, 'stat', root_owned)
    accounts.disable_user(name)
    assert set_aside(home)


def test_one_account_that_fails_does_not_stop_the_start_pass(keyed_user, monkeypatch):
    accounts, name, home, write = keyed_user
    write('authorized_keys', 'ssh-ed25519 AAAA marker\n')
    accounts.user_dao.update_user({'username': name, 'enabled': False})
    real = accounts._ssh_keys
    seen = []

    def first_fails(user, aside, timeout=300):
        seen.append(user['username'])
        if len(seen) == 1:
            return 'failed'
        return real(user, aside, timeout)

    monkeypatch.setattr(accounts, '_ssh_keys', first_fails)
    monkeypatch.setattr(
        accounts.user_dao.table,
        'scan',
        lambda **_: {
            'Items': [
                {'username': name, 'enabled': False},
                {'username': name, 'enabled': False},
            ]
        },
    )
    accounts.set_aside_keys_of_disabled_users()
    assert set_aside(home)


def test_keys_move_without_ever_running_as_the_disabled_account(
    keyed_user, monkeypatch
):
    # the file browser's identity helper refuses a disabled account (fix20-cm-server
    # d80e2c68): the key move must not need it, on any of its three paths
    from ideaclustermanager.app.accounts import user_home_directory

    def refused(*args, **kwargs):
        raise AssertionError('ran as the account')

    monkeypatch.setattr(user_home_directory, 'UserIdentity', refused)
    accounts, name, home, write = keyed_user
    write('authorized_keys', 'ssh-ed25519 AAAA marker\n')
    accounts.disable_user(name)
    assert set_aside(home)
    accounts.enable_user(name)
    assert keys(home) == ['authorized_keys']
    accounts.user_dao.update_user({'username': name, 'enabled': False})
    accounts.set_aside_keys_of_disabled_users()
    assert set_aside(home)


def test_a_linked_ssh_folder_is_set_aside_as_a_whole(keyed_user, tmp_path):
    import shutil

    accounts, name, home, write = keyed_user
    elsewhere = tmp_path / 'elsewhere-ssh'
    elsewhere.mkdir()
    (elsewhere / 'authorized_keys').write_text('ssh-ed25519 AAAA marker\n')
    shutil.rmtree(os.path.join(home, '.ssh'))
    os.symlink(elsewhere, os.path.join(home, '.ssh'))
    accounts.disable_user(name)
    ssh = os.path.join(home, '.ssh')
    assert not os.path.islink(ssh) and os.path.isfile(ssh)
    assert os.path.getsize(ssh) == 0
    assert os.path.islink(os.path.join(home, '.ssh.disabled'))
    assert (elsewhere / 'authorized_keys').read_text() == 'ssh-ed25519 AAAA marker\n'
    accounts.enable_user(name)
    assert os.path.islink(ssh)


def test_a_missing_key_file_leaves_nothing_behind(keyed_user):
    accounts, name, home, write = keyed_user
    accounts.disable_user(name)
    assert keys(home) == []
    accounts.enable_user(name)
    assert keys(home) == []


def test_keys_replaced_while_disabled_stay_in_force_after_a_second_disable_and_enable(
    keyed_user,
):
    accounts, name, home, write = keyed_user
    write('authorized_keys', 'ssh-ed25519 AAAA marker-old\n')
    accounts.disable_user(name)
    os.unlink(os.path.join(home, '.ssh/authorized_keys'))
    write('authorized_keys', 'ssh-ed25519 AAAA marker-new\n')
    accounts.enable_user(name)
    accounts.disable_user(name)
    accounts.enable_user(name)
    with open(os.path.join(home, '.ssh/authorized_keys')) as f:
        assert 'marker-new' in f.read()
