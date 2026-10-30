from unittest.mock import MagicMock

from ideasdk.auth import token_service as token_service_module
from ideasdk.auth.token_service import TokenService

SECRET = 'sso-client-secret-value'
TOKENS = {
    'access_token': 'access-token-value',
    'refresh_token': 'refresh-token-value',
    'id_token': 'id-token-value',
    'expires_in': 3600,
    'token_type': 'Bearer',
}


def test_sso_exchange_never_logs_client_secret_or_tokens(monkeypatch):
    service = TokenService.__new__(TokenService)
    service._sso_client_id = 'sso-client-id'
    service._sso_client_secret = SECRET
    service._logger = MagicMock()
    monkeypatch.setattr(
        TokenService,
        'oauth2_access_token_url',
        property(lambda self: 'https://auth.example/oauth2/token'),
    )
    response = MagicMock()
    response.json.return_value = TOKENS
    monkeypatch.setattr(
        token_service_module.requests, 'post', MagicMock(return_value=response)
    )

    result = service.get_access_token_using_sso_auth_code(
        'auth-code', 'https://portal.example/sso'
    )

    assert result.access_token == TOKENS['access_token']
    logged = ' '.join(
        str(arg)
        for method in (service._logger.debug, service._logger.info)
        for call in method.call_args_list
        for arg in call.args
    )
    assert 'sso-client-id' in logged
    for value in (
        SECRET,
        TOKENS['access_token'],
        TOKENS['refresh_token'],
        TOKENS['id_token'],
    ):
        assert value not in logged
