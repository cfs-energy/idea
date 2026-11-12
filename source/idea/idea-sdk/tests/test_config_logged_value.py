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
            users={'anya.forger': 100, 'loid': 20},
            total_bytes=120,
        ),
        'fs-two': dict(filesystem_id='fs-two', users={'anya.forger': 3}, total_bytes=3),
    }
)
SHOWN = '<2 file systems, 3 users, 123 bytes>'
NAMES = ('anya', 'forger', 'loid')


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
                    'metrics': {'storage': {'usage_snapshot': SNAPSHOT, 'enabled': True}}
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
