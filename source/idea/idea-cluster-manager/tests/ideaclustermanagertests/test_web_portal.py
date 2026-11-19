import base64
import json
import time
from unittest.mock import MagicMock

import pytest

from ideaclustermanager.app.web_portal import WebPortal
from ideadatamodel import InitiateAuthRequest, errorcodes, exceptions
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
        'index_html': f'{base}/index.html',
        'sso_initiate': f'{base}/sso',
        'oauth2_callback': f'{base}/sso/oauth2/callback',
    }
    assert server.http_app.static.call_args.args[0] == (base or '/')
    assert portal.make_route_path('logo.png') == f'{base}/logo.png'
    assert portal.build_app_init_data(MagicMock())['sso_url'] == routes['sso_initiate']


@pytest.fixture
def sso(context, monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    from ideadatamodel import AuthResult

    config = SocaConfig(
        {
            'cluster-manager': {'web_resources_context_path': '/portal'},
            'identity-provider': {
                'cognito': {
                    'sso_enabled': True,
                    'domain_url': 'https://auth.example.com',
                    'sso_client_id': 'client',
                    'sso_idp_provider_name': 'idp',
                }
            },
            'global-settings': {
                'module_sets': {
                    'default': {'cluster-manager': {'module_id': 'cluster-manager'}}
                }
            },
        }
    )
    monkeypatch.setattr(
        SocaConfig,
        'get_cluster_external_endpoint',
        lambda _: 'https://portal.example.com',
    )
    portal_context = MagicMock()
    portal_context.config.return_value = config
    portal_context.module_set.return_value = 'default'
    portal_context.accounts = context.accounts
    tokens = portal_context.token_service
    tokens.get_access_token_using_sso_auth_code.return_value = AuthResult(
        access_token='access',
        refresh_token='refresh',
        id_token='id',
        expires_in=3600,
        token_type='Bearer',
    )
    tokens.decode_token.return_value = {'username': 'user'}
    monkeypatch.setattr(context.accounts, 'is_sso_enabled', lambda: True)
    users = context.accounts.user_dao.table
    users.put_item(Item={'username': 'user', 'enabled': True})
    (tmp_path / 'index.html').write_text('{{ app_init_data }}')
    monkeypatch.setattr(WebPortal, 'get_web_app_dir', lambda _: str(tmp_path))
    server = MagicMock()
    server.get_query_param_as_string.side_effect = lambda name, req: req.args.get(name)
    portal = WebPortal(portal_context, server)

    def query(location):
        return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}

    def request(args, browser_key):
        # a browser key alone is sent under the name of the sign-in the request is for
        if browser_key is None or isinstance(browser_key, dict):
            cookies = browser_key or {}
        else:
            state = args.get('state') or args.get('sso_auth_code')
            cookies = {f'__Host-idea-sso-{state}': browser_key}
        return SimpleNamespace(args=args, cookies=cookies)

    def start():
        response = asyncio.run(portal.sso_initiate_route(None))
        state = query(response.headers['location'])['state']
        cookie = response.cookies.get_cookie(
            f'idea-sso-{state}', path='/', host_prefix=True
        )
        return state, cookie

    def callback(state, browser_key):
        response = asyncio.run(
            portal.sso_oauth2_callback_route(
                request({'code': 'idp-code', 'state': state}, browser_key)
            )
        )
        return query(response.headers['location'])

    def page(args, browser_key):
        response = asyncio.run(portal.index_route(request(args, browser_key)))
        init = json.loads(base64.b64decode(response.body))
        return init, [str(c) for c in response.headers.getall('set-cookie', [])]

    def redeem(code):
        return context.accounts.initiate_auth(
            InitiateAuthRequest(auth_flow='SSO_AUTH', authorization_code=code)
        )

    yield SimpleNamespace(
        portal=portal,
        start=start,
        callback=callback,
        page=page,
        redeem=redeem,
        tokens=tokens,
        table=context.accounts.sso_state_dao.table,
    )
    users.delete_item(Key={'username': 'user'})


