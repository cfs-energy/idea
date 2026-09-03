"""
Test Cases for the DCV broker DynamoDB table billing mode

The broker creates its tables with a fixed provisioned capacity that stays idle, so the controller
moves them to on demand billing when it starts and again when a broker reports that its boot
completed. No AWS calls are made.
"""

from botocore.exceptions import ClientError

from ideavirtualdesktopcontroller.app.events.handlers.dcv_broker_userdata_execution_complete_event_handler import (
    DCVBrokerUserdataExecutionCompleteEventHandler,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    switch_dcv_broker_tables_to_on_demand,
    switch_ddb_table_to_on_demand,
)

HEALTH_TEST = 'idea-test.vdc.dcv-broker.HealthTest'
DCV_SERVER = 'idea-test.vdc.dcv-broker.dcvServer'
CONTROLLER_TABLE = 'idea-test.vdc.controller.user-sessions'

LIMIT_EXCEEDED = ClientError(
    {
        'Error': {
            'Code': 'LimitExceededException',
            'Message': 'Number of billing mode changes has exceeded the limit',
        }
    },
    'UpdateTable',
)


class FakeLogger:
    def __init__(self):
        self.info_messages = []
        self.warnings = []
        self.errors = []

    def info(self, message, *args, **kwargs):
        self.info_messages.append(str(message))

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))

    def error(self, message, *args, **kwargs):
        self.errors.append(str(message))


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self):
        return iter(self.pages)


class FakeDynamoDBClient:
    def __init__(self, tables=None, update_error=None, list_error=None):
        self.tables = {HEALTH_TEST: 'PROVISIONED'} if tables is None else tables
        self.update_error = update_error
        self.list_error = list_error
        self.updates = []

    def get_paginator(self, operation_name):
        if self.list_error is not None:
            raise self.list_error
        return FakePaginator([{'TableNames': sorted(self.tables)}])

    def describe_table(self, TableName):
        billing_mode = self.tables[TableName]
        if billing_mode is None:
            return {'Table': {}}
        return {'Table': {'BillingModeSummary': {'BillingMode': billing_mode}}}

    def update_table(self, TableName, BillingMode):
        if self.update_error is not None:
            raise self.update_error
        self.tables[TableName] = BillingMode
        self.updates.append((TableName, BillingMode))


class FakeConfig:
    def __init__(self, on_demand=True, autoscaling=True):
        self.values = {
            'virtual-desktop-controller.dcv_broker.dynamodb_table.on_demand': on_demand,
            'virtual-desktop-controller.dcv_broker.dynamodb_table.autoscaling.enabled': autoscaling,
        }

    def get_bool(self, key, default=None):
        return self.values.get(key, default)


class FakeAwsClientProvider:
    def __init__(self, client):
        self._client = client

    def dynamodb(self):
        return self._client


class FakeContext:
    def __init__(self, client=None, config=None):
        self._client = client
        self._config = config or FakeConfig()
        self.fake_logger = FakeLogger()

    def logger(self, name=None):
        return self.fake_logger

    def config(self):
        return self._config

    def cluster_name(self):
        return 'idea-test'

    def module_id(self):
        return 'vdc'

    def aws(self):
        return FakeAwsClientProvider(self._client)


class FakeBrokerClientUtils:
    def __init__(self, table_names):
        self.table_names = table_names

    def get_broker_dynamodb_table_names(self):
        return self.table_names


def build_handler(client, config=None, table_names=None):
    handler = DCVBrokerUserdataExecutionCompleteEventHandler.__new__(
        DCVBrokerUserdataExecutionCompleteEventHandler
    )
    handler.dynamodb_client = client
    handler._logger = FakeLogger()
    handler.context = FakeContext(client=client, config=config or FakeConfig())
    handler.dcv_broker_client_utils = FakeBrokerClientUtils(
        table_names or [HEALTH_TEST]
    )
    handler.tagged = []
    handler.scaled = []
    handler.is_sender_dcv_broker_role = lambda sender_id: True
    handler._add_tags_to_table = lambda message_id, table_name: handler.tagged.append(
        table_name
    )
    handler._create_scaling_policies_for_ddb_table_if_required = (
        lambda message_id, table_name: handler.scaled.append(table_name)
    )
    return handler


