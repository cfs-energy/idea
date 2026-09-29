from unittest.mock import Mock

import pytest

from ideaclustermanager.app.accounts.db.user_dao import UserDAO


@pytest.mark.parametrize('exceptions', [None, [], ['c5.large']])
def test_instance_type_exceptions_round_trip(exceptions):
    dao = UserDAO(Mock(), Mock())
    row = {'username': 'user-a'}
    if exceptions is not None:
        row['instance_type_exceptions'] = exceptions
    user = dao.convert_from_db(row)
    assert user.instance_type_exceptions == exceptions
    assert dao.convert_to_db(user) == row