def test_sso_sign_in_finishes_in_the_browser_that_started_it_once(sso):
    state, cookie = sso.start()
    # host-only: no other host under the domain can set a cookie of this name
    assert str(cookie).startswith(f'__Host-idea-sso-{state}=')
    for attribute in ('Path=/;', 'HttpOnly', 'Secure', 'SameSite=Lax'):
        assert attribute in str(cookie)
    assert 'Domain' not in str(cookie)
    row = sso.table.get_item(Key={'state': state})['Item']
    # the row keeps a hash of the browser's key, never the key, and expires in seconds
    assert cookie.value not in str(row)
    assert 0 < int(row['ttl']) - time.time() <= 600

    returned = sso.callback(state, cookie.value)
    assert returned == {'sso_auth_status': 'SUCCESS', 'sso_auth_code': state}
    init, set_cookies = sso.page(returned, cookie.value)
    assert init['sso_auth_code'] == f'{state}.{cookie.value}'
    # the page took the key: the browser drops the cookie
    assert any(
        c.startswith(f'__Host-idea-sso-{state}=""') and 'Max-Age=0' in c
        for c in set_cookies
    )

    assert sso.redeem(init['sso_auth_code']).auth.access_token == 'access'
    # a code redeems once
    with pytest.raises(exceptions.SocaException) as again:
        sso.redeem(init['sso_auth_code'])
    assert again.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


@pytest.mark.parametrize('browser_key', [None, 'another-browser'])
def test_sso_callback_in_another_browser_stores_nothing(sso, browser_key):
    state, _ = sso.start()
    assert sso.callback(state, browser_key) == {'sso_auth_status': 'FAIL'}
    sso.tokens.get_access_token_using_sso_auth_code.assert_not_called()
    assert 'access_token' not in sso.table.get_item(Key={'state': state})['Item']


def test_sso_code_redeems_only_with_the_starting_browsers_key(sso):
    state, cookie = sso.start()
    returned = sso.callback(state, cookie.value)
    # a page loaded without the cookie is given no code
    init, _ = sso.page(returned, None)
    assert 'sso_auth_code' not in init
    for code in (state, f'{state}.', f'{state}.another-browser'):
        with pytest.raises(exceptions.SocaException) as refused:
            sso.redeem(code)
        assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    # the refused attempts did not use the sign-in up
    assert sso.redeem(f'{state}.{cookie.value}').auth.access_token == 'access'


def test_sso_code_past_its_time_redeems_nothing(sso):
    state, cookie = sso.start()
    sso.callback(state, cookie.value)
    sso.table.update_item(
        Key={'state': state},
        UpdateExpression='SET #ttl = :past',
        ExpressionAttributeNames={'#ttl': 'ttl'},
        ExpressionAttributeValues={':past': int(time.time()) - 1},
    )
    with pytest.raises(exceptions.SocaException):
        sso.redeem(f'{state}.{cookie.value}')


def test_sign_ins_started_in_two_tabs_both_finish(sso):
    browser = {}
    started = []
    for _ in range(2):
        state, cookie = sso.start()
        browser[f'__Host-idea-sso-{state}'] = cookie.value
        started.append((state, cookie.value))
    for state, key in started:
        returned = sso.callback(state, dict(browser))
        assert returned == {'sso_auth_status': 'SUCCESS', 'sso_auth_code': state}
    for state, key in started:
        init, set_cookies = sso.page(
            {'sso_auth_status': 'SUCCESS', 'sso_auth_code': state}, dict(browser)
        )
        assert init['sso_auth_code'] == f'{state}.{key}'
        # each page clears its own sign-in's cookie only
        assert [c.split('=')[0] for c in set_cookies] == [f'__Host-idea-sso-{state}']
        assert sso.redeem(init['sso_auth_code']).auth.access_token == 'access'


