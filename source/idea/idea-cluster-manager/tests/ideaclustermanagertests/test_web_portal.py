from unittest.mock import MagicMock

import pytest

from ideaclustermanager.app.web_portal import WebPortal
from ideasdk.config.soca_config import SocaConfig


@pytest.mark.parametrize('context_path', ['/', '/portal', '/portal/'])
def test_routes_and_client_sso_url_agree(context_path, monkeypatch):
    context = MagicMock()
    context.config.return_value = SocaConfig(
        {
            'cluster-manager': {'web_resources_context_path': context_path},
            'identity-provider': {'cognito': {'sso_enabled': True}},
            'global-settings': {
                'module_sets': {
                    'default': {'cluster-manager': {'module_id': 'cluster-manager'}}
                }
            },
        }
    )
    context.module_set.return_value = 'default'
    context.cluster_name.return_value = 'test'
    context.aws().aws_region.return_value = 'us-east-1'
    server = MagicMock()
    server.get_query_param_as_string.return_value = None
    monkeypatch.setattr(WebPortal, 'get_web_app_dir', lambda _: '/tmp/webapp')
    portal = WebPortal(context, server)
    portal.initialize()
    base = context_path.rstrip('/')
    routes = {
        call.kwargs['name']: call.args[1]
        for call in server.http_app.add_route.call_args_list
    }
    assert routes == {
        'index': base or '/',
        'sso_initiate': f'{base}/sso',
        'oauth2_callback': f'{base}/sso/oauth2/callback',
    }
    assert server.http_app.static.call_args.args[0] == (base or '/')
    assert portal.make_route_path('logo.png') == f'{base}/logo.png'
    assert portal.build_app_init_data(MagicMock())['sso_url'] == routes['sso_initiate']
