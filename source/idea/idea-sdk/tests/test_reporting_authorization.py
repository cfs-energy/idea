"""Reporting uses verified membership without changing existing authorization."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ideadatamodel import constants
from ideasdk.api import ApiInvocationContext
from ideasdk.auth import ApiAuthorizationType, TokenService, TokenServiceOptions
from ideasdk.config.soca_config import SocaConfig
from ideasdk.utils import GroupNameHelper


@pytest.fixture
def reporting_context():
    config = SocaConfig(
        {
            'identity-provider': {
                'cognito': {
                    'administrators_group_name': 'administrators-cluster-group',
                    'managers_group_name': 'managers-cluster-group',
                }
            }
        }
    )
    return SimpleNamespace(
        config=lambda: config,
        module_id=lambda: constants.MODULE_CLUSTER_MANAGER,
        get_cluster_modules=lambda: [{'module_id': constants.MODULE_CLUSTER_MANAGER}],
    )


@pytest.fixture(scope='module')
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def reporting_invocation(reporting_context, signing_key):
    helper = GroupNameHelper(reporting_context)
    service = object.__new__(TokenService)
    service.options = TokenServiceOptions(
        administrators_group_name=helper.get_cluster_administrators_group(),
        managers_group_name=helper.get_cluster_managers_group(),
    )
    service._logger = Mock()
    service._jwk = Mock()
    service._jwk.get_signing_key_from_jwt.return_value = SimpleNamespace(
        key=signing_key.public_key()
    )

    def build(groups=(), username='reporting-user', scope='', expired=False):
        claims = {
            'cognito:groups': list(groups),
            'scope': scope,
            'exp': int(time.time()) + (-60 if expired else 3600),
        }
        if username is not None:
            claims['username'] = username
        token = jwt.encode(claims, signing_key, algorithm='RS256')
        return ApiInvocationContext(
            context=reporting_context,
            request={'payload': {}},
            invocation_source=constants.API_INVOCATION_SOURCE_HTTP,
            group_name_helper=helper,
            logger=Mock(),
            token={'token_type': 'Bearer', 'token': token},
            token_service=service,
        )

    return build


@pytest.mark.parametrize(
    'role,primary,administrator,manager,module_access,implicit_reporting',
    [
        ('ordinary', ApiAuthorizationType.USER, False, False, False, False),
        ('module_user', ApiAuthorizationType.USER, False, False, True, False),
        ('module_admin', ApiAuthorizationType.USER, False, True, True, False),
        ('manager', ApiAuthorizationType.MANAGER, False, True, True, True),
        ('administrator', ApiAuthorizationType.ADMINISTRATOR, True, False, True, True),
    ],
)
def test_reporting_is_additive_for_every_role(
    reporting_context,
    reporting_invocation,
    role,
    primary,
    administrator,
    manager,
    module_access,
    implicit_reporting,
):
    helper = GroupNameHelper(reporting_context)
    module_id = reporting_context.module_id()
    role_groups = {
        'ordinary': [],
        'module_user': [helper.get_module_users_group(module_id)],
        'module_admin': [helper.get_module_administrators_group(module_id)],
        'manager': [helper.get_cluster_managers_group()],
        'administrator': [helper.get_cluster_administrators_group()],
    }[role]
    operations = helper.get_cluster_operations_leads_group()
    for granted in (False, True, False):
        invocation = reporting_invocation(
            role_groups + ([operations] if granted else [])
        )
        assert invocation.can_read_reporting() is (implicit_reporting or granted)
        assert invocation.get_authorization().type == primary
        assert invocation.is_authenticated_user()
        assert invocation.is_administrator() is administrator
        assert invocation.is_manager() is manager
        assert invocation.is_authorized_user() is module_access
        assert invocation.is_authorized(elevated_access=False) is module_access
        assert invocation.is_authorized(elevated_access=True) is (
            administrator or manager
        )
        assert not invocation.is_authorized_app()
        assert not invocation.is_scope_authorized(f'{module_id}/write')
        assert [
            group
            for group in invocation.get_authorization().groups
            if group != operations
        ] == role_groups


def test_module_administrators_do_not_inherit_reporting(
    reporting_context, reporting_invocation
):
    helper = GroupNameHelper(reporting_context)
    invocation = reporting_invocation(
        [helper.get_module_administrators_group(reporting_context.module_id())]
    )
    assert invocation.is_manager()
    assert not invocation.can_read_reporting()


@pytest.mark.parametrize('scope', ['', 'cluster-manager/read', 'cluster-manager/write'])
def test_apps_cannot_read_reporting_with_privileged_groups(
    reporting_context, reporting_invocation, scope
):
    helper = GroupNameHelper(reporting_context)
    invocation = reporting_invocation(
        [
            helper.get_cluster_operations_leads_group(),
            helper.get_cluster_administrators_group(),
            helper.get_cluster_managers_group(),
        ],
        username=None,
        scope=scope,
    )
    assert invocation.get_authorization().type == ApiAuthorizationType.APP
    assert not invocation.can_read_reporting()


def test_read_scope_does_not_grant_human_reporting(reporting_invocation):
    assert not reporting_invocation(scope='cluster-manager/read').can_read_reporting()


@pytest.mark.parametrize(
    'source',
    [constants.API_INVOCATION_SOURCE_HTTP, constants.API_INVOCATION_SOURCE_UNIX_SOCKET],
)
def test_unauthenticated_groups_are_ignored(reporting_invocation, source):
    invocation = reporting_invocation(['operations-leads-cluster-group'])
    invocation._token = None
    invocation._invocation_source = source
    invocation.request['payload']['groups'] = ['operations-leads-cluster-group']
    assert not invocation.can_read_reporting()


def test_membership_in_request_is_untrusted(reporting_invocation):
    invocation = reporting_invocation()
    invocation.request['payload']['groups'] = ['operations-leads-cluster-group']
    assert not invocation.can_read_reporting()


def test_invalid_signature_cannot_supply_membership(reporting_invocation):
    invocation = reporting_invocation(['operations-leads-cluster-group'])
    claims = jwt.decode(invocation.access_token, options={'verify_signature': False})
    untrusted_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    invocation._token['token'] = jwt.encode(claims, untrusted_key, algorithm='RS256')
    assert not invocation.can_read_reporting()


@pytest.mark.parametrize(
    'configured', ['report-readers', 'report-readers-cluster-group']
)
def test_custom_group_names_are_normalized(
    reporting_context, reporting_invocation, configured
):
    reporting_context.config().put(
        'identity-provider.cognito.operations_leads_group_name', configured
    )
    assert reporting_invocation(['report-readers-cluster-group']).can_read_reporting()
    assert not reporting_invocation(['report-readers']).can_read_reporting()
    assert not reporting_invocation(
        ['operations-leads-cluster-group']
    ).can_read_reporting()


@pytest.mark.parametrize('granted', [False, True])
def test_membership_changes_take_effect_with_verified_token_renewal(
    reporting_invocation, granted
):
    groups = ['operations-leads-cluster-group']
    old_token = reporting_invocation(groups if granted else [])
    assert old_token.can_read_reporting() is granted
    renewed_token = reporting_invocation([] if granted else groups)
    assert renewed_token.can_read_reporting() is not granted
    replayed_token = reporting_invocation()
    replayed_token._token = old_token._token
    assert replayed_token.can_read_reporting() is granted
    expired_token = reporting_invocation(groups if granted else [], expired=True)
    assert not expired_token.can_read_reporting()
