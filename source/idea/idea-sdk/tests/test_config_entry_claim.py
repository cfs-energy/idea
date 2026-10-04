"""set_config_entry_if: the conditional write a lagging settings copy claims a run with."""

from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ideasdk.config.cluster_config_db import ClusterConfigDB


def config_db(error=None):
    db = object.__new__(ClusterConfigDB)
    db.log_info = Mock()
    db.cluster_settings_table = Mock()
    if error:
        db.cluster_settings_table.update_item.side_effect = error
    return db


def failed(code):
    return ClientError({'Error': {'Code': code, 'Message': code}}, 'UpdateItem')


def test_a_first_claim_requires_no_stored_value():
    db = config_db()
    assert db.set_config_entry_if('k', 2, None) is True
    call = db.cluster_settings_table.update_item.call_args.kwargs
    assert call['ConditionExpression'] == 'attribute_not_exists(#value)'
    assert call['ExpressionAttributeValues'][':value'] == 2


def test_a_claim_requires_the_value_it_read():
    db = config_db()
    assert db.set_config_entry_if('k', 2, 1) is True
    call = db.cluster_settings_table.update_item.call_args.kwargs
    assert call['ConditionExpression'] == '#value = :expected'
    assert call['ExpressionAttributeValues'][':expected'] == 1


def test_a_claim_someone_else_made_first_is_refused():
    db = config_db(failed('ConditionalCheckFailedException'))
    assert db.set_config_entry_if('k', 2, 1) is False


def test_other_errors_are_raised():
    db = config_db(failed('ProvisionedThroughputExceededException'))
    with pytest.raises(ClientError):
        db.set_config_entry_if('k', 2, 1)