def test_provisioned_table_is_switched_to_on_demand():
    client = FakeDynamoDBClient()
    logger = FakeLogger()

    switch_ddb_table_to_on_demand(client, HEALTH_TEST, logger)

    assert client.updates == [(HEALTH_TEST, 'PAY_PER_REQUEST')]


def test_table_already_on_demand_is_left_alone():
    client = FakeDynamoDBClient(tables={HEALTH_TEST: 'PAY_PER_REQUEST'})

    switch_ddb_table_to_on_demand(client, HEALTH_TEST, FakeLogger())

    assert client.updates == []


def test_table_without_billing_mode_summary_is_treated_as_provisioned():
    client = FakeDynamoDBClient(tables={HEALTH_TEST: None})

    switch_ddb_table_to_on_demand(client, HEALTH_TEST, FakeLogger())

    assert client.updates == [(HEALTH_TEST, 'PAY_PER_REQUEST')]


def test_billing_mode_change_limit_is_logged_and_does_not_raise():
    client = FakeDynamoDBClient(update_error=LIMIT_EXCEEDED)
    logger = FakeLogger()

    switch_ddb_table_to_on_demand(client, HEALTH_TEST, logger)

    assert client.updates == []
    assert any('on demand billing' in message for message in logger.warnings)


def test_startup_sweep_switches_only_the_broker_tables():
    client = FakeDynamoDBClient(
        tables={
            HEALTH_TEST: 'PROVISIONED',
            DCV_SERVER: 'PROVISIONED',
            CONTROLLER_TABLE: 'PAY_PER_REQUEST',
        }
    )

    switch_dcv_broker_tables_to_on_demand(FakeContext(client=client))

    assert sorted(name for name, _ in client.updates) == sorted(
        [HEALTH_TEST, DCV_SERVER]
    )


def test_startup_sweep_leaves_on_demand_tables_alone():
    client = FakeDynamoDBClient(
        tables={HEALTH_TEST: 'PAY_PER_REQUEST', DCV_SERVER: 'PAY_PER_REQUEST'}
    )

    switch_dcv_broker_tables_to_on_demand(FakeContext(client=client))

    assert client.updates == []


def test_startup_sweep_does_nothing_when_on_demand_is_disabled():
    client = FakeDynamoDBClient()
    context = FakeContext(client=client, config=FakeConfig(on_demand=False))

    switch_dcv_broker_tables_to_on_demand(context)

    assert client.updates == []
    assert client.tables[HEALTH_TEST] == 'PROVISIONED'


def test_startup_sweep_logs_a_client_error_and_continues():
    client = FakeDynamoDBClient(
        tables={HEALTH_TEST: 'PROVISIONED', DCV_SERVER: 'PROVISIONED'},
        update_error=LIMIT_EXCEEDED,
    )
    context = FakeContext(client=client)

    switch_dcv_broker_tables_to_on_demand(context)

    assert client.updates == []
    assert len(context.fake_logger.warnings) == 2
    assert context.fake_logger.errors == []


def test_startup_sweep_survives_a_list_tables_failure():
    client = FakeDynamoDBClient(list_error=RuntimeError('no credentials'))
    context = FakeContext(client=client)

    switch_dcv_broker_tables_to_on_demand(context)

    assert client.updates == []
    assert any('at startup' in message for message in context.fake_logger.errors)


def test_handle_event_switches_billing_and_skips_autoscaling():
    client = FakeDynamoDBClient(
        tables={HEALTH_TEST: 'PROVISIONED', DCV_SERVER: 'PROVISIONED'}
    )
    handler = build_handler(client, table_names=[HEALTH_TEST, DCV_SERVER])

    handler.handle_event('msg-1', 'sender', None)

    assert [name for name, _ in client.updates] == [HEALTH_TEST, DCV_SERVER]
    assert all('msg-id: msg-1' in m for m in handler._logger.info_messages)
    assert handler.scaled == []
    assert len(handler.tagged) == 2


def test_handle_event_keeps_autoscaling_when_on_demand_is_disabled():
    client = FakeDynamoDBClient()
    handler = build_handler(client, config=FakeConfig(on_demand=False))

    handler.handle_event('msg-1', 'sender', None)

    assert client.updates == []
    assert handler.scaled == [HEALTH_TEST]
    assert handler.tagged == [HEALTH_TEST]
