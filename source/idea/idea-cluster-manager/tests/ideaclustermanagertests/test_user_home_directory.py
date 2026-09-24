from unittest.mock import Mock

import pytest
from ideadatamodel import User
from ideaclustermanager.app.accounts.user_home_directory import UserHomeDirectory


def test_home_initialization_uses_account_ids_without_local_user(monkeypatch, tmp_path):
    user = User(username='user', uid=12001, gid=12002, home_dir=str(tmp_path / 'home'))
    helper = UserHomeDirectory(Mock(), user)
    chown = Mock()
    monkeypatch.setattr('os.chown', chown)
    monkeypatch.setattr(
        'pwd.getpwnam', Mock(side_effect=AssertionError('NSS unavailable'))
    )
    monkeypatch.setattr('shutil.copy', Mock())
    helper.initialize()
    assert (tmp_path / 'home/.ssh/id_rsa').is_file()
    assert chown.call_count > 1
    assert all(call.args[1:] == (12001, 12002) for call in chown.call_args_list)


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
    chown = Mock()
    monkeypatch.setattr('os.chown', chown)

    def convert(args, **kwargs):
        assert args == [str(puttygen), str(key), '-o', str(key.with_suffix('.ppk'))]
        key.with_suffix('.ppk').write_text('converted key\n')
        return Mock(returncode=0)

    monkeypatch.setattr('subprocess.run', convert)
    assert helper.get_key_material('ppk') == 'converted key'
    chown.assert_called_once_with(str(key.with_suffix('.ppk')), 12001, 12002)
