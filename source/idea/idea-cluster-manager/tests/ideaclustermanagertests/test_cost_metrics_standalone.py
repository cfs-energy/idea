import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
from unittest.mock import Mock

import pytest

from ideaclustermanager.app.metrics import standalone
from ideaclustermanager.app.metrics.cost_metrics_service import (
    CostMetrics,
    CostMetricsService,
)


def environment(**overrides):
    return {
        'AWS_DEFAULT_REGION': 'us-west-2',
        'IDEA_CLUSTER_NAME': 'gov-cluster',
        **overrides,
    }


def test_environment_configuration_and_defaults(monkeypatch):
    session = Mock()
    session.get_partition_for_region.return_value = 'aws'
    factory = Mock(return_value=session)
    monkeypatch.setattr(standalone.boto3, 'Session', factory)
    context = standalone.StandaloneContext(environment())
    service = CostMetricsService(context)
    assert service.is_enabled()
    assert service.get_interval_seconds() == 6 * 3600
    assert service.get_lookback_days() == 3
    reader = service.reader()
    assert (reader.module_tag, reader.project_tag, reader.owner_tag) == (
        'idea:ModuleId',
        'idea:Project',
        'idea:JobOwner',
    )
    factory.assert_called_once_with(region_name='us-west-2')
    session.client.assert_called_once_with('ce', region_name='us-east-1')
    session.get_partition_for_region.assert_called_with('us-west-2')
    assert context.module_id() == 'cost-metrics'
    assert context.service_registry().get_service('cost-metrics') is service
    assert context.service_registry().get_service('absent') is None
    context.distributed_lock().acquire(key='collection')
    context.distributed_lock().release(key='collection')
    assert not context.config().get_bool('cost-metrics.metrics.cost.by_account', False)


def test_all_environment_overrides():
    values = {
        'enabled': 'false',
        'interval_hours': '12',
        'lookback_days': '7',
        'module_tag': 'custom:Module',
        'project_tag': 'custom:Project',
        'owner_tag': 'custom:Owner',
        'by_account': 'true',
    }
    context = standalone.StandaloneContext(
        environment(
            **{
                f'IDEA_COST_METRICS_{key.upper()}': value
                for key, value in values.items()
            }
        )
    )
    config = context.config()
    for key, value in values.items():
        assert config.get_string(f'cost-metrics.metrics.cost.{key}') == value
    service = CostMetricsService(context)
    assert not service.is_enabled()
    assert service.get_interval_seconds() == 12 * 3600
    assert service.get_lookback_days() == 7
    assert config.get_bool('cost-metrics.metrics.cost.by_account')
    assert config.get_string('missing', 'fallback') == 'fallback'
    assert config.get_int('missing', 9) == 9
    assert config.get_bool('missing', True)


@pytest.mark.parametrize('region', ['us-gov-west-1', 'cn-north-1'])
def test_noncommercial_partition_disables_collection(region):
    context = standalone.StandaloneContext(environment(AWS_DEFAULT_REGION=region))
    assert not CostMetricsService(context).is_enabled()


@pytest.mark.parametrize('value', ['yes', '0', 'invalid'])
def test_invalid_boolean_is_rejected(value):
    config = standalone.EnvironmentConfig({'IDEA_COST_METRICS_ENABLED': value})
    with pytest.raises(ValueError, match='true or false'):
        config.get_bool('cost-metrics.metrics.cost.enabled')


def test_metrics_service_publishes_to_socket(monkeypatch):
    sender = Mock()
    factory = Mock(return_value=sender)
    monkeypatch.setattr(socket, 'socket', factory)
    path = '/var/run/datadog/dsd.socket'
    context = standalone.StandaloneContext(
        environment(DD_DOGSTATSD_URL=f'unix://{path}')
    )
    CostMetrics(context).publish('cost', 123456, {'module': 'scheduler'}, 2, 3)
    factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_DGRAM)
    assert sender.sendto.call_count == 2
    for call, (basis, value) in zip(
        sender.sendto.call_args_list, [('amortized', 2), ('unblended', 3)]
    ):
        assert call.args == (
            f'idea.cost.{basis}:{value}|c|#idea_cluster:gov-cluster,module:scheduler|T123456|card:none'.encode(),
            path,
        )


def test_sigterm_stops_service_loop():
    code = """
import threading
from ideaclustermanager.app.metrics import standalone
standalone.CostMetricsService.run_once = lambda self: print('running', flush=True)
standalone.main()
assert not any(thread.name == 'cost-metrics' for thread in threading.enumerate())
print('stopped', flush=True)
"""
    process = subprocess.Popen(
        [sys.executable, '-u', '-c', code],
        env={**os.environ, **environment(), 'AWS_EC2_METADATA_DISABLED': 'true'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([process.stdout], [], [], 20)[0], 'collector did not start'
        assert process.stdout.readline().strip() == 'running'
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert stdout.strip() == 'stopped'
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


@pytest.mark.parametrize('argument', [False, True])
def test_container_role_needs_no_cluster_settings(tmp_path, argument):
    script = (
        Path(__file__).resolve().parents[5]
        / 'deployment/ecr/idea-control-plane/entrypoint.sh'
    )
    executable = tmp_path / 'python3.13'
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o755)
    result = subprocess.run(
        ['bash', str(script), *(['cost-metrics'] if argument else [])],
        env={
            'PATH': f'{tmp_path}:/usr/bin:/bin',
            'IDEA_CONTAINER_ROLE': 'cost-metrics',
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        '-m',
        'ideaclustermanager.app.metrics.standalone',
    ]
