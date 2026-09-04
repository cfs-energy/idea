"""
Test Cases for AwsClientProvider.are_credentials_expired
"""

from ideasdk.aws.aws_client_provider import AwsClientProvider

from unittest.mock import Mock


class FakeDeferredCredentials:
    """
    Mimics botocore DeferredRefreshableCredentials: refresh_needed() reports True
    until the credentials are materialized by the first get_frozen_credentials call.
    """

    method = 'sso'

    def __init__(self, refresh_needed_after_fetch: bool = False):
        self.materialized = False
        self._refresh_needed_after_fetch = refresh_needed_after_fetch

    def get_frozen_credentials(self):
        self.materialized = True
        return 'frozen-credentials'

    def refresh_needed(self, refresh_in=None) -> bool:
        if not self.materialized:
            return True
        return self._refresh_needed_after_fetch


class FakeStaticCredentials:
    """
    Mimics botocore Credentials from a config file profile: no refresh_needed.
    """

    method = 'assume-role'


def build_provider(credentials, sts_client=None) -> AwsClientProvider:
    provider = AwsClientProvider.__new__(AwsClientProvider)
    provider._session = Mock(
        get_credentials=Mock(return_value=credentials), region_name=None
    )
    provider.options = Mock(region=None)
    provider._clients = {} if sts_client is None else {'sts': sts_client}
    return provider


def test_are_credentials_expired_materializes_deferred_credentials():
    """
    sso style credentials are not expired once materialized
    """
    credentials = FakeDeferredCredentials()
    provider = build_provider(credentials)

    assert provider.are_credentials_expired() is False
    assert credentials.materialized is True


def test_are_credentials_expired_reports_genuinely_expired_credentials():
    """
    refreshable credentials still stale after materialization are reported expired
    """
    credentials = FakeDeferredCredentials(refresh_needed_after_fetch=True)
    provider = build_provider(credentials)

    assert provider.are_credentials_expired() is True
    assert credentials.materialized is True


def test_are_credentials_expired_when_credentials_are_missing():
    """
    no credentials at all is treated as expired
    """
    provider = build_provider(None)

    assert provider.are_credentials_expired() is True


def test_are_credentials_expired_static_credentials_never_expire():
    """
    credentials without refresh_needed cannot be refreshed and are never expired
    """
    provider = build_provider(FakeStaticCredentials())

    assert provider.are_credentials_expired() is False


def test_are_credentials_expired_env_credentials_use_sts():
    """
    env credentials are validated with sts get_caller_identity
    """
    sts_client = Mock(
        get_caller_identity=Mock(return_value={'Account': '123456789012'})
    )
    provider = build_provider(Mock(method='env'), sts_client=sts_client)

    assert provider.are_credentials_expired() is False
    sts_client.get_caller_identity.assert_called_once()
