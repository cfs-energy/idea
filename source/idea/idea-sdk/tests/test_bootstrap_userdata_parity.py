"""The SDK builder renders exactly the user data ideactl records for its own builder (userdata.ts).

ideactl builds the same Linux and Windows user data in TypeScript, and its parity test compares that
output with bootstrap-userdata-expected.json. This test renders the same cases here, so a change to
either builder without the other, or without regenerating the fixture, fails one side.
"""

import json
from pathlib import Path

import pytest

from ideasdk.bootstrap import BootstrapUserDataBuilder

FIXTURES = Path(__file__).resolve().parents[2] / 'ideactl' / 'test' / 'stacks'
CASES = json.loads((FIXTURES / 'bootstrap-userdata-cases.json').read_text())
EXPECTED = json.loads((FIXTURES / 'bootstrap-userdata-expected.json').read_text())
# EC2 refuses user data over 16 KB before encoding
USER_DATA_LIMIT = 16 * 1024


def render(case: dict) -> str:
    return BootstrapUserDataBuilder(
        base_os=case['baseOs'],
        aws_region=case['awsRegion'],
        bootstrap_package_uri=case['bootstrapPackageUri'],
        install_commands=case['installCommands'],
        infra_config=case.get('infraConfig'),
        proxy_config=case.get('proxyConfig'),
        substitution_support=case.get('substitutionSupport', True),
    ).build()


@pytest.mark.parametrize('name', sorted(CASES))
def test_sdk_builder_matches_the_recorded_reference(name):
    assert set(CASES) == set(EXPECTED)
    assert render(CASES[name]) == EXPECTED[name]


def test_windows_user_data_fits_with_the_desktop_install_commands():
    # the commands virtual_desktop_controller_utils._build_userdata adds for a Windows desktop
    userdata = render(
        {
            **CASES['windows'],
            'installCommands': [
                'cd "virtual-desktop-host-windows"',
                'Import-Module .\\Install.ps1',
                'Install-WindowsEC2Instance -ConfigureForEVDI',
            ],
        }
    )
    assert len(userdata.encode()) < USER_DATA_LIMIT // 2


@pytest.mark.parametrize(
    'name,tag',
    [
        ('windows', "@{Key='idea:BootstrapStatus'; Value='bootstrap-download-failed'}"),
        (
            'windows-ami-builder',
            "@{Key='idea:AmiBuilderStatus'; Value='failed:userdata-bootstrap-download'}",
        ),
    ],
)
def test_windows_failure_tag_follows_the_host_kind(name, tag):
    assert tag in EXPECTED[name]
