import stat
from unittest.mock import Mock

import pytest
from ideadatamodel import User
from ideaclustermanager.app.accounts.user_home_directory import UserHomeDirectory


def test_home_initialization_uses_account_ids_without_local_user(monkeypatch, tmp_path):
    user = User(username='user', uid=12001, gid=12002, home_dir=str(tmp_path / 'home'))
    helper = UserHomeDirectory(Mock(), user)
    chown = Mock()
    fchown = Mock()
    monkeypatch.setattr('os.chown', chown)
    monkeypatch.setattr('os.fchown', fchown)
    monkeypatch.setattr(
        'pwd.getpwnam', Mock(side_effect=AssertionError('NSS unavailable'))
    )
    helper.initialize()
    assert (tmp_path / 'home/.ssh/id_rsa').is_file()
    for name in ('id_rsa', 'authorized_keys'):
        assert (tmp_path / 'home/.ssh' / name).stat().st_mode & 0o777 == 0o600, name
    calls = chown.call_args_list + fchown.call_args_list
    assert len(calls) > 1
    assert all(call.args[1:] == (12001, 12002) for call in calls)


@pytest.mark.parametrize(
    'uid,gid', [(None, 12002), (12001, None), (0, 12002), (12001, -1)]
)
def test_missing_or_unsafe_ids_fail_before_writing(uid, gid, tmp_path):
    home = tmp_path / 'home'
    helper = UserHomeDirectory(
        Mock(), User(username='user', uid=uid, gid=gid, home_dir=str(home))
    )
    with pytest.raises(Exception, match='positive uid and gid'):
        helper.initialize()
    assert not home.exists()


def test_ppk_conversion_runs_without_su_and_owns_the_result(monkeypatch, tmp_path):
    user = User(username='user', uid=12001, gid=12002, home_dir=str(tmp_path))
    # the key is read as the account, which the test process stands in for
    helper = UserHomeDirectory(key_context(), user)
    (tmp_path / '.ssh').mkdir()
    key = tmp_path / '.ssh/id_rsa'
    key.write_text('PRIVATE KEY\n')
    puttygen = tmp_path / 'puttygen'
    puttygen.write_text('')
    puttygen.chmod(0o755)
    helper._shell = Mock()
    helper._shell.invoke.return_value = Mock(returncode=0, stdout=f'{puttygen}\n')
    fchown = Mock()
    monkeypatch.setattr('os.fchown', fchown)

    def convert(args, **kwargs):
        # puttygen works on a private copy, never in the user's directory
        assert args[0] == str(puttygen) and args[2] == '-o'
        assert (
            not args[1].startswith(str(tmp_path))
            and open(args[1]).read() == 'PRIVATE KEY\n'
        )
        with open(args[3], 'w') as f:
            f.write('converted key\n')
        return Mock(returncode=0)

    monkeypatch.setattr('subprocess.run', convert)
    assert helper.get_key_material('ppk') == 'converted key'
    assert fchown.call_args.args[1:] == (12001, 12002)
    assert key.with_suffix('.ppk').stat().st_mode & 0o777 == 0o600


