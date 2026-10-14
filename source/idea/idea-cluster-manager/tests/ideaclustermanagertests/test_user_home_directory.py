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
    helper = UserHomeDirectory(Mock(), user)
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
    original_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if str(path) not in owners:
            return info
        fields = list(info)
        fields[4], fields[5] = owners[str(path)]
        return os.stat_result(fields)

    def lchown(path, uid, gid):
        assert str(path) in owners
        changes.append(str(path))
        owners[str(path)] = (uid, gid)

    monkeypatch.setattr(os, 'lstat', lstat)
    monkeypatch.setattr(os, 'lchown', lchown)
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
    monkeypatch.setattr(os, 'lchown', Mock(side_effect=PermissionError()))
    result = helper.repair_ownership()
    assert result.changed == 0
    assert result.failed == len(owners) - 2
    assert result.skipped == 2
    assert owners[helper.home_dir] == (11001, 11002)


def test_existing_ssh_keys_converge_without_regeneration(monkeypatch, tmp_path):
    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    monkeypatch.setattr('os.chown', Mock())
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
    monkeypatch.setattr('os.lchown', Mock(side_effect=PermissionError()))
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


@pytest.mark.parametrize('operation', ['lstat', 'scandir'])
def test_tree_read_failures_are_counted(monkeypatch, tmp_path, operation):
    import os

    helper, owners, changes = ownership_tree(monkeypatch, tmp_path)
    original = getattr(os, operation)
    blocked = str(tmp_path / 'home/nested')

    def fail_path(path, *args, **kwargs):
        if str(path) == blocked:
            raise PermissionError()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, operation, fail_path)
    result = helper.repair_ownership()
    assert result.failed == 1
    assert result.changed == 5
    assert result.skipped == 2
    assert owners[helper.home_dir] == (11001, 11002)
    assert owners[str(tmp_path / 'home/nested/data')] == (11001, 11002)


def _key_home(monkeypatch, tmp_path):
    import os

    home = tmp_path / 'home'
    (home / '.ssh').mkdir(parents=True)
    helper = UserHomeDirectory(
        Mock(),
        User(username='user', uid=os.getuid(), gid=os.getgid(), home_dir=str(home)),
    )
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
