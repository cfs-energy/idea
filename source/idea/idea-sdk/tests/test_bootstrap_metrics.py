"""Workload bootstrap renders logging and node metrics."""

from pathlib import Path

import pytest

from ideadatamodel import constants
from ideasdk.bootstrap.bootstrap_utils import BootstrapUtils
from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils


@pytest.mark.parametrize(
    'node_type,module_name',
    [
        (constants.NODE_TYPE_COMPUTE, 'scheduler'),
        (constants.NODE_TYPE_DCV_HOST, 'virtual-desktop-controller'),
    ],
)
@pytest.mark.parametrize('provider', ['cloudwatch', 'prometheus'])
def test_workload_metrics_render(context, node_type, module_name, provider):
    config = context.config()
    config.put('metrics.provider', provider)
    config.put('metrics.prometheus.remote_write.url', 'https://example.invalid/write')
    for architecture in ('x86_64', 'aarch64'):
        config.put(
            f'global-settings.package_config.prometheus.installer.linux.{architecture}',
            f'https://example.invalid/prometheus-{architecture}.tar.gz',
        )
    bootstrap = BootstrapContext(
        config=config,
        module_name=module_name,
        module_id=module_name,
        module_set='default',
        base_os='amazonlinux2023',
        instance_type='m7i.large',
    )
    BootstrapUtils.check_and_attach_cloudwatch_logging_and_metrics(
        bootstrap_context=bootstrap,
        metrics_namespace=node_type,
        node_type=node_type,
        enable_logging=True,
        log_files=[],
        enable_metrics=True,
    )
    templates = Path(__file__).resolve().parents[2] / 'idea-bootstrap'
    env = Jinja2Utils.env_using_file_system_loader(str(templates))
    rendered = env.get_template('_templates/linux/cloudwatch_agent.jinja2').render(
        context=bootstrap
    )
    assert 'amazon-cloudwatch-agent' in rendered
    if provider == 'cloudwatch':
        assert 'metrics' in bootstrap.vars.cloudwatch_agent_config
    else:
        assert bootstrap.vars.prometheus_exporters == ['node_exporter']
        assert [
            entry['job_name']
            for entry in bootstrap.vars.prometheus_config['scrape_configs']
        ] == ['node_exporter']
        rendered = env.get_template('_templates/linux/prometheus.jinja2').render(
            context=bootstrap, additional_scrape_configs=[]
        )
        assert 'localhost:9100' in rendered
        assert 'metrics_api_token' not in rendered
