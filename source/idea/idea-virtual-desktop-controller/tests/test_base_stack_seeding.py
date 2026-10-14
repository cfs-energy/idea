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
