"""
Test Cases for the analytics OpenSearch data node instance type an upgrade writes

An upgrade moves a cluster off the instance type default this release replaces, and leaves every
other stored value alone: a type the operator chose is theirs to keep.
"""

import pathlib

import pytest

from ideadatamodel import constants
from ideaadministrator.app_main import (
    OPENSEARCH_DATA_NODE_INSTANCE_TYPE,
    OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD,
    move_opensearch_data_node_instance_type,
    update_opensearch_data_node_instance_type,
)


DOMAIN_NAME = 'idea-test1-analytics'
ENGINE_VERSION = 'OpenSearch_2.19'
KEY = 'analytics.opensearch.data_node_instance_type'

MODULES = [
    {'module_id': 'analytics', 'name': constants.MODULE_ANALYTICS},
    {'module_id': 'scheduler', 'name': constants.MODULE_SCHEDULER},
]

# what list_instance_type_details returns for the domain's engine version
OFFERED = [
    OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD,
    'm6g.large.search',
    OPENSEARCH_DATA_NODE_INSTANCE_TYPE,
]
NOT_OFFERED = [OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD, 'm6g.large.search']


class FakeConfigDB:
    """the cluster settings reads and the one write the upgrade step makes"""

    def __init__(self, instance_type):
        self.entries = {
            KEY: instance_type,
            'analytics.opensearch.domain_name': DOMAIN_NAME,
        }
        self.writes = {}

    def get_config_entry(self, key):
        value = self.entries.get(key)
        return {'key': key, 'value': value} if value is not None else None

    def set_config_entry(self, key, value):
        self.writes[key] = value


class FakeOpenSearch:
    def __init__(self, offered, error=None):
        self.offered = offered
        self.error = error
        self.calls = []

    def describe_domain(self, **kwargs):
        self.calls.append(('describe_domain', kwargs))
        if self.error is not None:
            raise self.error
        return {'DomainStatus': {'EngineVersion': ENGINE_VERSION}}

    def list_instance_type_details(self, **kwargs):
        self.calls.append(('list_instance_type_details', kwargs))
        return {
            'InstanceTypeDetails': [
                {'InstanceType': instance_type} for instance_type in self.offered
            ]
        }


class RecordingContext:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    def warning(self, message):
        self.messages.append(message)


def update(instance_type, offered=None, error=None, modules=None):
    context = RecordingContext()
    db = FakeConfigDB(instance_type)
    client = FakeOpenSearch(OFFERED if offered is None else offered, error)
    update_opensearch_data_node_instance_type(
        context=context,
        db=db,
        modules=MODULES if modules is None else modules,
        opensearch_client=client,
    )
    return db.writes, context.messages, client


def test_the_old_default_moves_when_the_region_offers_the_new_type():
    writes, messages, client = update(OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD)

    assert writes == {KEY: OPENSEARCH_DATA_NODE_INSTANCE_TYPE}
    assert client.calls[1] == (
        'list_instance_type_details',
        {'EngineVersion': ENGINE_VERSION},
    ), 'the offered types must be read for the engine version the domain runs'
    assert any('blue/green' in message for message in messages)


def test_the_old_default_is_kept_when_the_region_does_not_offer_the_new_type():
    writes, messages, _ = update(
        OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD, offered=NOT_OFFERED
    )

    assert writes == {}
    assert any(
        f'{OPENSEARCH_DATA_NODE_INSTANCE_TYPE} is not offered' in message
        for message in messages
    )


def test_a_type_the_operator_chose_is_kept():
    writes, messages, client = update('r6g.xlarge.search')

    assert writes == {}
    assert client.calls == [], 'a cluster off the old default needs no lookup'
    assert any(
        'keeping analytics data node instance type r6g.xlarge.search' in message
        for message in messages
    )


def test_a_failed_lookup_keeps_the_stored_type():
    writes, messages, _ = update(
        OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD,
        error=RuntimeError('AccessDeniedException'),
    )

    assert writes == {}
    assert any('AccessDeniedException' in message for message in messages)


def test_a_cluster_without_the_analytics_module_is_untouched():
    writes, messages, client = update(
        OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD,
        modules=[{'module_id': 'scheduler', 'name': constants.MODULE_SCHEDULER}],
    )

    assert writes == {}
    assert client.calls == []
    assert messages == []


def test_the_move_decision_is_pure():
    assert move_opensearch_data_node_instance_type(
        OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD, OFFERED
    )
    assert not move_opensearch_data_node_instance_type(
        OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD, NOT_OFFERED
    )
    assert not move_opensearch_data_node_instance_type(
        OPENSEARCH_DATA_NODE_INSTANCE_TYPE_OLD, None
    ), 'an unreadable listing must not write a type the region may not offer'
    assert not move_opensearch_data_node_instance_type(
        OPENSEARCH_DATA_NODE_INSTANCE_TYPE, OFFERED
    ), 'a cluster already on the new default has nothing to move'
    assert not move_opensearch_data_node_instance_type(None, OFFERED)


def test_new_clusters_get_the_same_type_the_upgrade_moves_to():
    """
    a template default that drifts from the constant leaves new and upgraded clusters disagreeing
    """
    template = (
        pathlib.Path(__file__).parents[1]
        / 'resources'
        / 'config'
        / 'templates'
        / 'analytics'
        / 'settings.yml'
    )
    if not template.is_file():
        pytest.skip('config templates not available in this checkout')

    assert (
        f'data_node_instance_type: "{OPENSEARCH_DATA_NODE_INSTANCE_TYPE}"'
        in template.read_text()
    )
