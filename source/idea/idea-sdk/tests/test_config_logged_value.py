"""
The storage usage snapshot is a setting, and settings are logged with their value: by the
task that writes one, by every module following the stream of changes, and by every module
when it starts. So each hour's snapshot put every username with their bytes into every
module's log. A log line now shows it as counts and a total; the stored value is unchanged.
"""

import json
from unittest.mock import Mock

import pytest
from pyhocon import ConfigFactory

from ideasdk.config.cluster_config import ClusterConfig
from ideasdk.config.cluster_config_db import ClusterConfigDB
from ideasdk.config.soca_config import SocaConfig

KEY = 'cluster-manager.metrics.storage.usage_snapshot'
SNAPSHOT = json.dumps(
    {
        'fs-one': dict(
            filesystem_id='fs-one',
            users={'user.one': 100, 'user-two': 20},
            total_bytes=120,
        ),
        'fs-two': dict(filesystem_id='fs-two', users={'user.one': 3}, total_bytes=3),
    }
)
SHOWN = '<2 file systems, 3 users, 123 bytes>'
NAMES = ('user.one', 'user-two')


def logged_value(key, value):
    # imported here so the tests of each log line stand on their own
    from ideasdk.config import soca_config

    return soca_config.logged_value(key, value)


def names_no_one(text):
    return not [name for name in NAMES if name in str(text)]


def test_the_snapshot_is_shown_as_counts_and_a_total():
    assert logged_value(KEY, SNAPSHOT) == SHOWN
    assert logged_value('usage_snapshot', SNAPSHOT) == SHOWN


@pytest.mark.parametrize('value', ['not json', '[1, 2]', '{"fs": {"users": 1}}', 7])
def test_a_snapshot_that_cannot_be_read_is_shown_by_its_length(value):
    assert logged_value(KEY, value) == f'<{len(str(value))} characters>'


@pytest.mark.parametrize(
    'key, value',
    [
        ('cluster-manager.metrics.storage.last_collected', 1794333214.8),
        ('cluster-manager.metrics.storage.enabled', True),
        ('cluster.usage_snapshot_bucket', SNAPSHOT),
        (KEY, None),
    ],
)
def test_every_other_setting_is_shown_as_it_is(key, value):
    assert logged_value(key, value) == value


def config_db():
    db = object.__new__(ClusterConfigDB)
    db.log_info = Mock()
    db.cluster_settings_table = Mock()
    return db


def test_writing_the_snapshot_logs_no_names_and_stores_all_of_it():
    db = config_db()

    db.set_config_entry(KEY, SNAPSHOT)
    db.set_config_entry_if(KEY, SNAPSHOT, SNAPSHOT)

    lines = [call.args[0] for call in db.log_info.call_args_list]
    assert lines == [
        f'updating config: {KEY} = {SHOWN}',
        f'updating config: {KEY} = {SHOWN} (was {SHOWN})',
    ]
    for call in db.cluster_settings_table.update_item.call_args_list:
        assert call.kwargs['ExpressionAttributeValues'][':value'] == SNAPSHOT


def test_another_setting_is_still_logged_with_its_value():
    db = config_db()
    db.set_config_entry('cluster-manager.metrics.storage.last_collected', 12)
    db.log_info.assert_called_once_with(
        'updating config: cluster-manager.metrics.storage.last_collected = 12'
    )


def following():
    config = object.__new__(ClusterConfig)
    SocaConfig.__init__(config, config={})
    config.logger = Mock()
    config.module_info = None
    config.module_set = 'default'
    return config


def entry(value=SNAPSHOT, key=KEY):
    return {'key': key, 'value': value, 'source': 'sdk', 'version': 4}


def test_a_module_following_the_settings_logs_no_names_and_keeps_all_of_it():
    config = following()

    config.on_create(entry())
    assert SocaConfig.get_string(config, KEY) == SNAPSHOT
    grown = SNAPSHOT.replace('"total_bytes": 120', '"total_bytes": 121')
    config.on_update(entry(), entry(grown))
    assert SocaConfig.get_string(config, KEY) == grown != SNAPSHOT
    config.on_delete(entry())

    lines = [call.args[0] for call in config.logger.info.call_args_list]
    assert [line.split(':')[0] for line in lines] == [
        'config created',
        'config updated',
        'config deleted',
    ]
    assert all(names_no_one(line) for line in lines)
    assert [line.count(SHOWN) for line in lines] == [1, 1, 1]
    assert '<2 file systems, 3 users, 124 bytes>' in lines[1]