def ownership_tree(monkeypatch, tmp_path):
    import os

    home = tmp_path / 'home'
    (home / '.ssh').mkdir(parents=True)
    for name in ('authorized_keys', 'id_rsa', 'id_rsa.pub'):
        (home / '.ssh' / name).write_text('existing key')
    (home / 'nested').mkdir()
    (home / 'nested/data').write_text('data')
    (home / 'shared').write_text('other owner')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'data').write_text('outside')
    (home / 'link').symlink_to(outside, target_is_directory=True)
    owners = {str(path): (11001, 11002) for path in [home, *home.rglob('*')]}
    owners[str(home / 'shared')] = (13001, 13002)
    changes = []
    real_lstat, real_stat, real_fstat = os.lstat, os.stat, os.fstat

    # owners are faked by inode, so a lookup by name, by descriptor or by path agrees
    def path_of(ino):
        for path in owners:
            try:
                if real_lstat(path).st_ino == ino:
                    return path
            except OSError:
                pass

    def fake(info):
        path = path_of(info.st_ino)
        if path not in owners:
            return info
        fields = list(info)
        fields[4], fields[5] = owners[path]
        return os.stat_result(fields)

    def record(path, uid, gid):
        if path is None:  # a file the tree did not start with
            return
        assert path in owners
        changes.append(path)
        owners[path] = (uid, gid)

    def chown(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        assert not follow_symlinks
        info = real_stat(path, dir_fd=dir_fd, follow_symlinks=False)
        record(path_of(info.st_ino), uid, gid)

    monkeypatch.setattr(
        os, 'lstat', lambda path, *a, **k: fake(real_lstat(path, *a, **k))
    )
    monkeypatch.setattr(
        os, 'stat', lambda path, *a, **k: fake(real_stat(path, *a, **k))
    )
    monkeypatch.setattr(os, 'fstat', lambda fd: fake(real_fstat(fd)))
    monkeypatch.setattr(os, 'chown', chown)
    monkeypatch.setattr(
        os,
        'lchown',
        lambda path, uid, gid: record(path_of(real_lstat(path).st_ino), uid, gid),
    )
    monkeypatch.setattr(
        os,
        'fchown',
        lambda fd, uid, gid: record(path_of(real_fstat(fd).st_ino), uid, gid),
    )
    helper = UserHomeDirectory(
        Mock(), User(username='user', uid=12001, gid=12002, home_dir=str(home))
    )
    return helper, owners, changes


@pytest.mark.parametrize('dry_run', [True, False])
def test_tree_repair_selects_stale_uid_without_following_links(
    monkeypatch, tmp_path, dry_run
):
    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    expected = {path for path, owner in owners.items() if owner[0] == 11001}
    result = helper.repair_ownership(dry_run=dry_run)
    assert result.changed == len(expected)
    assert result.skipped == 1
    assert result.failed == 0
    assert set(result.paths) == expected
    assert set(changes) == (set() if dry_run else expected)
    assert owners[str(tmp_path / 'home/shared')] == (13001, 13002)
    assert (tmp_path / 'outside/data').read_text() == 'outside'
    if not dry_run:
        changes.clear()
        result = helper.repair_ownership()
        assert result.changed == 0
        assert changes == []


def test_tree_repair_bounds_preview_and_counts_failures(monkeypatch, tmp_path):
    import os

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    for index in range(30):
        path = tmp_path / 'home' / f'file{index}'
        path.touch()
        owners[str(path)] = (11001, 11002)
    preview = helper.repair_ownership(dry_run=True)
    assert preview.changed == len(owners) - 1
    assert len(preview.paths) == 20
    assert changes == []
    monkeypatch.setattr(os, 'chown', Mock(side_effect=PermissionError()))
    monkeypatch.setattr(os, 'fchown', Mock(side_effect=PermissionError()))
    result = helper.repair_ownership()
    assert result.changed == 0
    assert result.failed == len(owners) - 2
    assert result.skipped == 2
    assert owners[helper.home_dir] == (11001, 11002)


def test_existing_ssh_keys_converge_without_regeneration(monkeypatch, tmp_path):
    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    helper.initialize_ssh_dir()
    for name, mode in [
        ('authorized_keys', 0o600),
        ('id_rsa', 0o600),
        ('id_rsa.pub', 0o644),
    ]:
        path = tmp_path / 'home/.ssh' / name
        assert owners[str(path)] == (12001, 12002)
        assert path.stat().st_mode & 0o777 == mode
        assert path.read_text() == 'existing key'


@pytest.mark.parametrize('planted', ['hard link', 'another owner'])
def test_login_convergence_leaves_a_key_file_that_is_not_the_users_alone(
    monkeypatch, tmp_path, planted
):
    import os

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    key = tmp_path / 'home/.ssh/id_rsa.pub'
    elsewhere = tmp_path / 'outside/data'
    if planted == 'hard link':
        key.unlink()
        os.link(elsewhere, key)
        elsewhere.chmod(0o600)
    else:
        owners[str(key)] = (13001, 13002)
        key.chmod(0o600)
    result = helper.converge_login_paths()
    assert str(key) not in changes
    assert key.stat().st_mode & 0o777 == 0o600
    # the user's own login paths still converge
    assert str(tmp_path / 'home/.ssh/id_rsa') in changes
    assert result.failed == 0
    if planted == 'another owner':
        assert owners[str(key)] == (13001, 13002)


@pytest.mark.parametrize('link_name', ['.ssh', '.ssh/id_rsa'])
def test_login_convergence_does_not_follow_links(monkeypatch, tmp_path, link_name):
    import shutil

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    link = tmp_path / 'home' / link_name
    if link.is_dir():
        shutil.rmtree(link)
    else:
        link.unlink()
    target = tmp_path / 'outside'
    link.symlink_to(target, target_is_directory=True)
    before = target.stat()
    helper.converge_login_paths()
    assert target.stat().st_mode == before.st_mode
    assert not any(path.startswith(str(link) + '/') for path in changes)


def test_repair_task_uses_snapshot_and_reports_failures(monkeypatch, tmp_path):
    from ideaclustermanager.app.accounts.account_tasks import RepairHomeOwnershipTask

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    owners[helper.home_dir] = (12001, 12002)
    context = Mock()
    context.accounts.get_user.return_value = helper.user
    task = RepairHomeOwnershipTask(context)
    assert task.get_name() == 'accounts.repair-home-ownership'
    assert task.entity_ref({'username': 'user'}) == ('user', 'user')
    task.invoke({'username': 'user', 'dry_run': False, 'stale_uid': 11001})
    assert str(tmp_path / 'home/nested/data') in changes
    assert str(tmp_path / 'home/shared') not in changes
    owners[str(tmp_path / 'home/nested/data')] = (11001, 11002)
    monkeypatch.setattr('os.chown', Mock(side_effect=PermissionError()))
    monkeypatch.setattr('os.fchown', Mock(side_effect=PermissionError()))
    with pytest.raises(Exception, match='ownership repair failed'):
        task.invoke({'username': 'user', 'dry_run': False, 'stale_uid': 11001})


@pytest.mark.parametrize('home_dir', [None, '', '/', 'relative/home'])
def test_home_initialization_rejects_unsafe_paths(home_dir):
    helper = UserHomeDirectory(
        Mock(), User(username='user', uid=12001, gid=12002, home_dir=home_dir)
    )
    helper.initialize_home_dir = Mock()
    helper.initialize_ssh_dir = Mock()
    with pytest.raises(Exception, match='absolute non-root path'):
        helper.initialize()
    helper.initialize_home_dir.assert_not_called()
    helper.initialize_ssh_dir.assert_not_called()


def test_new_public_key_mode_ignores_restrictive_umask(monkeypatch, tmp_path):
    import os

    helper = UserHomeDirectory(
        Mock(), User(username='user', uid=12001, gid=12002, home_dir=str(tmp_path))
    )
    monkeypatch.setattr(os, 'chown', Mock())
    monkeypatch.setattr(os, 'fchown', Mock())
    previous = os.umask(0o077)
    try:
        helper.initialize_ssh_dir()
    finally:
        os.umask(previous)
    assert (tmp_path / '.ssh/id_rsa.pub').stat().st_mode & 0o777 == 0o644


def test_repair_task_dry_run_and_summary(monkeypatch, tmp_path):
    from ideaclustermanager.app.accounts.account_tasks import RepairHomeOwnershipTask

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    context = Mock()
    context.accounts.get_user.return_value = helper.user
    # the record holds no recorded old owner
    context.accounts.user_dao.get_user.return_value = {'username': 'user'}
    result = RepairHomeOwnershipTask(context).invoke(
        {'username': 'user', 'dry_run': True}
    )
    assert result.changed == len(owners) - 1
    assert changes == []
    context.logger.return_value.info.assert_called_once()
    summary = context.logger.return_value.info.call_args.args[0]
    assert 'dry_run=True' in summary
    assert f'changed={result.changed}' in summary
    assert 'skipped=1 failed=0' in summary


@pytest.mark.parametrize('operation', ['stat', 'scandir'])
def test_tree_read_failures_are_counted(monkeypatch, tmp_path, operation):
    import os

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    original = getattr(os, operation)
    blocked = os.lstat(tmp_path / 'home/nested').st_ino

    def fail_path(path, *args, **kwargs):
        # the walk names an entry inside its folder, and lists a folder by descriptor
        if path == 'nested' or (
            isinstance(path, int) and os.fstat(path).st_ino == blocked
        ):
            raise PermissionError()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, operation, fail_path)
    result = helper.repair_ownership()
    assert result.failed == 1
    assert result.changed == 5
    assert result.skipped == 2
    assert owners[helper.home_dir] == (11001, 11002)
    assert owners[str(tmp_path / 'home/nested/data')] == (11001, 11002)


