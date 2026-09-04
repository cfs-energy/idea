import pytest
from click.testing import CliRunner

from ideaadministrator import app_main
from ideaadministrator.app import deployment_helper

CLUSTER_NAME = 'c'
AWS_REGION = 'us-east-2'


class FakeContext:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    warning = info
    error = info
    success = info


def stub_db(monkeypatch, config_entries=(), error=None):
    class FakeDb:
        def __init__(self, **_kwargs):
            if error is not None:
                raise error

        def get_config_entries(self):
            return config_entries

    monkeypatch.setattr(app_main, 'ClusterConfigDB', FakeDb)


def settings(*base_os_values):
    return [
        {'key': f'module{index}.base_os', 'value': value}
        for index, value in enumerate(base_os_values)
    ]


def resolve(context, base_os):
    return app_main.resolve_upgrade_base_os(
        context=context,
        cluster_name=CLUSTER_NAME,
        aws_region=AWS_REGION,
        aws_profile=None,
        base_os=base_os,
    )


def test_without_base_os_the_cluster_keeps_the_one_it_runs(monkeypatch):
    # an unattended --force run must not move every module onto the CLI default
    stub_db(monkeypatch, settings('rhel9', 'rhel9'))
    context = FakeContext()

    assert resolve(context, None) == 'rhel9'
    assert any('rhel9' in message for message in context.messages)


def test_disagreeing_base_os_settings_refuse_rather_than_guess(monkeypatch):
    stub_db(monkeypatch, settings('rhel9', 'rocky9'))
    context = FakeContext()

    with pytest.raises(SystemExit):
        resolve(context, None)

    assert any(
        'rhel9' in message and 'rocky9' in message and '--base-os' in message
        for message in context.messages
    )


def test_unreadable_cluster_settings_refuse_rather_than_guess(monkeypatch):
    stub_db(monkeypatch, error=RuntimeError('access denied'))
    context = FakeContext()

    with pytest.raises(SystemExit):
        resolve(context, None)

    assert any('--base-os' in message for message in context.messages)


def test_no_base_os_setting_at_all_refuses(monkeypatch):
    stub_db(monkeypatch, [{'key': 'cluster.locale', 'value': 'en_US'}])
    context = FakeContext()

    with pytest.raises(SystemExit):
        resolve(context, None)


def test_an_explicit_base_os_change_is_announced(monkeypatch):
    stub_db(monkeypatch, settings('rhel9'))
    context = FakeContext()

    assert resolve(context, 'amazonlinux2023') == 'amazonlinux2023'
    assert any(
        'amazonlinux2023' in message and 'rhel9' in message
        for message in context.messages
    )


def test_an_explicit_base_os_survives_unreadable_settings(monkeypatch):
    stub_db(monkeypatch, error=RuntimeError('access denied'))
    context = FakeContext()

    assert resolve(context, 'rhel9') == 'rhel9'


def test_the_upgrade_context_uses_the_requested_region_and_profile(monkeypatch):
    # ClusterConfigDB takes the explicit region; the aws client provider must not be left on
    # whatever region the ambient session happens to default to
    captured = {}

    def fake_context(options=None):
        captured['options'] = options
        return FakeContext()

    monkeypatch.setattr(app_main, 'SocaCliContext', fake_context)
    stub_db(monkeypatch, error=RuntimeError('stop here'))

    result = CliRunner().invoke(
        app_main.upgrade_cluster,
        [
            '--cluster-name',
            CLUSTER_NAME,
            '--aws-region',
            AWS_REGION,
            '--aws-profile',
            'my-profile',
        ],
    )

    assert result.exit_code != 0
    assert captured['options'].aws_region == AWS_REGION
    assert captured['options'].aws_profile == 'my-profile'
    assert captured['options'].enable_aws_client_provider is True


def test_a_failed_parallel_module_fails_the_run(monkeypatch):
    # CdkInvoker signals a failed cdk run with SystemExit, which dies with the thread; the
    # status check cannot see it because the module is still 'deployed' from the last release
    monkeypatch.setattr(deployment_helper.time, 'sleep', lambda *_args: None)

    helper = deployment_helper.DeploymentHelper.__new__(
        deployment_helper.DeploymentHelper
    )
    helper.optimize_deployment = True
    helper.module_ids = ['scheduler', 'vdc']
    helper.get_optimized_deployment_order = lambda: [['scheduler', 'vdc']]

    deployed = []

    def deploy_module(module_id):
        if module_id == 'vdc':
            raise SystemExit(1)
        deployed.append(module_id)

    helper.deploy_module = deploy_module
    helper.initialize_cluster_modules = lambda: pytest.fail(
        'a failed module must fail the run before the status check'
    )

    with pytest.raises(Exception) as raised:
        helper.invoke()

    assert 'vdc' in str(raised.value)
    assert deployed == ['scheduler']
