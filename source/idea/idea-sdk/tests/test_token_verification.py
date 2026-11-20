import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ideadatamodel import errorcodes, exceptions
from ideasdk.auth import token_service as token_service_module
from ideasdk.auth.token_service import TokenService, TokenServiceOptions


@pytest.fixture(scope='module')
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def service(signing_key):
    service = TokenService.__new__(TokenService)
    service.options = TokenServiceOptions()
    service._logger = Mock()
    service._jwk = Mock()
    service._jwk.get_signing_key_from_jwt.return_value = SimpleNamespace(
        key=signing_key.public_key()
    )
    return service


def signed(signing_key, **claims):
    claims.setdefault('exp', int(time.time()) + 3600)
    return jwt.encode(claims, signing_key, algorithm='RS256')


def test_an_access_token_from_the_pool_is_accepted(service, signing_key):
    token = signed(signing_key, username='user-marker', token_use='access')
    assert service.decode_token(token)['username'] == 'user-marker'


@pytest.mark.parametrize(
    'claims',
    [
        {'username': 'user-marker', 'token_use': 'id'},
        {'username': 'user-marker'},
        {'username': 'user-marker', 'token_use': 'id', 'aud': 'client-marker'},
    ],
)
def test_a_token_that_is_not_an_access_token_carries_no_authority(
    service, signing_key, claims
):
    with pytest.raises(exceptions.SocaException) as refused:
        service.decode_token(signed(signing_key, **claims))
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


def test_user_pool_endpoints_are_called_with_timeouts(monkeypatch):
    service = TokenService.__new__(TokenService)
    service.options = TokenServiceOptions(
        client_credentials_scope=['scope-marker'], refresh_token='refresh-marker'
    )
    service._logger = Mock()
    service._client_id, service._client_secret = 'client-marker', 'secret-marker'
    service._sso_client_id, service._sso_client_secret = 'sso-marker', 'secret-marker'
    service._client_credentials_lock = MagicMock()
    service._refresh_token_lock = MagicMock()
    monkeypatch.setattr(
        TokenService,
        'cognito_user_pool_domain_url',
        property(lambda self: 'https://auth.example.invalid'),
    )
    response = MagicMock(status_code=200)
    response.json.return_value = {'access_token': 'a', 'expires_in': 1}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(token_service_module.requests, 'post', post)
    service.get_access_token_using_client_credentials()
    service.get_access_token_using_refresh_token()
    service.get_access_token_using_sso_auth_code('code-marker', 'https://x.invalid')
    service.revoke_authorization('refresh-marker', sso_auth=True)
    assert post.call_count == 4
    for call in post.call_args_list:
        connect, read = call.kwargs['timeout']
        assert 0 < connect <= 10 and 0 < read <= 30