ROOT = __import__('os').geteuid() == 0


def key_user(home):
    import os
    from ideadatamodel import Group

    # the key is read in the account's own identity worker: the test process is that
    # account, or under root (Linux CI) nobody, which then owns the test home
    uid = 65534 if ROOT else os.getuid()
    gid = 65534 if ROOT else os.getgid()
    gids = [gid] if ROOT else sorted(set(os.getgroups()) | {gid})
    return User(
        username='user',
        uid=uid,
        gid=gid,
        home_dir=str(home),
        additional_groups=[str(g) for g in gids],
    ), {str(g): Group(name=str(g), gid=g) for g in gids}


@pytest.fixture(autouse=True)
def the_account_owns_its_test_home(monkeypatch):
    """under root, files the test wrote are given to the account before it reads them"""
    import os
    from ideaclustermanager.app.accounts import user_home_directory

    if not ROOT:
        return
    real = user_home_directory.UserIdentity.run

    def run(identity, operation, path, *args, **kwargs):
        home = os.path.dirname(os.path.dirname(path))
        for root, dirs, files in os.walk(home):
            for name in dirs + files:
                os.lchown(os.path.join(root, name), identity.uid, identity.gid)
        os.lchown(home, identity.uid, identity.gid)
        # the account must be able to reach its home through the test's folders
        folder = os.path.dirname(home)
        while folder not in ('/', ''):
            os.chmod(folder, os.stat(folder).st_mode | 0o111)
            folder = os.path.dirname(folder)
        return real(identity, operation, path, *args, **kwargs)

    monkeypatch.setattr(user_home_directory.UserIdentity, 'run', run)


