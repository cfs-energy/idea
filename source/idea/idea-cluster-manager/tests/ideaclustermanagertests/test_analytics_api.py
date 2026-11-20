import pytest

from ideadatamodel import exceptions
from ideaclustermanager.app.api.analytics_api import AnalyticsAPI
from ideasdk.utils import GroupNameHelper
from ideatestutils.api_tokens import api_token_environment, api_token_invocation

ALIAS = 'sample-cluster_vdc_user_sessions'
ALIAS_KEY = 'virtual-desktop-controller.opensearch.dcv_session.alias'
DESKTOP_ADMINS = 'vdc-administrators-module-group'
BODY = {'size': 0, 'aggs': {'state': {'terms': {'field': 'state.raw', 'size': 100}}}}


def environment():
    env = api_token_environment()
    config = env.context.config()
    config.get_string.side_effect = lambda key, *args, **kwargs: (
        ALIAS if key == ALIAS_KEY else 'pool'
    )
    config.get_module_id.side_effect = lambda name: (
        'vdc' if name == 'virtual-desktop-controller' else name
    )
    env.context.accounts.group_name_helper = GroupNameHelper(env.context)
    return env


def query(env, groups, data, claims=None):
    env.groups.return_value = {'Groups': [{'GroupName': g} for g in groups]}
    search = env.context.analytics_service().os_client.os_client.search
    search.reset_mock()
    search.return_value = {'hits': {'hits': []}}
    # a portal session, or (with claims) a client-credentials token
    env.service.decode_token = lambda token, verify_exp=True: (
        claims
        or {
            'username': 'user-a',
            'cognito:groups': groups,
        }
    )
    invocation = api_token_invocation(
        env, 'Analytics.OpenSearchQuery', {'data': data}, token='session'
    )
    try:
        AnalyticsAPI(env.context).invoke(invocation)
        return search.call_args.kwargs
    except exceptions.SocaException:
        assert search.call_count == 0
        raise


@pytest.mark.parametrize('use_api_token', [False, True])
def test_a_user_cannot_query_the_search_indices(use_api_token):
    env = environment()
    env.groups.return_value = {
        'Groups': [{'GroupName': 'cluster-manager-users-module-group'}]
    }
    search = env.context.analytics_service().os_client.os_client.search
    invocation = api_token_invocation(
        env,
        'Analytics.OpenSearchQuery',
        {'data': {'index': ALIAS, 'body': BODY}},
        token=env.created.token if use_api_token else None,
    )
    if not use_api_token:
        env.service.decode_token = lambda token, verify_exp=True: {
            'username': 'user-a',
            'cognito:groups': ['cluster-manager-users-module-group'],
        }
    with pytest.raises(exceptions.SocaException):
        AnalyticsAPI(env.context).invoke(invocation)
    assert search.call_count == 0


@pytest.mark.parametrize('group', ['administrators', 'managers', DESKTOP_ADMINS])
def test_overview_callers_query_the_desktop_session_alias(group):
    env = environment()
    assert query(env, [group], {'index': ALIAS, 'body': BODY}) == {
        'index': ALIAS,
        'body': BODY,
    }


@pytest.mark.parametrize('group', ['administrators', 'managers', DESKTOP_ADMINS])
@pytest.mark.parametrize(
    'data',
    [
        {'body': BODY},
        {'index': 'sample-other-index', 'body': BODY},
        {'index': f'{ALIAS},sample-other-index', 'body': BODY},
        {'index': f'{ALIAS}*', 'body': BODY},
        {'index': [ALIAS], 'body': BODY},
    ],
)
def test_any_other_index_or_none_is_refused(group, data):
    env = environment()
    with pytest.raises(exceptions.SocaException):
        query(env, [group], data)


def test_an_unset_alias_refuses_every_query():
    env = environment()
    env.context.config().get_string.side_effect = lambda key, *a, **k: (
        None if key == ALIAS_KEY else 'pool'
    )
    with pytest.raises(exceptions.SocaException):
        query(env, ['administrators'], {'index': ALIAS, 'body': BODY})


def test_an_application_scope_does_not_admit_a_query():
    env = environment()
    with pytest.raises(exceptions.SocaException):
        query(
            env,
            [],
            {'index': ALIAS, 'body': BODY},
            claims={'client_id': 'sample-client', 'scope': 'cluster-manager/read'},
        )


def test_another_module_administrator_is_refused():
    env = environment()
    with pytest.raises(exceptions.SocaException):
        query(
            env,
            ['scheduler-administrators-module-group'],
            {'index': ALIAS, 'body': BODY},
        )