def test_a_stale_return_url_leaves_the_sign_in_in_progress_alone(sso):
    old_state, old_cookie = sso.start()
    sso.callback(old_state, old_cookie.value)
    # the old sign-in's cookie has expired; a new sign-in is under way
    state, cookie = sso.start()
    browser = {f'__Host-idea-sso-{state}': cookie.value}
    init, set_cookies = sso.page(
        {'sso_auth_status': 'SUCCESS', 'sso_auth_code': old_state}, dict(browser)
    )
    assert 'sso_auth_code' not in init
    assert set_cookies == []
    assert sso.callback(state, browser) == {
        'sso_auth_status': 'SUCCESS',
        'sso_auth_code': state,
    }


@pytest.mark.parametrize('prefix', ['', '__Secure-'])
def test_a_cookie_without_the_host_prefix_finishes_no_sign_in(sso, prefix):
    state, cookie = sso.start()
    browser = {f'{prefix}idea-sso-{state}': cookie.value}
    assert sso.callback(state, browser) == {'sso_auth_status': 'FAIL'}
    sso.tokens.get_access_token_using_sso_auth_code.assert_not_called()
    init, set_cookies = sso.page({'sso_auth_code': state}, browser)
    assert 'sso_auth_code' not in init and set_cookies == []


def test_sign_in_work_runs_off_the_event_loop(sso, monkeypatch):
    import threading

    threads = []
    dao = sso.portal.context.accounts.sso_state_dao
    for name in ('create_sso_state', 'get_sso_state', 'update_sso_state'):
        method = getattr(dao, name)

        def recorded(*args, _method=method, **kwargs):
            threads.append(threading.current_thread().name)
            return _method(*args, **kwargs)

        monkeypatch.setattr(dao, name, recorded)
    state, cookie = sso.start()
    assert sso.callback(state, cookie.value)['sso_auth_status'] == 'SUCCESS'
    assert len(threads) == 3
    assert all(name.startswith('web-portal-sso') for name in threads)


def test_sign_in_work_past_its_places_is_refused_at_once(sso, monkeypatch):
    import threading

    state, cookie = sso.start()
    monkeypatch.setattr(sso.portal, '_sso_pending', threading.BoundedSemaphore(1))
    sso.portal._sso_pending.acquire()
    count = sso.table.scan()['Count']
    assert sso.callback(state, cookie.value) == {'sso_auth_status': 'FAIL'}
    response = asyncio_run(sso.portal.sso_initiate_route(None))
    assert 'sso_auth_status=FAIL' in response.headers['location']
    # nothing was asked of DynamoDB or the user pool
    assert sso.table.scan()['Count'] == count
    sso.tokens.get_access_token_using_sso_auth_code.assert_not_called()


def asyncio_run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_the_page_file_is_served_by_the_framed_index_route(tmp_path, monkeypatch):
    import sanic

    (tmp_path / 'index.html').write_text('{{ app_init_data }}')
    context = MagicMock()
    context.config.return_value = SocaConfig(
        {'cluster-manager': {'web_resources_context_path': '/'}}
    )
    monkeypatch.setattr(WebPortal, 'get_web_app_dir', lambda _: str(tmp_path))
    server = MagicMock()
    server.http_app = sanic.Sanic('portal_route_marker')
    portal = WebPortal(context, server)
    portal.initialize()
    server.http_app.router.finalize()
    for path in ('/', '/index.html'):
        route, handler, _ = server.http_app.router.get(path, 'GET', None)
        assert handler == portal.index_route


def real_request(path, cookie_header):
    import sanic
    from sanic.compat import Header

    app = sanic.Sanic.get_app('sso_request_marker', force_create=True)
    return sanic.Request(
        url_bytes=path.encode(),
        headers=Header({'cookie': cookie_header}),
        version='1.1',
        method='GET',
        transport=None,
        app=app,
    )


@pytest.mark.parametrize(
    'cookie, finishes',
    [
        ('__Host-idea-sso-{state}={key}', True),
        # another host under the domain can set these names; neither stands for ours
        ('__Secure-__Host-idea-sso-{state}={key}', False),
        ('__Secure-idea-sso-{state}={key}', False),
        ('idea-sso-{state}={key}', False),
    ],
)
def test_the_callback_reads_only_the_host_only_cookie_of_a_real_request(
    sso, cookie, finishes
):
    state, started = sso.start()
    request = real_request(
        f'/sso/oauth2/callback?code=idp-code&state={state}',
        cookie.format(state=state, key=started.value),
    )
    response = asyncio_run(sso.portal.sso_oauth2_callback_route(request))
    from urllib.parse import parse_qs, urlsplit

    returned = parse_qs(urlsplit(response.headers['location']).query)
    assert returned['sso_auth_status'][0] == ('SUCCESS' if finishes else 'FAIL')


