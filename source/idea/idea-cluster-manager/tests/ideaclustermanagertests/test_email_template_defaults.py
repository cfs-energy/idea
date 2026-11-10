"""
Default email templates reach a cluster once: a stored template is never overwritten, since
an administrator may have edited it. A fix to a shipped template therefore never arrived.
Seen on a cluster that stored the scratch admin summary of an earlier build: every later
build kept the stored text. A stored text that is an earlier default word for word was not
edited, and takes the fix.
"""

import logging
import re
from pathlib import Path

import pytest
import yaml

import ideaclustermanager.app.email_templates.email_templates_service as module
from ideaclustermanager.app.email_templates.email_templates_dao import EmailTemplatesDAO
from ideaclustermanager.app.email_templates.email_templates_service import (
    EmailTemplatesService,
    default_text_hash,
)

DEFAULTS = Path(__file__).parents[2] / 'resources/defaults/email_templates.yml'
NAME = 'scheduler.scratch-cleanup-admin-summary'
# the row a cluster still stored two builds after the fix, as DynamoDB returned it
STORED_SUBJECT = '[{{cluster_name}}] Scratch cleanup this week on {{file_system}}'
STORED_BODY = (
    '<b>{{scheduled_folders}}</b> folders are scheduled for removal, and '
    '{{not_armed_folders}} are waiting for a backfill. {{removed_count}} folders '
    '({{removed_gb}} GB) went this week.<br>\n'
    '{% if waiting_message %}<br>{{waiting_message}}<br>{% endif %}\n'
    '{% if failures %}\n<h3>What stopped</h3>\n<ul>\n{% for failure in failures %}\n'
    '    <li> {{failure}}</li>\n{% endfor %}\n</ul>\n'
    '{% if more_failures %}And {{more_failures}} more.<br>{% endif %}\n{% endif %}\n'
    '{% if page_url %}<a href="{{page_url}}">Open Scratch cleanup</a>{% endif %}\n'
    '<hr>\n<i> This is an automated email, please do not respond.</i>\n'
)


class Table:
    def __init__(self, rows):
        self.rows = {row['name']: dict(row) for row in rows}
        self.updated = []

    def get_item(self, Key):
        row = self.rows.get(Key['name'])
        return {'Item': dict(row)} if row else {}

    def put_item(self, Item):
        self.rows[Item['name']] = dict(Item)

    def update_item(
        self, Key, ExpressionAttributeNames, ExpressionAttributeValues, **_
    ):
        self.updated.append(Key['name'])
        row = self.rows[Key['name']]
        for token, key in ExpressionAttributeNames.items():
            row[key] = ExpressionAttributeValues[':' + token[1:]]
        return {'Attributes': dict(row)}


def shipped():
    templates = yaml.safe_load(DEFAULTS.read_text())['templates']
    return {template['name']: template for template in templates}


def stored_row(**fields):
    row = {
        'name': NAME,
        'title': 'Weekly Scratch Cleanup Summary',
        'template_type': 'jinja2',
        'subject': STORED_SUBJECT,
        'body': STORED_BODY,
        'created_on': 1794234324964,
        'updated_on': 1794234324964,
    }
    row.update(fields)
    return row


@pytest.fixture()
def service(monkeypatch):
    monkeypatch.setattr(
        module.ClusterManagerUtils,
        'get_email_template_defaults_file',
        staticmethod(lambda: str(DEFAULTS)),
    )

    def build(rows):
        service = EmailTemplatesService.__new__(EmailTemplatesService)
        service.logger = logging.getLogger('test_email_template_defaults')
        service.email_templates_dao = EmailTemplatesDAO.__new__(EmailTemplatesDAO)
        service.email_templates_dao.table = Table(rows)
        return service, service.email_templates_dao.table

    return build


def test_an_unedited_earlier_default_takes_the_shipped_text(service):
    service, table = service([stored_row(title='Our own title')])
    service.create_defaults()
    row = table.rows[NAME]
    assert row['body'] == shipped()[NAME]['body']
    assert 'not_armed_folders' not in row['body']
    assert row['subject'] == shipped()[NAME]['subject']
    # only the text that was compared is replaced
    assert row['title'] == 'Our own title'
    assert row['created_on'] == 1794234324964
    assert row['updated_on'] > 1794234324964
    assert table.updated == [NAME]
    service.create_defaults()
    assert table.updated == [NAME], 'the second start finds nothing to do'


@pytest.mark.parametrize(
    'edit',
    [
        dict(body=STORED_BODY.replace('went this week', 'were removed this week')),
        dict(body=STORED_BODY + '\n'),
        dict(subject='[{{cluster_name}}] Scratch cleanup'),
    ],
)
def test_an_edited_template_is_left_alone(service, edit):
    service, table = service([stored_row(**edit)])
    service.create_defaults()
    assert table.updated == []
    assert table.rows[NAME] == stored_row(**edit)


def test_a_missing_template_is_created_and_every_other_is_untouched(service):
    current = shipped()['scheduler.job-started']
    kept = dict(current, body='ours', created_on=1, updated_on=1)
    kept.pop('superseded', None)
    service, table = service([kept])
    service.create_defaults()
    assert set(table.rows) == set(shipped())
    assert table.rows['scheduler.job-started'] == kept
    assert table.updated == []
    assert table.rows[NAME]['body'] == shipped()[NAME]['body']
    assert all('superseded' not in row for row in table.rows.values())


def test_the_superseded_lists_are_hashes_of_earlier_texts_only():
    assert (
        default_text_hash(STORED_SUBJECT, STORED_BODY) in shipped()[NAME]['superseded']
    )
    for name, template in shipped().items():
        for value in template.get('superseded') or []:
            assert re.fullmatch('[0-9a-f]{64}', value), name
            assert value != default_text_hash(template['subject'], template['body'])