def test_a_module_following_the_settings_still_logs_other_values():
    config = following()
    config.on_update(entry(1, 'cluster.locale'), entry(2, 'cluster.locale'))
    line = config.logger.info.call_args.args[0]
    assert '"value":1' in line.replace(' ', '') and "'value': 2" in line


def test_the_settings_a_module_prints_at_start_name_no_one():
    config = SocaConfig(
        config=ConfigFactory.from_dict(
            {
                'cluster-manager': {
                    'metrics': {
                        'storage': {'usage_snapshot': SNAPSHOT, 'enabled': True}
                    }
                },
                'cluster': {'cluster_name': 'idea-test'},
            }
        )
    )

    printed = config.as_logged_yaml()

    assert names_no_one(printed)
    assert f'usage_snapshot: {SHOWN}' in printed
    assert 'cluster_name: idea-test' in printed and 'enabled: true' in printed
    # the settings themselves are untouched
    assert SocaConfig.get_string(config, KEY) == SNAPSHOT
    assert printed.replace(SHOWN, SNAPSHOT) == config.as_yaml()


def removed_share_config():
    """a live config holding two file systems, one removed setting by setting"""
    config = following()
    for key, value in (
        ('shared-storage.data.provider', 'efs'),
        ('shared-storage.data.mount_dir', '/data'),
        ('shared-storage.rc17dss.provider', 'efs'),
        ('shared-storage.rc17dss.mount_dir', '/rc17d-ss'),
        ('shared-storage.rc17dss.efs.file_system_id', 'fs-1'),
    ):
        config.on_create(entry(value, key))
    for key in (
        'shared-storage.rc17dss.provider',
        'shared-storage.rc17dss.mount_dir',
        'shared-storage.rc17dss.efs.file_system_id',
    ):
        config.on_delete(entry(None, key))
    return config


def test_a_deleted_settings_subtree_leaves_no_empty_entry():
    # dev rc.17 F3: the running scheduler kept an empty rc17dss and every reader that
    # took it for a file system raised on its missing provider
    config = removed_share_config()
    storage = SocaConfig.get_config(config, 'shared-storage')
    assert list(storage.keys()) == ['data']
    assert SocaConfig.get_string(config, 'shared-storage.data.provider') == 'efs'


def test_an_entry_without_a_provider_is_skipped_with_one_warning():
    from ideasdk.config import cluster_config

    cluster_config._skipped_storage.clear()
    config = following()
    config.on_create(entry('efs', 'shared-storage.data.provider'))
    config.on_create(entry('/half', 'shared-storage.half.mount_dir'))
    logger = Mock()
    for _ in range(2):
        entries = cluster_config.shared_storage_entries(config, logger)
        assert [name for name, _ in entries] == ['data']
    logger.warning.assert_called_once()
    assert 'shared-storage.half has no provider' in logger.warning.call_args.args[0]


def test_the_bake_inputs_read_storage_with_an_entry_being_removed():
    from ideasdk.aws.image_builds import slow_inputs

    config = following()
    config.on_create(entry('fsx_lustre', 'shared-storage.scratch.provider'))
    config.on_create(entry('/half', 'shared-storage.half.mount_dir'))
    assert slow_inputs(config, None)['storage_providers'] == ['fsx_lustre']


def test_plain_settings_under_shared_storage_are_not_file_systems():
    # live run 2: every start warned for these two as file systems without a provider
    from ideasdk.config import cluster_config

    cluster_config._skipped_storage.clear()
    config = following()
    config.on_create(entry('efs', 'shared-storage.data.provider'))
    config.on_create(entry('d-123', 'shared-storage.deployment_id'))
    config.on_create(entry('sg-123', 'shared-storage.security_group_id'))
    logger = Mock()
    entries = cluster_config.shared_storage_entries(config, logger)
    assert [name for name, _ in entries] == ['data']
    logger.warning.assert_not_called()