def test_starting_a_sign_in_clears_the_oldest_held_sign_in_cookies(sso):
    held = [f'__Host-idea-sso-old{n}=key{n}' for n in range(6)]
    response = asyncio_run(
        sso.portal.sso_initiate_route(real_request('/sso', '; '.join(held)))
    )
    set_cookies = [str(c) for c in response.headers.getall('set-cookie', [])]
    cleared = [c.split('=')[0] for c in set_cookies if 'Max-Age=0' in c]
    kept = WebPortal.SSO_SIGN_INS_KEPT
    # the oldest go, so the new one and the newest others stay within the bound
    assert cleared == [f'__Host-idea-sso-old{n}' for n in range(6 - (kept - 1))]
    assert len([c for c in set_cookies if 'Max-Age=0' not in c]) == 1


def test_the_sign_in_code_state_and_claims_stay_out_of_the_log(sso):
    lines = []
    for level in ('debug', 'info', 'warning', 'error'):
        setattr(
            sso.portal.logger,
            level,
            lambda message, *a, **k: lines.append(str(message)),
        )
    state, cookie = sso.start()
    # the start's redirect carries the state in the browser's address anyway
    lines.clear()
    assert sso.callback(state, cookie.value)['sso_auth_status'] == 'SUCCESS'
    text = ' '.join(lines)
    assert 'idp-code' not in text and state not in text


def test_a_disabled_account_finishes_no_sign_in(sso):
    users = sso.portal.context.accounts.user_dao.table
    users.put_item(Item={'username': 'user', 'enabled': False})
    state, cookie = sso.start()
    assert sso.callback(state, cookie.value) == {'sso_auth_status': 'FAIL'}
    row = sso.table.get_item(Key={'state': state})['Item']
    assert not any(key in row for key in ('access_token', 'refresh_token', 'id_token'))
    users.put_item(Item={'username': 'user', 'enabled': True})


def test_a_callback_without_its_cookie_looks_nothing_up(sso, monkeypatch):
    state, _ = sso.start()
    looked = []
    monkeypatch.setattr(
        sso.portal.context.accounts.sso_state_dao,
        'get_sso_state',
        lambda **kw: looked.append(kw),
    )
    assert sso.callback(state, {}) == {'sso_auth_status': 'FAIL'}
    assert looked == []


@pytest.mark.parametrize(
    'logo, signed',
    [
        ('assets/logo.png', True),
        ('branding/Logo.PNG', True),
        ('my logo.jpg', True),
        ('config/settings.json', False),
        ('a/../b.png', False),
    ],
)
def test_only_an_image_key_is_signed_for_the_sign_in_page(sso, logo, signed):
    portal = sso.portal
    portal.context.config().put('cluster-manager.web_portal.logo', logo)
    signer = portal.context.aws_util().create_s3_presigned_url
    signer.reset_mock()
    url = portal.get_logo_url()
    assert signer.called is signed
    if not signed:
        assert url.endswith('/logo.png')


def test_an_unknown_module_set_is_answered_as_a_bad_request(sso):
    from types import SimpleNamespace

    response = asyncio_run(
        sso.portal.index_route(
            SimpleNamespace(args={'module_set': 'marker'}, cookies={})
        )
    )
    assert response.status == 400


def test_the_page_with_the_one_time_code_is_not_cached(sso):
    from types import SimpleNamespace

    state, cookie = sso.start()
    returned = sso.callback(state, cookie.value)
    request = SimpleNamespace(
        args=returned, cookies={f'__Host-idea-sso-{state}': cookie.value}
    )
    response = asyncio_run(sso.portal.index_route(request))
    assert response.headers.get('Cache-Control') == 'no-store'
