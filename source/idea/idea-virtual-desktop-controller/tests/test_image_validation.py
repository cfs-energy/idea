"""
Test launch for the image pipeline: the READY gate counts from acceptance and a timeout
is a failure, a capacity refusal waits instead of failing, the host checks come back per
check (missing output is a failure), a missing bootstrap log fails, and the validation
desktop and candidate stack are always deleted.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from botocore.exceptions import ClientError

from ideadatamodel import (
    ImageBuildRecord,
    ImagePipelineSettings,
    Project,
    SocaMemory,
    SocaMemoryUnit,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionConnectionInfo,
    VirtualDesktopSessionState,
    VirtualDesktopSoftwareStack,
)
from ideavirtualdesktopcontroller.app.sessions import image_validation as module
from ideavirtualdesktopcontroller.app.sessions.image_validation import (
    CapacityWait,
    ImageTestLauncher,
    linux_host_script,
    parse_check_lines,
    shared_filesystems,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeApi:
    def __init__(self, states, failure_reason=None):
        self.states = list(states)
        self.failure_reason = failure_reason
        self.software_stack_db = Mock()
        self.software_stack_db.get.return_value = None
        self.software_stack_db.create.side_effect = lambda stack: stack
        self.session_utils = Mock()
        self.session_utils.reboot_sessions.return_value = ([], [])
        self.session_db = Mock()
        self.session_db.get_from_db.side_effect = self._get
        self.validated = []

    def _get(self, idea_session_owner, idea_session_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return VirtualDesktopSession(
            idea_session_id=idea_session_id,
            owner=idea_session_owner,
            dcv_session_id='dcv-1',
            state=state,
            server=VirtualDesktopServer(instance_id='i-desk'),
        )

    def validate_create_session_request(self, session):
        self.validated.append(session)
        return session, True

    def complete_create_session_request(self, session, context):
        session.idea_session_id = 'sess-1'
        return session

    def create_session_hook(self, session):
        session.failure_reason = self.failure_reason
        return session


def launcher(api, clock, logs_enabled=True):
    context = MagicMock()
    context.config().get_bool.return_value = logs_enabled
    context.config().get_config.return_value = {}
    context.config().get_string.return_value = 'gw.example.internal'
    context.dcv_broker_client.get_session_connection_data.return_value = (
        VirtualDesktopSessionConnectionInfo(access_token='tok')
    )
    context.aws().ec2().describe_instances.return_value = {'Reservations': []}
    context.aws().logs().filter_log_events.return_value = {
        'events': [{'message': 'IDEA_BOOTSTRAP_COMPLETE'}]
    }
    api.session_utils.create_session.side_effect = api.create_session_hook
    tester = ImageTestLauncher(context, api, clock=clock.time, sleep=clock.sleep)
    tester.ensure_identity = lambda settings: Project(
        project_id='p-validate', name='idea-validate'
    )
    return tester, context


RECORD = ImageBuildRecord(base_os='rocky9', architecture='x86_64', image_id='ami-cand')
STACK = VirtualDesktopSoftwareStack(
    stack_id='ss-base-rocky9-x86-64-base',
    base_os='rocky9',
    ami_id='ami-old',
    min_storage=SocaMemory(value=20, unit=SocaMemoryUnit.GB),
)


@pytest.fixture(autouse=True)
def fake_ssm_and_gateway(monkeypatch):
    def run_ssm(context, instance_id, windows, script, timeout=180, sleep=None):
        return (
            'Success',
            'CHECK|dcv_session|ok|listed\nCHECK|directory_user|ok|resolves\n',
            '',
        )

    monkeypatch.setattr(module, 'run_ssm', run_ssm)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *a, **k: Response())


def test_a_good_launch_passes_every_check_and_is_deleted():
    clock = Clock()
    api = FakeApi(
        [VirtualDesktopSessionState.PROVISIONING, VirtualDesktopSessionState.READY]
    )
    tester, _ = launcher(api, clock)

    checks = tester.test_launch(RECORD, STACK, ImagePipelineSettings())

    assert all(c.ok for c in checks), [(c.name, c.detail) for c in checks if not c.ok]
    assert [c.name for c in checks] == [
        'test_launch',
        'ready_gate',
        'bootstrap_status',
        'connection_info',
        'dcv_session',
        'directory_user',
        'bootstrap_log',
        'reboot_ready',
    ]
    session = api.validated[0]
    assert session.name == 'validate-rocky9-x86_64'
    assert (
        session.owner == 'idea-validate' and session.project.project_id == 'p-validate'
    )
    assert session.software_stack.stack_id == 'ss-validate-rocky9-x86-64'
    created = api.software_stack_db.create.call_args[0][0]
    assert created.ami_id == 'ami-cand' and created.image_pinned is True
    api.session_utils.terminate_sessions.assert_called_once()
    api.software_stack_db.delete.assert_called_once()


def test_not_ready_within_the_gate_is_a_failure_and_still_cleans_up():
    clock = Clock()
    api = FakeApi([VirtualDesktopSessionState.PROVISIONING])
    tester, _ = launcher(api, clock)

    checks = tester.test_launch(
        RECORD, STACK, ImagePipelineSettings(ready_gate_seconds_linux=300)
    )

    assert checks[-1].name == 'ready_gate' and checks[-1].ok is False
    assert 'not READY within 300 s' in checks[-1].detail
    api.session_utils.terminate_sessions.assert_called_once()
    api.software_stack_db.delete.assert_called_once()


def test_windows_gets_the_longer_gate():
    clock = Clock()
    api = FakeApi(
        [VirtualDesktopSessionState.PROVISIONING] * 30
        + [VirtualDesktopSessionState.READY]
    )
    tester, _ = launcher(api, clock)
    record = ImageBuildRecord(
        base_os='windows2022', architecture='x86_64', image_id='ami-win'
    )
    gate = tester._wait_ready(
        SimpleNamespace(owner='u', idea_session_id='s'),
        ImagePipelineSettings().ready_gate_seconds_windows,
        'ready_gate',
    )
    assert gate.ok is True  # 450 s: past the Linux gate, inside the Windows one
    assert record.base_os.startswith('windows')


def test_a_desktop_in_error_fails_with_its_reason():
    clock = Clock()
    api = FakeApi([VirtualDesktopSessionState.ERROR])
    tester, _ = launcher(api, clock)
    checks = tester.test_launch(RECORD, STACK, ImagePipelineSettings())
    assert checks[-1].ok is False and 'ERROR' in checks[-1].detail


def test_a_capacity_refusal_raises_capacity_wait_and_cleans_up():
    clock = Clock()
    api = FakeApi(
        [VirtualDesktopSessionState.READY],
        failure_reason='InsufficientInstanceCapacity: none left',
    )
    tester, _ = launcher(api, clock)
    with pytest.raises(CapacityWait):
        tester.test_launch(RECORD, STACK, ImagePipelineSettings())
    api.software_stack_db.delete.assert_called_once()


def test_any_other_launch_refusal_is_a_failed_check():
    clock = Clock()
    api = FakeApi(
        [VirtualDesktopSessionState.READY], failure_reason='project budget exceeded'
    )
    tester, _ = launcher(api, clock)
    checks = tester.test_launch(RECORD, STACK, ImagePipelineSettings())
    assert [(c.name, c.ok) for c in checks] == [('test_launch', False)]


def test_a_missing_bootstrap_log_fails(monkeypatch):
    clock = Clock()
    api = FakeApi([VirtualDesktopSessionState.READY])
    tester, context = launcher(api, clock)
    context.aws().logs().filter_log_events.side_effect = ClientError(
        {'Error': {'Code': 'ResourceNotFoundException'}}, 'FilterLogEvents'
    )
    checks = {
        c.name: c for c in tester.test_launch(RECORD, STACK, ImagePipelineSettings())
    }
    assert checks['bootstrap_log'].ok is False
    assert 'IDEA_BOOTSTRAP_COMPLETE' in checks['bootstrap_log'].detail


def test_host_checks_missing_from_the_output_fail(monkeypatch):
    clock = Clock()
    api = FakeApi([VirtualDesktopSessionState.READY])
    tester, context = launcher(api, clock)
    context.config().get_config.return_value = {
        'home': {'provider': 'efs', 'mount_dir': '/home'},
        'proj': {'provider': 'efs', 'mount_dir': '/proj', 'scope': ['project']},
    }
    monkeypatch.setattr(
        module,
        'run_ssm',
        lambda *a, **k: ('TimedOut', '', 'no result within 180 seconds'),
    )
    checks = {
        c.name: c for c in tester.test_launch(RECORD, STACK, ImagePipelineSettings())
    }
    assert checks['filesystem:home'].ok is False
    assert 'TimedOut' in checks['filesystem:home'].detail
    assert 'filesystem:proj' not in checks  # another project's storage is not expected


def test_the_host_script_emits_one_check_line_per_check():
    script = linux_host_script('dcv-1', 'idea-validate', [('home', '/home')], 'nvidia')
    for name in ('dcv_session', 'directory_user', 'filesystem:home', 'gpu_runtime'):
        assert name in script
    lines = 'CHECK|dcv_session|ok|listed\nnoise\nCHECK|gpu_runtime|fail|no GPU\n'
    parsed = parse_check_lines(lines, 0)
    assert [(c.name, c.ok) for c in parsed] == [
        ('dcv_session', True),
        ('gpu_runtime', False),
    ]


def test_windows_filesystems_are_the_smb_shares():
    config = MagicMock()
    config.get_config.return_value = {
        'data': {
            'provider': 'fsx_netapp_ontap',
            'mount_dir': '/data',
            'fsx_netapp_ontap': {
                'svm': {'smb_dns': 'svm.example'},
                'volume': {'cifs_share_name': 'data'},
            },
        },
        'home': {'provider': 'efs', 'mount_dir': '/home'},
    }
    assert shared_filesystems(config, windows=True) == [
        ('data', '\\\\svm.example\\data')
    ]
    assert shared_filesystems(config, windows=False) == [
        ('data', '/data'),
        ('home', '/home'),
    ]


def test_the_validation_identity_is_created_with_the_request_models():
    """pydantic serializes the envelope; a SocaAnyPayload payload cannot be serialized"""
    from ideadatamodel import (
        CreateProjectRequest,
        CreateUserRequest,
        SocaEnvelope,
        SocaHeader,
        exceptions,
    )
    from ideasdk.utils import Utils

    api = FakeApi([VirtualDesktopSessionState.READY])
    tester, context = launcher(api, Clock())
    tester.ensure_identity = ImageTestLauncher.ensure_identity.__get__(tester)
    context.accounts_client.get_user.side_effect = exceptions.soca_exception(
        error_code='AUTH_USER_NOT_FOUND', message='User not found: idea-validate'
    )
    context.projects_client.get_project_by_name.side_effect = [
        exceptions.soca_exception(error_code='PROJECT_NOT_FOUND', message='not found'),
        Project(project_id='p-validate', name='idea-validate'),
    ]
    sent = []
    tester._invoke_cluster_manager = lambda namespace, payload: sent.append(
        (namespace, payload)
    )
    project = tester.ensure_identity(ImagePipelineSettings())
    assert project.name == 'idea-validate'
    assert [n for n, _ in sent] == ['Accounts.CreateUser', 'Projects.CreateProject']
    assert isinstance(sent[0][1], CreateUserRequest)
    assert isinstance(sent[1][1], CreateProjectRequest)
    for namespace, payload in sent:
        envelope = SocaEnvelope(
            header=SocaHeader(namespace=namespace, request_id='r-1'), payload=payload
        )
        assert namespace in Utils.to_json(envelope)
    assert sent[0][1].user.username == 'idea-validate'
    assert sent[0][1].user.sudo is False
    assert sent[1][1].project.enable_budgets is False
