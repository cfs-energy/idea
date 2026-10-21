import json

import pytest
from test_image_bake_bootstrap import LINUX, render


@pytest.mark.parametrize('base_os', LINUX)
def test_desktop_collects_each_dcv_log_without_duplicating_system_logs(base_os):
    group = '/idea-mock/vdc/dcv-host'
    existing = [
        {'file_path': path, 'log_group_name': group, 'log_stream_name': stream}
        for path, stream in (
            ('/var/log/messages', 'system_{ip_address}'),
            ('/var/log/syslog', 'syslog_{ip_address}'),
            ('/root/bootstrap/logs/*.log', 'bootstrap_{instance_id}'),
        )
    ]
    config = {'logs': {'logs_collected': {'files': {'collect_list': existing.copy()}}}}
    text = render(
        'virtual-desktop-host-linux/setup.sh.jinja2',
        base_os,
        variables={'cloudwatch_agent_config': config},
    )
    encoded = text.split(
        "' > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json"
    )[0].rsplit("echo '", 1)[1]
    files = json.loads(encoded)['logs']['logs_collected']['files']['collect_list']
    assert files[:3] == existing
    assert len(files) == 8
    expected = {
        '/var/log/dcv/server.log': 'dcv-server',
        '/var/log/dcv/sessionlauncher.log': 'dcv-sessionlauncher',
        '/var/log/dcv/agent.*.log': 'dcv-agent',
        '/var/log/dcv/agentlauncher.*.log': 'dcv-agentlauncher',
        '/var/log/dcv-session-manager-agent/agent.log': 'dcv-session-manager-agent',
    }
    for entry in files[3:]:
        path = entry['file_path']
        assert entry['log_group_name'] == group
        assert entry['log_stream_name'] == expected[path] + '_{ip_address}'
        assert entry.get('publish_multi_logs', False) == ('*' in path)


@pytest.mark.parametrize('config', [None, {'metrics': {}}])
def test_desktop_does_not_enable_disabled_cloudwatch_logs(config):
    text = render(
        'virtual-desktop-host-linux/setup.sh.jinja2',
        variables={'cloudwatch_agent_config': config},
    )
    assert 'dcv-server_{ip_address}' not in text
