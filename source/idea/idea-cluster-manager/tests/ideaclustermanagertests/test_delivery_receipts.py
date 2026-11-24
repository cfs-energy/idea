from unittest.mock import Mock

import pytest

from ideaclustermanager.app.notifications.notifications_service import (
    NotificationsService,
)

RECEIPT = '0123456789abcdef0123456789abcdef'
DESKTOP = 'virtual-desktop-controller.session-cleanup-warning'


class Table:
    """a table stub keyed as DynamoDB keys it"""

    def __init__(self):
        self.rows = {}

    def put_item(self, Item):
        self.rows[(Item['pk'], Item['sk'])] = dict(Item)

    def get_item(self, Key, ConsistentRead=False):
        row = self.rows.get((Key['pk'], Key['sk']))
        return {'Item': row} if row else {}


def service(configured=None):
    notifications = NotificationsService.__new__(NotificationsService)
    notifications.context = Mock()
    notifications.context.cluster_name.return_value = 'cluster-marker'
    config = notifications.context.config()
    config.get_module_id.side_effect = lambda module: {
        'scheduler': 'scheduler',
        'virtual-desktop-controller': 'vdc',
    }[module]
    config.get_string.side_effect = lambda key, default=None: (
        configured
        if configured
        and key == 'vdc.dcv_session.notifications.cleanup_warning.email_template'
        else default
    )
    tables = {}
    notifications.context.aws().dynamodb_table().Table.side_effect = lambda name: (
        tables.setdefault(name, Table())
    )
    notifications.logger = Mock()
    return notifications, tables


@pytest.mark.parametrize('configured', [None, 'site.desktop-deletion-notice'])
def test_the_controller_finds_the_receipt_of_a_sent_desktop_warning(configured):
    notifications, tables = service(configured)
    notifications.record_delivery(configured or DESKTOP, {'delivery_receipt': RECEIPT})
    # read as the desktop controller reads it
    found = tables['cluster-marker.vdc.desktop-cleanup'].get_item(
        Key={'pk': 'delivered', 'sk': RECEIPT}, ConsistentRead=True
    )
    assert found.get('Item') is not None
    assert found['Item']['expires_at'] > 0


def test_scratch_cleanup_receipts_still_go_to_the_scheduler():
    notifications, tables = service()
    notifications.record_delivery(
        'scheduler.scratch-cleanup-warning', {'delivery_receipt': RECEIPT}
    )
    assert ('delivered', RECEIPT) in tables[
        'cluster-marker.scheduler.scratch-cleanup'
    ].rows


@pytest.mark.parametrize(
    'template, params',
    [
        ('virtual-desktop-controller.session-ready', {'delivery_receipt': RECEIPT}),
        (DESKTOP, {}),
        (DESKTOP, {'delivery_receipt': 'not-a-receipt'}),
    ],
)
def test_other_emails_record_nothing(template, params):
    notifications, tables = service()
    notifications.record_delivery(template, params)
    assert tables == {}


def test_a_receipt_that_cannot_be_written_is_logged_and_the_email_stands():
    notifications, tables = service()
    table = Mock()
    table.put_item.side_effect = RuntimeError('table missing')
    notifications.context.aws().dynamodb_table().Table.side_effect = lambda name: table
    notifications.record_delivery(DESKTOP, {'delivery_receipt': RECEIPT})
    notifications.logger.exception.assert_called_once()


def test_a_desktop_receipt_outlasts_a_long_warning_window():
    import time

    notifications, tables = service()
    notifications.record_delivery(DESKTOP, {'delivery_receipt': RECEIPT})
    row = tables['cluster-marker.vdc.desktop-cleanup'].rows[('delivered', RECEIPT)]
    assert row['expires_at'] - time.time() > 365 * 24 * 3600


