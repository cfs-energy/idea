"""Scratch cleanup counts a digest as told only from the receipt written once SES takes it."""

from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from ideadatamodel import Notification
from ideaclustermanager.app.notifications.notifications_service import (
    NotificationsService,
)

SENDER = 'cluster@example.com'
TABLE = 'idea-test.scheduler.scratch-cleanup'
RECEIPT = 'ab' * 16


class Config:
    values = {
        'cluster-manager.notifications_queue_url': 'queue',
        'cluster.ses.enabled': True,
        'cluster.ses.sender_email': SENDER,
        'cluster.ses.region': 'us-east-1',
        'cluster.ses.max_sending_rate': 1000,
        'cluster-manager.notifications.email.enabled': True,
    }

    def get_string(self, key, default=None, required=False):
        return self.values.get(key, default)

    get_bool = get_int = get_string

    def get_module_id(self, name):
        return name


@pytest.fixture()
def mail(monkeypatch):
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    with mock_aws():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        table = dynamodb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {'AttributeName': 'pk', 'AttributeType': 'S'},
                {'AttributeName': 'sk', 'AttributeType': 'S'},
            ],
            KeySchema=[
                {'AttributeName': 'pk', 'KeyType': 'HASH'},
                {'AttributeName': 'sk', 'KeyType': 'RANGE'},
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        ses = boto3.client('ses', region_name='us-east-1')
        context = SimpleNamespace(
            logger=lambda name: Mock(),
            config=lambda: Config(),
            aws=lambda: SimpleNamespace(
                ses=lambda region_name: ses, dynamodb_table=lambda: dynamodb
            ),
            cluster_name=lambda: 'idea-test',
            service_registry=lambda: Mock(),
        )
        service = NotificationsService(
            context,
            accounts=SimpleNamespace(
                get_user=lambda username: SimpleNamespace(
                    enabled=True, email=f'{username}@example.com'
                )
            ),
            email_templates=SimpleNamespace(
                get_email_template=lambda request: SimpleNamespace(
                    template=SimpleNamespace(subject='s', body='b')
                )
            ),
        )
        yield SimpleNamespace(service=service, ses=ses, table=table)


def receipts(mail):
    return mail.table.scan()['Items']


def send(mail, template='scheduler.scratch-cleanup-digest', receipt=RECEIPT):
    mail.service.send_email(
        Notification(
            username='alice',
            template_name=template,
            params={'delivery_receipt': receipt},
        )
    )


def test_a_digest_ses_takes_leaves_a_receipt(mail):
    mail.ses.verify_email_identity(EmailAddress=SENDER)
    send(mail)
    [row] = receipts(mail)
    assert row['pk'] == 'delivered' and row['sk'] == RECEIPT and row['at']


def test_a_digest_ses_refuses_leaves_none(mail):
    send(mail)
    assert receipts(mail) == []


@pytest.mark.parametrize(
    'template, receipt',
    [
        ('scheduler.job-completed', RECEIPT),
        ('scheduler.scratch-cleanup-digest', 'not-a-receipt'),
        ('scheduler.scratch-cleanup-digest', {'pk': 'lease'}),
    ],
    ids=['another template', 'odd receipt', 'not a string'],
)
def test_nothing_else_writes_to_the_cleanup_table(mail, template, receipt):
    mail.ses.verify_email_identity(EmailAddress=SENDER)
    send(mail, template, receipt)
    assert receipts(mail) == []


def mail_with(mail, subject, body, params):
    mail.service.email_templates = SimpleNamespace(
        get_email_template=lambda request: SimpleNamespace(
            template=SimpleNamespace(subject=subject, body=body)
        )
    )
    sent = []
    mail.service.context.aws = lambda: SimpleNamespace(
        ses=lambda region_name: SimpleNamespace(
            send_email=lambda **kwargs: sent.append(kwargs['Message'])
        )
    )
    mail.service.send_email(
        Notification(
            username='alice',
            template_name='virtual-desktop-controller.x',
            params=params,
        )
    )
    return sent


def test_a_value_with_markup_arrives_as_text_in_the_html_body(mail):
    sent = mail_with(
        mail,
        'Desktop {{session.name}}',
        '<b>{{session.name}}</b> is ready',
        {'session': {'name': '<i>mine</i> & yours'}},
    )
    assert sent[0]['Subject']['Data'] == 'Desktop <i>mine</i> & yours'
    assert sent[0]['Body']['Html']['Data'] == (
        '<b>&lt;i&gt;mine&lt;/i&gt; &amp; yours</b> is ready'
    )


def test_a_template_reaching_past_its_values_fails_only_its_email(mail):
    sent = mail_with(
        mail, 'subject', '{{ cluster_name.__class__ }}', {'cluster_name': 'c'}
    )
    assert sent == []
    mail.service.logger.exception.assert_called_once()
