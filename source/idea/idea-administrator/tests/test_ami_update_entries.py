#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

"""
Test Cases for upgrade_cluster Phase 3 config entries

The write-list is derived from the module config templates: every module that
templates base_os / instance_ami must be updated, or that module keeps its
pre-upgrade OS while the rest of the cluster moves.
"""

import pathlib
import re

import pytest

from ideaadministrator.app_main import build_ami_update_entries


AMI_ID = 'ami-0123456789abcdef0'
BASE_OS = 'amazonlinux2023'

# module config key prefix -> settings template that declares base_os / instance_ami
MODULE_TEMPLATES = {
    'bastion-host': 'bastion-host/settings.yml',
    'cluster-manager.ec2.autoscaling': 'cluster-manager/settings.yml',
    'directoryservice': 'directoryservice/_templates/openldap.yml',
    'scheduler': 'scheduler/settings.yml',
    'vdc.controller.autoscaling': 'virtual-desktop-controller/settings.yml',
    'vdc.dcv_broker.autoscaling': 'virtual-desktop-controller/settings.yml',
    'vdc.dcv_connection_gateway.autoscaling': 'virtual-desktop-controller/settings.yml',
}


@pytest.fixture(scope='module')
def entries():
    return build_ami_update_entries(ami_id=AMI_ID, base_os=BASE_OS)


@pytest.fixture(scope='module')
def entries_by_key(entries):
    parsed = {}
    for entry in entries:
        match = re.fullmatch(r'Key=(.+?),Type=(.+?),Value=(.*)', entry)
        assert match is not None, f'malformed entry: {entry}'
        key, data_type, value = match.groups()
        parsed[key] = (data_type, value)
    return parsed


def test_every_entry_is_parseable_by_the_caller(entries, entries_by_key):
    """
    upgrade_cluster splits each entry on ',' - the format must survive that
    """
    assert len(entries_by_key) == len(entries), 'duplicate keys in the write-list'
    for data_type, value in entries_by_key.values():
        assert data_type == 'string'
        assert value in (AMI_ID, BASE_OS)


def test_every_module_gets_both_base_os_and_ami(entries_by_key):
    """
    an AMI without its matching base_os leaves the module inconsistent
    """
    for prefix in MODULE_TEMPLATES:
        assert entries_by_key[f'{prefix}.base_os'] == ('string', BASE_OS)
        assert entries_by_key[f'{prefix}.instance_ami'] == ('string', AMI_ID)


def test_scheduler_compute_node_keys_are_updated(entries_by_key):
    """
    compute nodes are launched from scheduler.compute_node_ami / compute_node_os;
    updating only the AMI leaves the two disagreeing
    """
    assert entries_by_key['scheduler.compute_node_ami'] == ('string', AMI_ID)
    assert entries_by_key['scheduler.compute_node_os'] == ('string', BASE_OS)


def test_write_list_covers_every_templated_module(entries_by_key):
    """
    guards against a new module templating base_os without being added here
    """
    templates_dir = (
        pathlib.Path(__file__).parents[1] / 'resources' / 'config' / 'templates'
    )
    if not templates_dir.is_dir():
        pytest.skip('config templates not available in this checkout')

    templated = set()
    for template in templates_dir.rglob('*.yml'):
        text = template.read_text()
        if re.search(r'^\s*base_os:\s*\{\{\s*base_os\s*\}\}', text, flags=re.MULTILINE):
            templated.add(template.relative_to(templates_dir).as_posix())

    covered = set(MODULE_TEMPLATES.values())
    assert templated == covered, (
        f'config templates declaring base_os changed: {sorted(templated)}; '
        f'update MODULE_TEMPLATES and build_ami_update_entries()'
    )

    for prefix in MODULE_TEMPLATES:
        assert f'{prefix}.base_os' in entries_by_key
