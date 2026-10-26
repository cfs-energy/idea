"""Base software stacks are seeded once per release and once per change to the base stack list."""

from pathlib import Path
from unittest.mock import Mock

import yaml

import ideavirtualdesktopcontroller
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
    VirtualDesktopSoftwareStackDB,
)


CONFIG_FILE = (
    Path(__file__).resolve().parents[1]
    / 'resources'
    / 'base-software-stack-config.yaml'
)


def marker():
    db = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    db.BASE_STACKS_CONFIG_FILE = str(CONFIG_FILE)
    return (
        f'{ideavirtualdesktopcontroller.__version__}+{db._base_stack_config_digest()}'
    )


def build_db(seeded_release, config_file=CONFIG_FILE):
    db = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    db.BASE_STACKS_CONFIG_FILE = str(config_file)
    db.context = Mock()
    db.context.module_id.return_value = 'vdc'
    db.context.aws_util().dynamodb_check_table_exists.return_value = True
    db.context.config().get_string.return_value = seeded_release
    db._logger = Mock()
    db._create_base_software_stacks = Mock()
    return db


def test_a_new_release_seeds_once_and_records_it():
    db = build_db(None)
    db.initialize()
    db._create_base_software_stacks.assert_called_once()
    db.context.config().db.set_config_entry.assert_called_once_with(
        'vdc.software_stacks.base_stacks_seeded_release',
        marker(),
    )


def test_a_seeded_release_leaves_deleted_stacks_deleted():
    db = build_db(marker())
    db.initialize()
    db._create_base_software_stacks.assert_not_called()
    db.context.config().db.set_config_entry.assert_not_called()


def test_seeded_base_stacks_are_at_least_20_gb():
    config_path = (
        Path(__file__).resolve().parents[1]
        / 'resources'
        / 'base-software-stack-config.yaml'
    )
    config = yaml.safe_load(config_path.read_text())
    for base_os, arches in config.items():
        for arch, arch_config in arches.items():
            default = arch_config.get('default-min-storage-value')
            assert default >= 20, f'{base_os} {arch} default is {default} GB'
            for key, entries in arch_config.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and 'min-storage-value' in entry:
                        value = entry['min-storage-value']
                        assert value >= 20, (
                            f'{base_os} {arch} {key} min-storage-value is {value} GB'
                        )


def test_a_seeding_failure_does_not_stop_the_start_or_record_the_release():
    db = build_db(None)
    db._create_base_software_stacks.side_effect = RuntimeError(
        'cluster manager unreachable'
    )
    db.initialize()
    db.context.config().db.set_config_entry.assert_not_called()
    db._logger.warning.assert_called_once()


def test_a_candidate_that_changes_the_base_stack_list_seeds_again(tmp_path):
    # release candidates share a version: rc.1 seeded 26.10.6 before rc.6 added three OSes
    changed = tmp_path / 'base-software-stack-config.yaml'
    changed.write_text(CONFIG_FILE.read_text() + '\n# a new base OS\n')
    db = build_db(marker(), config_file=changed)
    db.initialize()
    db._create_base_software_stacks.assert_called_once()


def test_a_marker_from_before_the_digest_seeds_once_more():
    db = build_db(ideavirtualdesktopcontroller.__version__)
    db.initialize()
    db._create_base_software_stacks.assert_called_once()


def test_new_base_stacks_inherit_same_arch_projects_across_pages():
    from ideadatamodel import (
        Project,
        SocaListingPayload,
        SocaPaginator,
        VirtualDesktopSoftwareStack,
    )

    db = build_db(None)
    del db._create_base_software_stacks
    db.context.config().get_string.return_value = 'us-east-1'
    default = Project(project_id='default', name='default')
    first = Project(project_id='first', name='first')
    second = Project(project_id='second', name='second')
    arm = Project(project_id='arm', name='arm')
    db.context.projects_client.get_default_project.return_value.project = default
    db.get = Mock(return_value=None)
    db.create = Mock()
    db.raise_base_stack_min_storage = Mock()
    config = {
        'default-name': 'base',
        'default-description': 'base desktop',
        'default-min-storage-value': 20,
        'default-min-storage-unit': 'gb',
        'default-min-ram-value': 4,
        'default-min-ram-unit': 'gb',
        'us-east-1': [{'ami-id': 'ami-example', 'ss-id-suffix': 'base'}],
    }
    db.get_base_software_stack_config = Mock(
        return_value={'rocky9': {'x86-64': config, 'arm64': config}}
    )
    db.list_all_from_db = Mock(
        side_effect=[
            SocaListingPayload(
                listing=[
                    VirtualDesktopSoftwareStack(
                        stack_id='ss-base-rocky8-x86-64-base',
                        architecture='x86_64',
                        projects=[first],
                    ),
                    VirtualDesktopSoftwareStack(
                        stack_id='custom', architecture='x86_64', projects=[default]
                    ),
                ],
                paginator=SocaPaginator(cursor='next'),
            ),
            SocaListingPayload(
                listing=[
                    VirtualDesktopSoftwareStack(
                        stack_id='ss-base-rhel9-x86-64-base',
                        architecture='x86_64',
                        projects=[first, second],
                    ),
                    VirtualDesktopSoftwareStack(
                        stack_id='ss-base-rhel9-arm64-base',
                        architecture='arm64',
                        projects=[arm],
                    ),
                ]
            ),
        ]
    )
    db._create_base_software_stacks()
    created = {
        call.args[0].architecture: call.args[0] for call in db.create.call_args_list
    }
    assert [p.project_id for p in created['x86_64'].projects] == ['first', 'second']
    assert [p.project_id for p in created['arm64'].projects] == ['arm']
    assert db.list_all_from_db.call_count == 2

    db.list_all_from_db.side_effect = None
    db.list_all_from_db.return_value = SocaListingPayload(listing=[])
    db.create.reset_mock()
    db._create_base_software_stacks()
    assert len(db.create.call_args_list) == 2
    assert all(call.args[0].projects == [default] for call in db.create.call_args_list)


def test_base_stack_project_defaults_only_when_no_same_arch_base_stack_exists():
    from ideadatamodel import SocaListingPayload, VirtualDesktopSoftwareStack

    db = build_db(None)
    db.list_all_from_db = Mock(
        return_value=SocaListingPayload(
            listing=[
                VirtualDesktopSoftwareStack(
                    stack_id='ss-base-rocky8-x86-64-base',
                    architecture='x86_64',
                    projects=[],
                ),
            ]
        )
    )
    projects = db._base_stack_projects()
    assert projects == {'x86_64': []}
    assert 'arm64' not in projects