def key_context():
    import tempfile

    user, groups = key_user(tempfile.gettempdir())
    context = Mock()
    context.accounts.get_user.side_effect = lambda _: user
    context.accounts.get_group.side_effect = groups.__getitem__
    return context


def _key_home(monkeypatch, tmp_path):

    home = tmp_path / 'home'
    (home / '.ssh').mkdir(parents=True)
    helper = UserHomeDirectory(key_context(), key_user(home)[0])
    helper._shell = Mock()
    helper._shell.invoke.return_value = Mock(returncode=1, stdout='')
    return helper, home


@pytest.mark.parametrize('live', [False, True])
def test_a_planted_ppk_link_is_never_written_through(monkeypatch, tmp_path, live):
    # root must not create or overwrite the link's target and hand it to the user
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    helper, home = _key_home(monkeypatch, tmp_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (home / '.ssh/id_rsa').write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    target = tmp_path / 'system-file'
    if live:
        target.write_text('system')
    (home / '.ssh/id_rsa.ppk').symlink_to(target)
    with pytest.raises(Exception):
        helper.get_key_material('ppk')
    assert target.read_text() == 'system' if live else not target.exists()
    assert (home / '.ssh/id_rsa.ppk').is_symlink()


def test_a_ppk_is_written_for_the_user_and_an_id_rsa_link_is_not_read(
    monkeypatch, tmp_path
):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    helper, home = _key_home(monkeypatch, tmp_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (home / '.ssh/id_rsa').write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    assert helper.get_key_material('ppk').startswith('PuTTY-User-Key-File-2: ssh-rsa')
    assert (home / '.ssh/id_rsa.ppk').stat().st_mode & 0o777 == 0o600
    assert [p.name for p in (home / '.ssh').iterdir() if p.name.startswith('.')] == []
    # the key download reads as root: a link to another file is refused, not returned
    secret = tmp_path / 'secret'
    secret.write_text("not the user's")
    (home / '.ssh/id_rsa').unlink()
    (home / '.ssh/id_rsa').symlink_to(secret)
    with pytest.raises(Exception, match='cannot be read'):
        helper.get_key_material('pem')


def test_home_setup_does_not_write_through_a_planted_dotfile_link(tmp_path):
    import os

    if os.geteuid() != 0 or not os.path.isfile('/etc/skel/.bashrc'):
        pytest.skip('needs root and /etc/skel/.bashrc')
    home = tmp_path / 'home'
    home.mkdir()
    target = tmp_path / 'planted'
    (home / '.bashrc').symlink_to(target)
    helper = UserHomeDirectory(
        Mock(), User(username='user', uid=12001, gid=12002, home_dir=str(home))
    )
    helper.initialize_home_dir()
    assert not target.exists()
    assert (home / '.bashrc').is_symlink()
    logout = home / '.bash_logout'
    if os.path.isfile('/etc/skel/.bash_logout'):
        assert not logout.is_symlink() and os.stat(logout).st_uid == 12001


def touched_inodes(monkeypatch):
    """the inodes every chown reaches, whatever form of the call is used"""
    import os

    touched = []
    real = {name: getattr(os, name) for name in ('chown', 'lchown', 'fchown')}
    real_lstat, real_stat, real_fstat = os.lstat, os.stat, os.fstat

    def chown(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        touched.append(
            real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks).st_ino
        )
        return real['chown'](
            path, uid, gid, dir_fd=dir_fd, follow_symlinks=follow_symlinks
        )

    def lchown(path, uid, gid):
        touched.append(real_lstat(path).st_ino)
        return real['lchown'](path, uid, gid)

    def fchown(fd, uid, gid):
        touched.append(real_fstat(fd).st_ino)
        return real['fchown'](fd, uid, gid)

    monkeypatch.setattr(os, 'chown', chown)
    monkeypatch.setattr(os, 'lchown', lchown)
    monkeypatch.setattr(os, 'fchown', fchown)
    return touched


def swap_for_link(folder, target):
    import os

    os.rename(folder, str(folder) + '.moved')
    os.symlink(target, folder)


def test_tree_repair_does_not_follow_a_folder_swapped_during_the_walk(
    monkeypatch, tmp_path
):
    import os

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    nested = tmp_path / 'home/nested'
    outside_data = tmp_path / 'outside/data'
    owners[str(outside_data)] = (11001, 11002)
    outside_ino = os.lstat(outside_data).st_ino
    nested_ino = os.lstat(nested).st_ino
    touched = touched_inodes(monkeypatch)
    scandir = os.scandir
    swapped = []

    def swap_then_list(path):
        # checked as a folder, it becomes a link just before it is listed
        listing_nested = (
            os.fstat(path).st_ino == nested_ino
            if isinstance(path, int)
            else str(path) == str(nested)
        )
        if listing_nested and not swapped:
            swapped.append(True)
            swap_for_link(nested, tmp_path / 'outside')
        return scandir(path)

    monkeypatch.setattr(os, 'scandir', swap_then_list)
    helper.repair_ownership()
    assert swapped
    assert outside_ino not in touched


def test_login_convergence_does_not_follow_a_folder_swapped_after_its_check(
    monkeypatch, tmp_path
):
    import os

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    ssh = tmp_path / 'home/.ssh'
    victim = tmp_path / 'victim'
    victim.mkdir()
    (victim / 'authorized_keys').write_text('victim key')
    owners[str(victim / 'authorized_keys')] = (13001, 13002)
    victim_ino = os.lstat(victim / 'authorized_keys').st_ino
    touched = touched_inodes(monkeypatch)
    swapped = []
    looked = {'lstat': os.lstat, 'stat': os.stat}

    def check_then_swap(name):
        def call(path, *args, **kwargs):
            info = looked[name](path, *args, **kwargs)
            if str(path) in (str(ssh), '.ssh') and not swapped:
                swapped.append(True)
                swap_for_link(ssh, victim)
            return info

        return call

    for name in looked:
        monkeypatch.setattr(os, name, check_then_swap(name))
    helper.converge_login_paths()
    assert swapped
    assert victim_ino not in touched


def test_a_key_download_refuses_a_hard_linked_key(monkeypatch, tmp_path):
    import os

    helper, home = _key_home(monkeypatch, tmp_path)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.write_text("not the user's")
    os.link(elsewhere, home / '.ssh/id_rsa')
    with pytest.raises(Exception, match='more than one link'):
        helper.get_key_material('pem')


def test_a_key_download_reads_as_the_account_and_refuses_a_foreign_key(
    monkeypatch, tmp_path
):
    import os
    from ideaclustermanager.app.accounts import user_home_directory

    helper, home = _key_home(monkeypatch, tmp_path)
    (home / '.ssh/id_rsa').write_text('marker key\n')
    ran = []
    real_run = user_home_directory.UserIdentity.run

    def run(identity, operation, *args, **kwargs):
        ran.append((identity.uid, identity.gid))
        return real_run(identity, operation, *args, **kwargs)

    monkeypatch.setattr(user_home_directory.UserIdentity, 'run', run)
    assert helper.get_key_material('pem') == 'marker key'
    assert ran == [(helper.user.uid, helper.user.gid)]
    # a single-link regular file another account owns is not this account's key
    real_fstat = os.fstat

    def foreign(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            values = list(info)
            values[stat.ST_UID] = os.getuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, 'fstat', foreign)
    with pytest.raises(Exception, match='not owned by the account'):
        helper.get_key_material('pem')


def test_a_key_download_refuses_a_file_too_large_to_be_a_key(monkeypatch, tmp_path):
    helper, home = _key_home(monkeypatch, tmp_path)
    with open(home / '.ssh/id_rsa', 'wb') as f:
        f.truncate(helper.KEY_FILE_LIMIT + 1)
    with pytest.raises(Exception, match='too large to be a key'):
        helper.get_key_material('pem')


@pytest.mark.parametrize('enabled', [False, None])
def test_a_disabled_account_cannot_download_its_key(enabled):
    from ideadatamodel import GetUserPrivateKeyRequest
    from ideadatamodel import exceptions as soca_exceptions
    from ideaclustermanager.app.api.auth_api import AuthAPI

    invocation = Mock()
    invocation.namespace = 'Auth.GetUserPrivateKey'
    invocation.is_authorized_user.return_value = True
    invocation.get_username.return_value = 'user'
    invocation.get_request_payload_as.return_value = GetUserPrivateKeyRequest(
        key_format='pem', platform='linux'
    )
    api = AuthAPI.__new__(AuthAPI)
    api.context = Mock()
    api.context.accounts.get_user.return_value = User(
        username='user', uid=7001, gid=7001, home_dir='/marker', enabled=enabled
    )
    with pytest.raises(soca_exceptions.SocaException) as refused:
        api.invoke(invocation)
    assert refused.value.error_code == 'UNAUTHORIZED_ACCESS'
    invocation.success.assert_not_called()


def test_a_login_file_swapped_after_its_check_is_not_the_one_changed(
    monkeypatch, tmp_path
):
    import os

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    ssh = tmp_path / 'home/.ssh'
    foreign = tmp_path / 'foreign'
    foreign.write_text("not the user's")
    owners[str(foreign)] = (13001, 13002)
    foreign_ino = os.lstat(foreign).st_ino
    touched = touched_inodes(monkeypatch)
    looked = os.stat
    swapped = []

    def check_then_swap(path, *args, **kwargs):
        info = looked(path, *args, **kwargs)
        if str(path) == 'id_rsa' and not swapped:
            # after the name was checked, a foreign-owned file takes its place
            swapped.append(True)
            os.rename(foreign, ssh / 'id_rsa')
        return info

    monkeypatch.setattr(os, 'stat', check_then_swap)
    helper.converge_login_paths()
    assert swapped
    assert foreign_ino not in touched
    assert owners[str(foreign)] == (13001, 13002)


def test_the_tree_repair_leaves_a_file_with_a_second_name(monkeypatch, tmp_path):
    import os

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    data = tmp_path / 'home/nested/data'
    os.link(data, tmp_path / 'home/nested/second')
    owners[str(tmp_path / 'home/nested/second')] = owners[str(data)]
    helper.repair_ownership(stale_uid=11001)
    assert str(data) not in changes


def test_a_new_key_set_writes_the_private_key_last(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    helper = UserHomeDirectory(Mock(), key_user(home)[0])
    written = []
    monkeypatch.setattr(
        helper, '_write_user_file', lambda path, data, mode: written.append(path)
    )
    monkeypatch.setattr('os.fchown', lambda *a: None)
    helper.initialize_ssh_dir()
    assert [p.rsplit('/', 1)[-1] for p in written] == [
        'authorized_keys',
        'id_rsa.pub',
        'id_rsa',
    ]


def test_a_home_too_large_for_one_budget_is_repaired_over_attempts(
    monkeypatch, tmp_path
):
    import os
    from ideaclustermanager.app.accounts import user_home_directory
    from ideaclustermanager.app.accounts.account_tasks import RepairHomeOwnershipTask

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    for n in range(12):
        path = tmp_path / f'home/nested/file{n}'
        path.write_text('data')
        owners[str(path)] = (11001, 11002)
    # each ownership change takes a fifth of the budget, and a visit that changes
    # nothing almost none: a pass covers only part of the tree
    clock = [0.0, 0]

    def monotonic():
        clock[0] += (len(changes) - clock[1]) * helper.REPAIR_BUDGET_SECONDS / 5
        clock[1] = len(changes)
        return clock[0]

    monkeypatch.setattr(user_home_directory.time, 'monotonic', monotonic)
    monkeypatch.setattr(os, '_exit', Mock(side_effect=AssertionError('process ended')))
    context = Mock()
    context.accounts.get_user.return_value = helper.user
    context.accounts.user_dao.get_user.return_value = {'username': 'user'}
    attempts = 0
    while True:
        attempts += 1
        assert attempts < 20
        try:
            RepairHomeOwnershipTask(context).invoke(
                {'username': 'user', 'stale_uid': 11001}
            )
            break
        except Exception as error:
            assert 'repair failed' in str(error)
    assert attempts > 1
    assert all(
        owner == (12001, 12002)
        for path, owner in owners.items()
        if not path.endswith('shared')
    )


def archive_home(tmp_path, monkeypatch, make):
    import os

    base = tmp_path / 'homes'
    base.mkdir()
    home = base / 'user'
    make(home)
    owners = []
    # the archive is given to root; a test not run as root records it instead
    if os.geteuid() != 0:
        monkeypatch.setattr(
            os, 'fchown', lambda fd, uid, gid: owners.append((uid, gid))
        )
    helper = UserHomeDirectory(
        Mock(), User(username='user', uid=12001, gid=12002, home_dir=str(home))
    )
    return helper, base, home, owners


def test_a_deleted_accounts_home_is_renamed_aside_root_only(monkeypatch, tmp_path):
    import os

    helper, base, home, owners = archive_home(
        tmp_path,
        monkeypatch,
        lambda home: (home.mkdir(), (home / 'data').write_text('x')),
    )
    assert helper.archive() == 'archived'
    assert not home.exists()
    [archived] = [p for p in base.iterdir()]
    assert (archived / 'data').read_text() == 'x'
    assert os.stat(archived).st_mode & 0o777 == 0o700
    if os.geteuid() != 0:
        assert owners == [(0, 0)]


def test_a_linked_home_is_left_where_it_is_and_its_target_untouched(
    monkeypatch, tmp_path
):
    import os

    target = tmp_path / 'shared-volume'
    target.mkdir()
    os.chmod(target, 0o755)
    helper, base, home, owners = archive_home(
        tmp_path, monkeypatch, lambda home: home.symlink_to(target)
    )
    assert helper.archive() == 'left in place'
    assert home.is_symlink()
    assert os.stat(target).st_mode & 0o777 == 0o755 and owners == []


def test_a_home_on_its_own_mount_is_left_where_it_is(monkeypatch, tmp_path):
    import os

    helper, base, home, owners = archive_home(
        tmp_path, monkeypatch, lambda home: home.mkdir()
    )
    real = os.lstat

    def mounted(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        if os.path.normpath(str(path)) == str(home):
            values = list(info)
            values[2] = info.st_dev + 1  # st_dev
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, 'lstat', mounted)
    assert helper.archive() == 'left in place'
    assert home.is_dir() and owners == []


def test_a_session_from_before_the_name_was_reused_downloads_no_key(monkeypatch):
    from decimal import Decimal

    from ideaclustermanager.app.api import auth_api

    # refused before the home is read at all
    monkeypatch.setattr(
        auth_api, 'UserHomeDirectory', Mock(side_effect=AssertionError('home read'))
    )

    from ideadatamodel import GetUserPrivateKeyRequest
    from ideadatamodel import exceptions as soca_exceptions
    from ideaclustermanager.app.accounts.accounts_service import AccountsService
    from ideaclustermanager.app.api.auth_api import AuthAPI

    invocation = Mock()
    invocation.namespace = 'Auth.GetUserPrivateKey'
    invocation.is_authorized_user.return_value = True
    invocation.get_username.return_value = 'user'
    invocation.get_authorization.return_value = Mock(token_id=None)
    invocation.get_decoded_token.return_value = {'auth_time': 1_700_000_000}
    invocation.get_request_payload_as.return_value = GetUserPrivateKeyRequest(
        key_format='pem', platform='linux'
    )
    accounts = AccountsService.__new__(AccountsService)
    accounts.user_dao = Mock()
    accounts.user_dao.get_user.return_value = {
        'username': 'user',
        'created_on': Decimal(1_800_000_000_000),
    }
    accounts.get_user = Mock(
        return_value=User(
            username='user', uid=7001, gid=7001, home_dir='/x', enabled=True
        )
    )
    api = AuthAPI.__new__(AuthAPI)
    api.context = Mock()
    api.context.accounts = accounts
    with pytest.raises(soca_exceptions.SocaException) as refused:
        api.invoke(invocation)
    assert refused.value.error_code == 'UNAUTHORIZED_ACCESS'
    invocation.success.assert_not_called()
