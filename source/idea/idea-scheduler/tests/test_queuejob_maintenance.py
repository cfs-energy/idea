"""Direct PBS submissions obey the live maintenance setting, using the submitter's identity."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest

from ideadatamodel import GetUserResult, User
from ideascheduler.app.api.opepbs_api import OpenPBSAPI
from ideascheduler.app.scheduler.openpbs.openpbs_api import OpenPBSHookRequest
from ideascheduler.app.scheduler.openpbs.openpbs_api_invocation_context import (
    OpenPBSAPIInvocationContext,
)
from ideasdk.config.cluster_config import ClusterConfig
from ideasdk.config.soca_config import SocaConfig

MESSAGE = 'Cluster is in maintenance.'
HOOK = Path(__file__).parents[1] / 'resources/openpbs/hooks/openpbs_hook_handler.py'


def admission(context, monkeypatch, event_type='queuejob', requestor='testuser'):
    api = OpenPBSAPI(context)
    # Use the real subscribed config and its stream callbacks, including a renamed module.
    config = ClusterConfig.__new__(ClusterConfig)
    SocaConfig.__init__(
        config,
        {
            'global-settings': {
                'module_sets': {
                    'default': {'cluster-manager': {'module_id': 'manager'}}
                }
            },
        },
    )
    config.module_set = 'default'
    config.module_info = None
    config.logger = Mock()
    monkeypatch.setattr(context, 'config', lambda: config)
    context.accounts_client.get_user.return_value = GetUserResult(
        user=User(enabled=True, sudo=False)
    )
    invocation = Mock(context=context)
    invocation.get_request_payload_as.return_value = OpenPBSHookRequest(
        event={
            'type': event_type,
            'hook_name': 'validate_job',
            'requestor': requestor,
            'job': {'Job_Owner': 'admin@submit.example', 'queue': 'normal'},
        }
    )
    # The transport is admin, and a claimed job owner must not exempt the real submitter.
    invocation.is_administrator.return_value = True
    hook = OpenPBSAPIInvocationContext(invocation)
    hook.build_and_validate_job = Mock(side_effect=ReachedValidation)
    monkeypatch.setattr(context, 'is_ready', lambda: True)
    return api, hook, config


class ReachedValidation(BaseException):
    """Stop after admission without provisioning a job or invoking AWS."""


def enabled(config, value):
    config.on_update({}, {'key': 'manager.maintenance.enabled', 'value': value})


def test_queuejob_reads_live_setting_without_recreating_api(context, monkeypatch):
    api, hook, config = admission(context, monkeypatch)
    # Missing, on, off and on again all use the same API and invocation objects.
    for value in (None, True, False, True):
        if value is not None:
            enabled(config, value)
        hook.api_context.success.reset_mock()
        hook.build_and_validate_job.reset_mock()
        if value:
            api.hook_validate_job(hook)
            result = hook.api_context.success.call_args.args[0]
            assert result.accept is False
            assert result.formatted_user_message == MESSAGE
            hook.build_and_validate_job.assert_not_called()
        else:
            with pytest.raises(ReachedValidation):
                api.hook_validate_job(hook)
            hook.build_and_validate_job.assert_called_once()
            hook.api_context.success.assert_not_called()
    assert context.accounts_client.get_user.call_count == 2
    assert all(
        call.args[0].username == 'testuser'
        for call in context.accounts_client.get_user.call_args_list
    )


@pytest.mark.parametrize('requestor,sudo', [('testuser', True), ('root', False)])
def test_queuejob_admin_exemption(context, monkeypatch, requestor, sudo):
    api, hook, config = admission(context, monkeypatch, requestor=requestor)
    enabled(config, True)
    context.accounts_client.get_user.return_value = GetUserResult(
        user=User(enabled=True, sudo=sudo)
    )
    with pytest.raises(ReachedValidation):
        api.hook_validate_job(hook)
    hook.api_context.success.assert_not_called()
    if requestor == 'root':
        context.accounts_client.get_user.assert_not_called()


@pytest.mark.parametrize('sudo', [False, None, 'missing-user'])
def test_queuejob_non_admin_rejected(context, monkeypatch, sudo):
    api, hook, config = admission(context, monkeypatch)
    enabled(config, True)
    context.accounts_client.get_user.return_value = GetUserResult(
        user=None if sudo == 'missing-user' else User(enabled=True, sudo=sudo)
    )
    api.hook_validate_job(hook)
    result = hook.api_context.success.call_args.args[0]
    assert result.accept is False
    assert result.formatted_user_message == MESSAGE
    hook.build_and_validate_job.assert_not_called()


@pytest.mark.parametrize('event_type', ['modifyjob', 'movejob'])
def test_maintenance_does_not_gate_existing_jobs(context, monkeypatch, event_type):
    api, hook, config = admission(context, monkeypatch, event_type)
    enabled(config, True)
    with pytest.raises(ReachedValidation):
        api.hook_validate_job(hook)
    context.accounts_client.get_user.assert_not_called()


def test_queuejob_hook_returns_maintenance_message_to_qsub(context, monkeypatch):
    api, hook, config = admission(context, monkeypatch)
    enabled(config, True)
    api.hook_validate_job(hook)
    result = hook.api_context.success.call_args.args[0]
    event = SimpleNamespace(type='queuejob', accept=Mock(), reject=Mock())
    entrypoint = HOOK.read_text().split('\ne = pbs.event()\n', 1)[1]

    class SocaException(Exception):
        def __init__(self, message):
            self.message = message

    exec(
        compile(entrypoint, str(HOOK), 'exec'),
        {
            'e': event,
            'pbs': Mock(),
            'is_applicable': lambda e: True,
            'PRE_EXECUTION_HOOKS': ['queuejob', 'modifyjob', 'movejob'],
            'invoke_soca_scheduler': lambda e: {
                'success': True,
                'payload': result.model_dump(),
            },
            'time': time,
            'start_time': time.time() * 1000,
            'SocaException': SocaException,
            'sanitize_error_message': lambda value: value,
            'is_always_success_event': lambda e: False,
        },
    )
    event.reject.assert_called_once_with(MESSAGE)
    event.accept.assert_not_called()


@pytest.mark.parametrize('message', ['Upgrading, back at 18:00 UTC', '', '   '])
@pytest.mark.parametrize('lookup_fails', [False, True])
def test_queuejob_maintenance_notice_survives_account_lookup_failure(
    context, monkeypatch, message, lookup_fails
):
    api, hook, config = admission(context, monkeypatch)
    enabled(config, True)
    config.on_update({}, {'key': 'manager.maintenance.message', 'value': message})
    api.logger = Mock()
    if lookup_fails:
        context.accounts_client.get_user.side_effect = RuntimeError('HTTP error 502')

    api.hook_validate_job(hook)

    result = hook.api_context.success.call_args.args[0]
    assert result.accept is False
    assert result.formatted_user_message == (
        f'Cluster is in maintenance: {message.strip()}' if message.strip() else MESSAGE
    )
    hook.build_and_validate_job.assert_not_called()
    assert api.logger.warning.called is lookup_fails


@pytest.mark.parametrize(
    'message,ends_at,expected',
    [
        (
            'Upgrade',
            '2026-11-01T13:30:00-05:00',
            'Cluster is in maintenance: Upgrade. Expected to end 2026-11-01 18:30 UTC.',
        ),
        (
            'Upgrade',
            '2026-11-01T18:30:00',
            'Cluster is in maintenance: Upgrade. Expected to end 2026-11-01 18:30 UTC.',
        ),
        (
            '',
            '2026-11-01T18:30:00Z',
            'Cluster is in maintenance. Expected to end 2026-11-01 18:30 UTC.',
        ),
        ('Upgrade', 'invalid', 'Cluster is in maintenance: Upgrade'),
        ('   ', '', 'Cluster is in maintenance.'),
    ],
)
def test_portal_and_qsub_share_complete_notice(
    context, monkeypatch, message, ends_at, expected
):
    from ideadatamodel import exceptions
    from ideasdk.api import ApiInvocationContext
    from ideascheduler.app.api.scheduler_api import SchedulerAPI
    from test_job_submission_maintenance import build_invocation_context

    api, hook, config = admission(context, monkeypatch)
    enabled(config, True)
    config.on_update({}, {'key': 'manager.maintenance.message', 'value': message})
    config.on_update({}, {'key': 'manager.maintenance.ends_at', 'value': ends_at})
    monkeypatch.setattr(ApiInvocationContext, 'get_username', lambda _: 'testuser')
    invocation = build_invocation_context(context, {'job_owner': 'root'})
    with pytest.raises(exceptions.SocaException) as error:
        SchedulerAPI(context).submit_job(invocation)
    api.hook_validate_job(hook)
    assert error.value.message == expected
    assert (
        hook.api_context.success.call_args.args[0].formatted_user_message
        == error.value.message
    )


@pytest.mark.parametrize('username,sudo', [('testuser', True), ('root', False)])
def test_portal_exempts_authenticated_administrator(
    context, monkeypatch, username, sudo
):
    from ideadatamodel import exceptions
    from ideasdk.api import ApiInvocationContext
    from ideascheduler.app.api.scheduler_api import SchedulerAPI
    from test_job_submission_maintenance import build_invocation_context

    _, _, config = admission(context, monkeypatch)
    enabled(config, True)
    context.accounts_client.get_user.return_value = GetUserResult(
        user=User(enabled=True, sudo=sudo)
    )
    monkeypatch.setattr(ApiInvocationContext, 'get_username', lambda _: username)
    invocation = build_invocation_context(context, {'job_owner': username})
    with pytest.raises(exceptions.SocaException, match='job_script is required'):
        SchedulerAPI(context).submit_job(invocation)
    # Root skips the maintenance lookup, but still passes the enabled-account guard.
    assert context.accounts_client.get_user.call_count == (
        1 if username == 'root' else 2
    )


@pytest.mark.parametrize('missing_user', [False, True])
def test_portal_maintenance_fails_closed_on_account_lookup(
    context, monkeypatch, missing_user
):
    from ideadatamodel import exceptions
    from ideasdk.api import ApiInvocationContext
    from ideascheduler.app.api.scheduler_api import SchedulerAPI
    from test_job_submission_maintenance import build_invocation_context

    _, _, config = admission(context, monkeypatch)
    enabled(config, True)
    if missing_user:
        context.accounts_client.get_user.return_value = GetUserResult()
    else:
        context.accounts_client.get_user.side_effect = RuntimeError('unavailable')
    monkeypatch.setattr(ApiInvocationContext, 'get_username', lambda _: 'testuser')
    invocation = build_invocation_context(context, {'job_owner': 'root'})
    with pytest.raises(exceptions.SocaException, match=MESSAGE):
        SchedulerAPI(context).submit_job(invocation)


@pytest.mark.parametrize('stall_at', ['token', 'accounts', 'both'])
def test_slow_real_accounts_client_returns_notice_before_hook_deadline(
    context, monkeypatch, stall_at
):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import BoundedSemaphore, Event, Thread

    from ideasdk.auth import TokenService, TokenServiceOptions
    from ideasdk.client.accounts_client import AccountsClient
    from ideasdk.client.soca_client import SocaClientOptions
    from ideascheduler.app.api import scheduler_api

    api, hook, config = admission(context, monkeypatch)
    enabled(config, True)
    config.on_update(
        {}, {'key': 'manager.maintenance.message', 'value': 'Upgrade in progress'}
    )
    config.on_update(
        {}, {'key': 'manager.maintenance.ends_at', 'value': '2026-11-01T18:00:00Z'}
    )
    # Scale only the outer budget; the real token service and accounts HTTP timeouts stay intact.
    budget = 0.15
    assert 0 < getattr(scheduler_api, 'MAINTENANCE_LOOKUP_TIMEOUT_SECONDS', 2) < 10
    monkeypatch.setattr(
        scheduler_api, 'MAINTENANCE_LOOKUP_TIMEOUT_SECONDS', budget, raising=False
    )
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(
        scheduler_api, '_maintenance_lookup_slots', slots, raising=False
    )
    requests = []
    done = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            requests.append(self.path)
            token = self.path == '/oauth2/token'
            if stall_at == 'both':
                time.sleep(budget * 0.7)
            elif (stall_at == 'token') == token:
                time.sleep(budget * 3)
            body = json.dumps(
                {
                    'access_token': 'test-token',
                    'token_type': 'Bearer',
                    'expires_in': 3600,
                }
                if token
                else {'success': True, 'payload': {'user': {'sudo': True}}}
            ).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if not token:
                done.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f'http://127.0.0.1:{server.server_port}'
    token_service = TokenService(
        context,
        TokenServiceOptions(
            cognito_user_pool_provider_url=endpoint,
            cognito_user_pool_domain_url=endpoint,
            client_id='test-client',
            client_secret='test-secret',
            client_credentials_scope=['accounts/read'],
        ),
    )
    client = AccountsClient(
        context, SocaClientOptions(endpoint=endpoint + '/accounts'), token_service
    )
    context.accounts_client = client
    try:
        started = time.monotonic()
        api.hook_validate_job(hook)
        elapsed = time.monotonic() - started
        result = hook.api_context.success.call_args.args[0]
        assert result.accept is False
        assert result.formatted_user_message == (
            'Cluster is in maintenance: Upgrade in progress. Expected to end 2026-11-01 18:00 UTC.'
        )
        assert elapsed < budget * 1.8
        hook.build_and_validate_job.assert_not_called()
        # Saturation fails closed without launching another token/account request.
        assert not scheduler_api.maintenance_exempt(context, 'another-user', api.logger)
        assert scheduler_api.maintenance_exempt(context, 'root', api.logger)
        assert done.wait(2)
        assert requests == ['/oauth2/token', '/accounts']
        assert slots.acquire(timeout=1)
        slots.release()
        # A late sudo result cannot change the already returned rejection.
        assert hook.api_context.success.call_count == 1
    finally:
        done.wait(2)
        client.destroy()
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize('validation_user', ['idea-validate', 'custom-validator'])
def test_queuejob_exempts_only_configured_validation_submitter(
    context, monkeypatch, validation_user
):
    api, hook, config = admission(context, monkeypatch, requestor=validation_user)
    if validation_user != 'idea-validate':
        config.put(
            'virtual-desktop-controller.software_stacks.image_pipeline',
            {'validation_user': validation_user},
        )
    enabled(config, True)
    with pytest.raises(ReachedValidation):
        api.hook_validate_job(hook)
    context.accounts_client.get_user.assert_not_called()
    # A claimed validation owner cannot exempt an ordinary PBS submitter.
    hook.event.requestor = 'ordinary-user'
    hook.event.job.Job_Owner = validation_user + '@host'
    api.hook_validate_job(hook)
    assert hook.api_context.success.call_args.args[0].accept is False


def test_saturated_maintenance_lookup_logs_refusal(context, monkeypatch):
    from threading import BoundedSemaphore
    from ideascheduler.app.api import scheduler_api

    slots = BoundedSemaphore(4)
    for _ in range(4):
        assert slots.acquire(blocking=False)
    monkeypatch.setattr(scheduler_api, '_maintenance_lookup_slots', slots)
    logger = Mock()
    assert not scheduler_api.maintenance_exempt(context, 'testuser', logger)
    context.accounts_client.get_user.assert_not_called()
    logger.warning.assert_called_once_with(
        'Maintenance admin exemption lookup capacity exhausted; refusing %s',
        'testuser',
    )
    slots.release()
    context.accounts_client.get_user.return_value = GetUserResult(user=User(sudo=True))
    assert scheduler_api.maintenance_exempt(context, 'testuser', logger)


@pytest.mark.parametrize(
    'ends_at,utc',
    json.loads(
        (
            Path(__file__).parents[2]
            / 'idea-cluster-manager/tests/fixtures/maintenance-end-times.json'
        ).read_text()
    )['valid'],
)
def test_accepted_maintenance_end_times_render_in_utc(ends_at, utc):
    from ideascheduler.app.api.scheduler_api import maintenance_message

    config = Mock()
    config.get_string.side_effect = lambda key, default='': (
        ends_at if key.endswith('.ends_at') else 'Closed.'
    )
    assert maintenance_message(config) == (
        f'Cluster is in maintenance: Closed. Expected to end {utc[:10]} {utc[11:16]} UTC.'
    )
