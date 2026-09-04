"""
Test Cases for empty list config values

`system_7: []` in a config template must reach the `<cluster>.cluster-settings` table as an
empty list, not as a DynamoDB NULL: a NULL reads back as None and the jinja templates that
join the list fail. only a genuinely null key stays null.
"""

from decimal import Decimal
from unittest.mock import MagicMock

from boto3.dynamodb.types import TypeSerializer

from ideasdk.config.cluster_config_db import ClusterConfigDB
from ideasdk.config.soca_config import SocaConfig, is_null_value


def stored_value(value):
    """the value set_config_entry hands to the ddb table resource as :value"""
    db = MagicMock()
    ClusterConfigDB.set_config_entry(db, 'key', value)
    kwargs = db.cluster_settings_table.update_item.call_args.kwargs
    return kwargs['ExpressionAttributeValues'][':value']


def test_is_null_value_spares_only_lists():
    assert is_null_value(None) is True
    assert is_null_value('') is True
    assert is_null_value('  ') is True
    assert is_null_value({}) is True
    assert is_null_value([]) is False
    assert is_null_value(['a']) is False
    assert is_null_value(0) is False
    assert is_null_value(False) is False


def test_empty_list_round_trips_as_empty_list():
    config = SocaConfig(config={})
    config.put('module.system_7', [])
    assert config.get_list('module.system_7') == []
    assert config.get('module.system_7') == []


def test_empty_list_read_back_from_db_shaped_config():
    # build_config_from_db() rebuilds the tree from the stored entries
    config = SocaConfig(config={'module': {'system_7': []}})
    assert config.get_list('module.system_7', required=True) == []


def test_empty_list_is_stored_as_a_dynamodb_list_not_null():
    assert stored_value([]) == []
    assert TypeSerializer().serialize(stored_value([])) == {'L': []}


def test_none_is_still_stored_and_read_as_null():
    assert stored_value(None) is None
    assert TypeSerializer().serialize(stored_value(None)) == {'NULL': True}

    config = SocaConfig(config={})
    config.put('module.not_set', None)
    assert config.get_list('module.not_set') is None
    assert config.get_list('module.not_set', default=['fallback']) == ['fallback']


def test_non_empty_list_unchanged():
    config = SocaConfig(config={'module': {'subnet_ids': ['subnet-a', 'subnet-b']}})
    assert config.get_list('module.subnet_ids') == ['subnet-a', 'subnet-b']

    assert stored_value(['subnet-a']) == ['subnet-a']
    # ddb has no float type, so set_config_entry still converts float lists to Decimal
    assert stored_value([1.5]) == [Decimal('1.5')]