def task_service(config, user=None, user_error=None, send_error=None):
    from types import SimpleNamespace

    from ideadatamodel import User

    notifications, tables = service()
    values = {
        'cluster.ses.enabled': True,
        'cluster-manager.notifications.email.enabled': True,
        **config,
    }
    context_config = notifications.context.config()
    context_config.get_bool.side_effect = lambda key, default=None: values.get(
        key, default
    )
    real_get_string = context_config.get_string.side_effect

    def get_string(key, default=None, required=False):
        if key in ('cluster.ses.sender_email', 'cluster.ses.region'):
            return 'marker'
        return real_get_string(key, default)

    context_config.get_string.side_effect = get_string
    context_config.get_int.side_effect = lambda key, default=None: default
    notifications.accounts = Mock()
    if user_error is not None:
        notifications.accounts.get_user.side_effect = user_error
    else:
        notifications.accounts.get_user.return_value = user or User(
            username='owner', email='owner@example.com', enabled=True
        )
    notifications.email_templates = Mock()
    notifications.email_templates.get_email_template.return_value = SimpleNamespace(
        template=SimpleNamespace(subject='marker', body='marker')
    )
    if send_error is not None:
        notifications.context.aws().ses().send_email.side_effect = send_error
    notifications.notifications_queue_url = 'queue-marker'
    return notifications, tables


def queued(notifications):
    import json

    body = json.dumps(
        {
            'username': 'owner',
            'template_name': DESKTOP,
            'params': {'delivery_receipt': RECEIPT},
        }
    )
    notifications.execute_notifications({'Body': body, 'ReceiptHandle': 'handle'})


def receipt(tables):
    table = tables.get('cluster-marker.vdc.desktop-cleanup')
    return table.rows.get(('delivered', RECEIPT)) if table else None


def test_a_sent_warning_is_recorded_as_sent(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda _: None)
    notifications, tables = task_service({})
    queued(notifications)
    row = receipt(tables)
    assert row['outcome'] == 'sent' and 'reason' not in row


@pytest.mark.parametrize(
    'case, reason',
    [
        ('email-off', 'email-off'),
        ('notifications-off', 'notifications-off'),
        ('owner-disabled', 'owner-disabled'),
        ('no-address', 'no-address'),
        ('owner-not-found', 'owner-not-found'),
    ],
)
def test_a_warning_that_can_never_be_sent_is_recorded_as_such(case, reason):
    import time

    from ideadatamodel import User, errorcodes, exceptions

    kwargs = {
        'email-off': dict(config={'cluster.ses.enabled': False}),
        'notifications-off': dict(
            config={'cluster-manager.notifications.email.enabled': False}
        ),
        'owner-disabled': dict(
            config={},
            user=User(username='owner', email='owner@example.com', enabled=False),
        ),
        'no-address': dict(config={}, user=User(username='owner', enabled=True)),
        'owner-not-found': dict(
            config={},
            user_error=exceptions.soca_exception(
                error_code=errorcodes.AUTH_USER_NOT_FOUND, message='marker'
            ),
        ),
    }[case]
    notifications, tables = task_service(**kwargs)
    queued(notifications)
    row = receipt(tables)
    assert (row['outcome'], row['reason']) == ('cannot-notify', reason)
    assert row['expires_at'] - time.time() > 365 * 24 * 3600


def test_a_send_the_mail_service_refuses_records_nothing():
    notifications, tables = task_service({}, send_error=RuntimeError('throttled'))
    queued(notifications)
    assert receipt(tables) is None


def test_other_templates_record_no_cannot_notify_outcome():
    import json

    notifications, tables = task_service({'cluster.ses.enabled': False})
    body = json.dumps(
        {
            'username': 'owner',
            'template_name': 'virtual-desktop-controller.session-ready',
            'params': {'delivery_receipt': RECEIPT},
        }
    )
    notifications.execute_notifications({'Body': body, 'ReceiptHandle': 'handle'})
    assert tables == {}


def test_a_desktop_template_named_with_the_scheduler_prefix_still_reaches_its_table():
    notifications, tables = service(configured='scheduler.scratch-cleanup-desktops')
    notifications.record_delivery(
        'scheduler.scratch-cleanup-desktops', {'delivery_receipt': RECEIPT}
    )
    assert ('delivered', RECEIPT) in tables['cluster-marker.vdc.desktop-cleanup'].rows
    assert 'cluster-marker.scheduler.scratch-cleanup' not in tables
