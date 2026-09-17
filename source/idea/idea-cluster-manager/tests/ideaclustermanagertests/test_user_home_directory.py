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
