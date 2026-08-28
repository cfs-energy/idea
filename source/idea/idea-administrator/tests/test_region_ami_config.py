"""
Test Cases for region_ami_config.yml resolution

AMI identity is region + architecture + base OS. A lookup that drops the architecture hands an
x86_64 AMI to an arm64 instance type, which fails at RunInstances instead of at config time.
"""

import pathlib

import pytest

from ideadatamodel import constants, errorcodes, exceptions
from ideasdk.utils import Utils

from ideaadministrator.app.region_ami_config import resolve_region_ami

FLAT_CONFIG = {
    'us-east-2': {
        'amazonlinux2023': 'ami-flat-al2023',
        'rhel9': 'ami-flat-rhel9',
    }
}

NESTED_CONFIG = {
    'us-east-2': {
        'x86_64': {'amazonlinux2023': 'ami-x86-al2023'},
        'arm64': {'amazonlinux2023': 'ami-arm-al2023'},
    }
}

HALF_MIGRATED_CONFIG = {
    'us-east-2': {
        'amazonlinux2023': 'ami-flat-al2023',
        'arm64': {'amazonlinux2023': 'ami-arm-al2023'},
    }
}


def test_flat_schema_defaults_to_x86_64():
    """
    entries listed directly under a region are x86_64 - the documented legacy shape
    """
    assert (
        resolve_region_ami(
            regions_config=FLAT_CONFIG,
            aws_region='us-east-2',
            base_os='amazonlinux2023',
        )
        == 'ami-flat-al2023'
    )
    assert (
        resolve_region_ami(
            regions_config=FLAT_CONFIG,
            aws_region='us-east-2',
            base_os='amazonlinux2023',
            architecture=constants.ARCHITECTURE_X86_64,
        )
        == 'ami-flat-al2023'
    )


def test_flat_schema_rejects_non_x86_64():
    """
    a flat region carries no arm64 AMI - fail naming the architecture rather than returning
    the x86_64 AMI for it
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        resolve_region_ami(
            regions_config=FLAT_CONFIG,
            aws_region='us-east-2',
            base_os='amazonlinux2023',
            architecture=constants.ARCHITECTURE_ARM64,
        )
    assert 'arm64' in exc_info.value.message


def test_nested_schema_selects_by_architecture():
    for architecture, expected in (
        (constants.ARCHITECTURE_X86_64, 'ami-x86-al2023'),
        (constants.ARCHITECTURE_ARM64, 'ami-arm-al2023'),
    ):
        assert (
            resolve_region_ami(
                regions_config=NESTED_CONFIG,
                aws_region='us-east-2',
                base_os='amazonlinux2023',
                architecture=architecture,
            )
            == expected
        )


def test_nested_schema_missing_architecture():
    config = {'us-east-2': {'x86_64': {'amazonlinux2023': 'ami-x86-al2023'}}}
    with pytest.raises(exceptions.SocaException) as exc_info:
        resolve_region_ami(
            regions_config=config,
            aws_region='us-east-2',
            base_os='amazonlinux2023',
            architecture=constants.ARCHITECTURE_ARM64,
        )
    assert 'arm64' in exc_info.value.message


def test_unknown_region():
    with pytest.raises(exceptions.SocaException) as exc_info:
        resolve_region_ami(
            regions_config=FLAT_CONFIG,
            aws_region='eu-west-9',
            base_os='amazonlinux2023',
        )
    assert 'eu-west-9' in exc_info.value.message


def test_unknown_base_os_names_the_architecture():
    with pytest.raises(exceptions.SocaException) as exc_info:
        resolve_region_ami(
            regions_config=NESTED_CONFIG,
            aws_region='us-east-2',
            base_os='rocky9',
            architecture=constants.ARCHITECTURE_ARM64,
        )
    assert 'rocky9' in exc_info.value.message
    assert 'arm64' in exc_info.value.message


def test_missing_base_os_for_region_raises_cluster_config_error():
    """
    a base_os with no AMI for an otherwise-known region (e.g. rocky10 in a region the EL10
    backfill skipped) is a user config mistake, not a broken region_ami_config.yml - it must
    fail with CONFIG_ERROR like the adjacent EOL/EL10 guards, not a raw GENERAL_ERROR traceback.
    """
    with pytest.raises(exceptions.SocaException) as exc_info:
        resolve_region_ami(
            regions_config=FLAT_CONFIG,
            aws_region='us-east-2',
            base_os='rocky10',
        )
    assert exc_info.value.error_code == errorcodes.CONFIG_ERROR


def test_shipped_config_resolves_every_region_and_base_os():
    """
    every AMI in the shipped file must be reachable through the resolver, at the architecture
    the file's shape declares
    """
    config_file = (
        pathlib.Path(__file__).parents[1]
        / 'resources'
        / 'config'
        / 'region_ami_config.yml'
    )
    if not config_file.is_file():
        pytest.skip('region_ami_config.yml not available in this checkout')

    regions_config = Utils.from_yaml(config_file.read_text())
    assert len(regions_config) > 0

    for aws_region, region_config in regions_config.items():
        architectures = [
            key for key in region_config if key in constants.SUPPORTED_ARCHITECTURES
        ]
        if len(architectures) == 0:
            architectures = [constants.ARCHITECTURE_X86_64]
            base_os_by_architecture = {constants.ARCHITECTURE_X86_64: region_config}
        else:
            base_os_by_architecture = {
                architecture: region_config[architecture]
                for architecture in architectures
            }

        for architecture in architectures:
            for base_os, ami_id in base_os_by_architecture[architecture].items():
                assert (
                    resolve_region_ami(
                        regions_config=regions_config,
                        aws_region=aws_region,
                        base_os=base_os,
                        architecture=architecture,
                    )
                    == ami_id
                )


def test_half_migrated_region_is_rejected_naming_both_halves():
    """
    adding an arm64 section without moving the base OS entries under x86_64 reads as nested,
    which would fail every x86_64 lookup in the region rather than the half that was edited
    """
    for architecture in (
        constants.ARCHITECTURE_X86_64,
        constants.ARCHITECTURE_ARM64,
    ):
        with pytest.raises(exceptions.SocaException) as exc_info:
            resolve_region_ami(
                regions_config=HALF_MIGRATED_CONFIG,
                aws_region='us-east-2',
                base_os='amazonlinux2023',
                architecture=architecture,
            )
        message = exc_info.value.message
        assert 'arm64' in message
        assert 'amazonlinux2023' in message
