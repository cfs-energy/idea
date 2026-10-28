"""Keep the operator recovery procedure executable without reaching AWS."""

import re
from pathlib import Path
from unittest.mock import MagicMock

import asyncssh
import boto3
import pytest

ROOT = Path(__file__).resolve().parents[5]
DOC = ROOT / 'docs/modules/cluster-manager/web-terminal.md'


def test_rotation_runbook_uses_task_credentials_and_prints_only_public_key(
    monkeypatch, capsys
):
    snippet = re.search(r'```python\n(.*?)\n\s*```', DOC.read_text(), re.S)
    assert snippet, 'rotation must provide an executable in-container procedure'
    import textwrap

    code = textwrap.dedent(snippet[1])
    monkeypatch.setenv('IDEA_CLUSTER_NAME', 'example')
    monkeypatch.setenv('IDEA_MODULE_ID', 'cluster-manager')
    settings = MagicMock()
    monkeypatch.setattr(
        boto3, 'resource', MagicMock(return_value=MagicMock(Table=lambda _: settings))
    )
    client = MagicMock()
    monkeypatch.setattr(boto3, 'client', lambda _: client)
    settings.get_item.return_value = {'Item': {'value': True}}
    with pytest.raises(RuntimeError, match='Disable Terminal'):
        exec(code, {})
    client.put_secret_value.assert_not_called()
    settings.get_item.return_value = {'Item': {'value': False}}
    exec(code, {})
    put = client.put_secret_value.call_args.kwargs
    assert put['SecretId'] == 'example-cluster-manager-terminal-ca'
    ca = asyncssh.import_private_key(put['SecretString'])
    assert capsys.readouterr().out.strip() == ca.export_public_key().decode().strip()
    assert len(put['ClientRequestToken']) == 32


def test_operator_docs_match_ecs_trust_and_network():
    doc = DOC.read_text()
    for stale in [
        'legacy host deployments',
        'Host bastion',
        'host bastion using',
        'host bastion and',
        'SSH ingress is restricted',
        'before sshd starts',
    ]:
        assert stale not in doc
    assert 'VPC CIDR' in doc
    assert 'max_sessions_per_user' in doc
    assert 'while the setting remains disabled' in doc


def test_ca_policy_documents_cloudformation_mutation_exception():
    doc = DOC.read_text()
    assert 'CloudFormation execution role' in doc
    for action in (
        'UpdateSecret',
        'DeleteSecret',
        'PutResourcePolicy',
        'DeleteResourcePolicy',
    ):
        assert f'`{action}`' in doc
    assert (
        'anyone who can assume that role can read or replace the CA or remove its policy protections'
        in doc
    )
    assert 'The secret policy also blocks `UpdateSecret`' not in doc


def test_ticket_lifetime_is_documented_as_an_upper_bound():
    assert 'at most 30 seconds' in DOC.read_text()


def test_websockets_is_a_direct_runtime_dependency():
    assert 'websockets' in (ROOT / 'requirements/runtime.in').read_text().splitlines()


def test_license_instructions_use_persistent_ecs_scheduler():
    doc = (
        ROOT
        / 'docs/modules/hpc-workloads/admin-documentation/configure-floating-license-resources.md'
    ).read_text()
    page = (
        ROOT
        / 'source/idea/idea-cluster-manager/webapp/src/pages/hpc/update-hpc-license.tsx'
    ).read_text()
    for text in (doc, page):
        assert 'Scheduler EC2' not in text
        assert 'systemctl restart pbs' not in text
        assert 'ECS Exec' in text
        assert '--force-new-deployment' in text
        assert 'EFS' in text
    assert 'schedulerInstanceId' not in page
    assert 'getSessionManagerConnectionUrl' not in page
    assert 'server_priv/resourcedef' in doc and 'sched_priv/sched_config' in doc


def test_terminal_api_documents_portal_internal_contract():
    import yaml

    doc = yaml.safe_load(
        (
            ROOT / 'source/idea/idea-cluster-manager/resources/api/api_doc.yml'
        ).read_text()
    )
    description = doc['spec']['description']
    assert 'portal-internal' in description
    for name in (
        'Terminal.ListTargets',
        'Terminal.CreateTicket',
        'TERMINAL_UNAVAILABLE',
        'TERMINAL_TARGET_STOPPED',
        'TERMINAL_TARGET_ERROR',
        'TERMINAL_TARGET_MISCONFIGURED',
        'UNAUTHORIZED_ACCESS',
    ):
        assert name in description


def test_user_docs_do_not_assume_a_bastion_or_upgrade():
    doc = (ROOT / 'docs/first-time-users/terminal.md').read_text()
    assert 'when the cluster has a bastion' in doc
    assert 'launched before the cluster ran 26.11.1' in doc
    assert 'Terminal page' not in doc


