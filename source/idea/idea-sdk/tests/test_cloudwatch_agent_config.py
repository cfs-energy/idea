from unittest.mock import MagicMock

import pytest

from ideasdk.context.bootstrap_context import BootstrapContext
from ideasdk.metrics.cloudwatch.cloudwatch_agent_config import (
    CloudWatchAgentConfig,
    CloudWatchAgentConfigOptions,
    CloudWatchAgentLogFileOptions,
    CloudWatchAgentLogsOptions,
)


def cluster_config():
    config = MagicMock()
    settings = {
        'cluster.cluster_name': 'idea-test',
        'cluster.aws.region': 'us-east-1',
        'cluster.cloudwatch_logs.retention_in_days': 90,
    }

    def get(key, default=None, **_):
        return settings.get(key, default if default is not None else 'value')

    config.get_string.side_effect = get
    config.get_int.side_effect = get
    config.get_bool.side_effect = get
    config.get_list.side_effect = lambda key, default=None, **_: default or []
    return config


@pytest.mark.parametrize('base_os', ['amazonlinux2023', 'windows'])
def test_a_host_agent_asks_for_no_log_retention(base_os):
    rendered = CloudWatchAgentConfig(
        cluster_config=cluster_config(),
        options=CloudWatchAgentConfigOptions(
            module_id='vdc',
            base_os=base_os,
            enable_metrics=False,
            enable_logs=True,
            logs=CloudWatchAgentLogsOptions(
                files=[
                    CloudWatchAgentLogFileOptions(
                        file_path='/var/log/messages',
                        log_group_name='/idea-test/vdc/dcv-host',
                        log_stream_name='system_{ip_address}',
                        # even a caller that still sets it: hosts may not change retention
                        retention_in_days=30,
                    )
                ]
            ),
        ),
    ).build()
    collected = rendered['logs']['logs_collected']['files']['collect_list']
    assert [entry['log_group_name'] for entry in collected] == [
        '/idea-test/vdc/dcv-host'
    ]
    assert 'retention_in_days' not in str(rendered)


def test_additional_log_files_join_a_config_that_had_none():
    context = BootstrapContext.__new__(BootstrapContext)
    context.vars = MagicMock()
    context.vars.cloudwatch_agent_config = {'logs': {'logs_collected': {}}}
    vars(context.vars)['cloudwatch_agent_config'] = context.vars.cloudwatch_agent_config
    extra = {'file_path': '/var/log/x', 'log_group_name': 'g', 'log_stream_name': 's'}
    config = context.get_cloudwatch_agent_config(additional_log_files=[extra])
    assert config['logs']['logs_collected']['files']['collect_list'] == [extra]
