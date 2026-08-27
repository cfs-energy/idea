"""
Resolution of region_ami_config.yml, which is keyed by region, processor architecture and base OS.

Two file shapes are accepted. The flat shape lists x86_64 AMIs only:

    us-east-2:
      amazonlinux2023: ami-...

The nested shape names the architecture, and is required to serve anything other than x86_64:

    us-east-2:
      x86_64:
        amazonlinux2023: ami-...
      arm64:
        amazonlinux2023: ami-...
"""

from ideadatamodel import constants, exceptions
from ideasdk.utils import Utils

from typing import Dict, Optional

__all__ = ('resolve_region_ami',)


def _region_config(regions_config: Dict, aws_region: str) -> Dict:
    ami_config = Utils.get_value_as_dict(aws_region, regions_config)
    if ami_config is None:
        raise exceptions.general_exception(
            f'aws_region: {aws_region} not found in region_ami_config.yml'
        )
    return ami_config


def resolve_region_ami(
    regions_config: Dict,
    aws_region: str,
    base_os: str,
    architecture: Optional[str] = None,
) -> str:
    """
    the AMI for a region / architecture / base OS. raises when the combination is not configured,
    naming the architecture so the failure is not mistaken for an unsupported base OS.
    """
    if Utils.is_empty(architecture):
        architecture = constants.ARCHITECTURE_X86_64

    ami_config = _region_config(regions_config=regions_config, aws_region=aws_region)

    architecture_keys = [
        key for key in ami_config if key in constants.SUPPORTED_ARCHITECTURES
    ]
    base_os_keys = [
        key for key in ami_config if key not in constants.SUPPORTED_ARCHITECTURES
    ]

    if len(architecture_keys) > 0 and len(base_os_keys) > 0:
        # adding an arm64 section without moving the existing base OS keys under
        # x86_64 would otherwise read as nested and fail every x86_64 install here.
        raise exceptions.general_exception(
            f'region: {aws_region} in region_ami_config.yml mixes architecture sections '
            f'({", ".join(sorted(architecture_keys))}) with base OS entries at the top '
            f'level ({", ".join(sorted(base_os_keys))}). Move the base OS entries under an '
            f'architecture section.'
        )

    if len(architecture_keys) > 0:
        architecture_config = Utils.get_value_as_dict(architecture, ami_config)
        if architecture_config is None:
            raise exceptions.general_exception(
                f'no AMIs configured for architecture: {architecture} in region: '
                f'{aws_region} (region_ami_config.yml)'
            )
    elif architecture == constants.ARCHITECTURE_X86_64:
        architecture_config = ami_config
    else:
        raise exceptions.general_exception(
            f'region_ami_config.yml lists {constants.ARCHITECTURE_X86_64} AMIs only for region: '
            f'{aws_region}, and architecture: {architecture} was requested. Add an '
            f'{architecture} section for the region, or set the instance_ami explicitly.'
        )

    ami_id = Utils.get_value_as_string(base_os, architecture_config)
    if Utils.is_empty(ami_id):
        # base_os is a user selection, not a file-authoring bug - fail cleanly like the
        # EOL/EL10 guards in get_base_os() rather than tracebacking on a config mistake.
        raise exceptions.cluster_config_error(
            f'instance_ami not found for base_os: {base_os}, architecture: {architecture}, '
            f'region: {aws_region}'
        )
    return ami_id
