import random
import re
import string

import pytest

from ideasdk.utils import utils as utils_module
from ideasdk.utils import Utils
from ideasdk.utils.utils import PASSWORD_SPECIAL_CHARS

# What accounts_service accepts as a symbol in a user-supplied password (Cognito's symbol set).
COGNITO_SYMBOLS = set('^$*.[]{}()?"!@#%&/\\,><\':;|_~`=+-')
ALLOWED = set(string.ascii_letters + string.digits + PASSWORD_SPECIAL_CHARS)


def classes(password: str) -> tuple:
    return (
        sum(c in string.ascii_uppercase for c in password),
        sum(c in string.ascii_lowercase for c in password),
        sum(c in string.digits for c in password),
        sum(c in PASSWORD_SPECIAL_CHARS for c in password),
    )


@pytest.mark.parametrize(
    'args, length, minimums',
    [
        ((), 8, (1, 1, 1, 1)),  # Windows desktop local password
        ((8, 2, 2, 2, 2), 8, (2, 2, 2, 2)),  # new user's initial password
        ((16, 2, 2, 2, 2), 16, (2, 2, 2, 2)),  # AD service account rotation
        ((4, 2, 2, 2, 2), 8, (2, 2, 2, 2)),  # minimums longer than length win
    ],
)
def test_length_and_class_minimums(args, length, minimums):
    for _ in range(500):
        password = Utils.generate_password(*args)
        assert len(password) == length
        assert set(password) <= ALLOWED
        assert all(have >= need for have, need in zip(classes(password), minimums))


def test_symbols_satisfy_the_pool_policy_and_powershell_quoting():
    assert len(PASSWORD_SPECIAL_CHARS) == len(set(PASSWORD_SPECIAL_CHARS)) == 17
    assert set(PASSWORD_SPECIAL_CHARS) <= COGNITO_SYMBOLS
    assert not set(PASSWORD_SPECIAL_CHARS) & set('$`"\'\\')
    password = Utils.generate_password(16, 2, 2, 2, 2)
    for pattern in (
        '[0-9]',
        '[A-Z]',
        '[a-z]',
        '[\\^\\$\\*\\.\\[\\]{}\\(\\)\\?"!@#%&\\/\\\\,><\':;\\|_~`=\\+\\-]',
    ):
        assert re.search(pattern, password)


def test_every_position_is_shuffled():
    # with 2+2+2+2 in 8 characters the classes would sit in fixed slots if the shuffle were missing
    first = {
        Utils.generate_password(8, 2, 2, 2, 2)[0] in string.ascii_uppercase
        for _ in range(200)
    }
    assert first == {True, False}


def test_uses_secrets_not_random(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('random module used for a password')

    for name in (
        'random',
        'choice',
        'choices',
        'shuffle',
        'randint',
        'randrange',
        'sample',
        'uniform',
    ):
        monkeypatch.setattr(random, name, refuse)
    monkeypatch.setattr(random, 'SystemRandom', refuse)
    assert len(Utils.generate_password(16, 2, 2, 2, 2)) == 16
    assert not hasattr(utils_module, 'PasswordGenerator')


def test_negative_values_are_rejected():
    with pytest.raises(ValueError):
        Utils.generate_password(-1)
    with pytest.raises(ValueError):
        Utils.generate_password(8, -1)