def runbook_lines():
    return [
        line
        for line in DOC.read_text().splitlines()
        if line.startswith('sudo ') and 'idea-terminal-trust' in line
    ]


def test_trust_refresh_runbook_never_waits_on_the_retry_loop():
    lines = runbook_lines()
    restarts = [line for line in lines if ' restart ' in line]
    # The service retries until trust lands; a blocking restart would never return.
    assert restarts == ['sudo systemctl --no-block restart idea-terminal-trust.service']
    assert not [line for line in lines if 'is-active' in line]


STUB_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$LOG"
case "$*" in
  *reset-failed*) echo inactive > "$STATE" ;;
  *restart*) (
    # --no-block: the job runs after systemctl returns.
    sleep 0.2; echo "$OUTCOME" > "$STATE"
    if [ "$OUTCOME" = ready ]; then touch "$MARKER"; fi
  ) & ;;
  *is-failed*) [ "$(cat "$STATE")" = failed ] ;;
esac
"""


@pytest.mark.parametrize(
    'before, outcome, succeeds',
    [
        # A previous boot left the marker; this refresh never rewrites it.
        ('ready', 'retrying', False),
        ('ready', 'ready', True),
        # The unit failed 78 earlier and the site policy is now integrated.
        ('failed', 'ready', True),
        # The unit exits 78 again: stop at once rather than wait out the bound.
        ('failed', 'failed', False),
        ('none', 'failed', False),
    ],
)
def test_trust_refresh_runbook_waits_for_this_refresh(
    tmp_path, before, outcome, succeeds
):
    import os
    import subprocess
    import time

    # Run every command in the documented block, not only the trust-unit lines.
    block = DOC.read_text().split('refresh trust as root', 1)[1].split('```', 2)[1]
    lines = [line for line in block.splitlines() if line.startswith('sudo ')]
    wait = next(line for line in lines if 'until test -f' in line)
    assert re.match(r"sudo timeout \d+ sh -c 'until test -f ", wait)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, body in {
        'sudo': '#!/bin/sh\nexec "$@"\n',
        'journalctl': '#!/bin/sh\necho "journal $*"\n',
        'sshd': '#!/bin/sh\nexit 0\n',
        'systemctl': STUB_SYSTEMCTL,
    }.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    marker = tmp_path / 'ready'
    if before == 'ready':
        marker.touch()
    (tmp_path / 'state').write_text('failed\n' if before == 'failed' else 'active\n')
    # Shorten only the bounds; run the documented commands as written otherwise.
    script = '\n'.join(
        re.sub(r'timeout \d+ ', 'timeout 3 ', line)
        .replace('sleep 5', 'sleep 0.1')
        .replace('/run/idea-terminal-trust-ready', str(marker))
        for line in lines
    )
    began = time.monotonic()
    # SSM Run Command runs the block with sh and no -e; the exit status must still fail.
    result = subprocess.run(
        ['sh', '-c', script],
        capture_output=True,
        text=True,
        timeout=10,
        env=dict(
            os.environ,
            PATH=f'{bin_dir}:{os.environ["PATH"]}',
            LOG=str(tmp_path / 'log'),
            STATE=str(tmp_path / 'state'),
            MARKER=str(marker),
            OUTCOME=outcome,
        ),
    )
    assert (result.returncode == 0) is succeeds, result.stderr
    assert ('journal -u idea-terminal-trust.service' in result.stdout) is not succeeds
    if outcome == 'failed':
        assert time.monotonic() - began < 2
    calls = (tmp_path / 'log').read_text().splitlines()
    assert calls[:2] == [
        'reset-failed idea-terminal-trust.service',
        '--no-block restart idea-terminal-trust.service',
    ]


def test_trust_refresh_runbook_leaves_sshd_reload_to_the_helper():
    block = DOC.read_text().split('refresh trust as root', 1)[1].split('```', 2)[1]
    commands = [line for line in block.splitlines() if line.startswith('sudo ')]
    assert commands[0] == 'sudo rm -f /run/idea-terminal-trust-ready'
    assert commands[-1] == 'sudo sshd -t && test -f /run/idea-terminal-trust-ready'
    assert not [line for line in commands if 'reload' in line]


def test_terminal_pages_sit_with_access_and_cluster_manager_docs():
    summary = (ROOT / 'docs/SUMMARY.md').read_text().splitlines()
    help_heading = next(
        i for i, line in enumerate(summary) if 'Help and Support' in line
    )
    ssh = summary.index('  * [SSH](first-time-users/access-your-idea-cluster/ssh.md)')
    banner = summary.index(
        '  * [Maintenance Banner](modules/cluster-manager/maintenance-banner.md)'
    )
    assert summary[ssh + 1] == '  * [Browser Terminal](first-time-users/terminal.md)'
    assert summary[banner + 1] == (
        '  * [Operating browser terminals](modules/cluster-manager/web-terminal.md)'
    )
    assert ssh < help_heading and banner < help_heading
