"""Retired bootstrap references must not imply a supported deployment path."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
REFERENCES = [
    ('source/idea/idea-bootstrap/common/bootstrap_common.sh', 'get_shake256'),
    ('source/idea/idea-bootstrap/common/bootstrap_common.sh', 'idea_python'),
    (
        'source/idea/idea-bootstrap/_templates/linux/fsx_lustre_client.jinja2',
        'ami_update.py',
    ),
    (
        'source/idea/idea-scheduler/src/ideascheduler/app/scheduler_app.py',
        'supervisord',
    ),
    (
        'source/idea/ideactl/tools/e2e/checks/scheduler-server-name.ts',
        'configure_openpbs_server.jinja2',
    ),
]


def test_retired_bastion_policy_is_absent():
    assert not (
        ROOT / 'source/idea/ideactl/resources/policies/bastion-host.yml'
    ).exists()


@pytest.mark.parametrize('path, reference', REFERENCES)
def test_retired_bootstrap_reference_is_absent(path, reference):
    assert reference not in (ROOT / path).read_text()
