from unittest.mock import MagicMock

from ideasdk.config.cluster_config_db import ClusterConfigDB


def test_a_setting_is_read_back_consistently():
    db = ClusterConfigDB.__new__(ClusterConfigDB)
    db.cluster_settings_table = MagicMock()
    db.cluster_settings_table.get_item.return_value = {'Item': {'key': 'k', 'value': 1}}
    assert db.get_config_entry('k') == {'key': 'k', 'value': 1}
    db.cluster_settings_table.get_item.assert_called_once_with(
        Key={'key': 'k'}, ConsistentRead=True
    )
