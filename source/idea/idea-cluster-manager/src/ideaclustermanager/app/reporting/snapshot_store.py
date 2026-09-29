"""Shared immutable snapshots with application expiry and bound cursors."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import get_args

from botocore.exceptions import ClientError

from ideadatamodel import exceptions
from ideadatamodel.reporting.reporting_api import Column

MAX_ROWS = 10000
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CHUNK_BYTES = 240000
BUILD_SECONDS = 30
TTL_SECONDS = 600
FACETS = ('jobs', 'desktops', 'desktop_disks', 'shared_storage', 'ai')


def fail(code, message):
    raise exceptions.SocaException(error_code=code, message=message)


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        fail('REPORT_TIMEOUT', 'Report build timed out. Retry with a narrower period.')


def too_large():
    fail(
        'REPORT_TOO_LARGE',
        'Report exceeds the supported size. Select a narrower period.',
    )


def encode(value):
    return json.dumps(
        value, default=str, ensure_ascii=False, allow_nan=False, separators=(',', ':')
    )


def column_value(row, column):
    return (
        row.get('spend_by_facet', {}).get(column)
        if column in FACETS
        else row.get(column)
    )


def sort_rows(rows, sort_by, descending):
    if sort_by not in get_args(Column) or type(descending) is not bool:
        raise exceptions.invalid_params('Unsupported sort')
    known, missing = [], []
    for row in sorted(rows, key=lambda item: item['key']):
        (missing if column_value(row, sort_by) is None else known).append(row)
    text_columns = ('key', 'label', 'project_id')
    return (
        sorted(
            known,
            key=lambda row: column_value(row, sort_by)
            if sort_by in text_columns
            else Decimal(str(column_value(row, sort_by))),
            reverse=descending,
        )
        + missing
    )


class SnapshotStore:
    def __init__(self, context):
        self.context = context
        self.table = None

    def initialize(self):
        name = f'{self.context.cluster_name()}.cluster-manager.reporting-snapshots'
        if not self.context.aws_util().dynamodb_check_table_exists(name, True):
            try:
                self.context.aws_util().dynamodb_create_table(
                    create_table_request={
                        'TableName': name,
                        'AttributeDefinitions': [
                            {'AttributeName': key, 'AttributeType': 'S'}
                            for key in ('snapshot_id', 'record')
                        ],
                        'KeySchema': [
                            {'AttributeName': 'snapshot_id', 'KeyType': 'HASH'},
                            {'AttributeName': 'record', 'KeyType': 'RANGE'},
                        ],
                        'BillingMode': 'PAY_PER_REQUEST',
                    },
                    wait=True,
                    ttl=True,
                    ttl_attribute_name='expires_at',
                )
            except ClientError as error:
                if error.response['Error']['Code'] != 'ResourceInUseException':
                    raise
                self.context.aws_util().dynamodb_check_table_exists(name, True)
        self.table = self.context.aws().dynamodb_table().Table(name)

    def publish(self, actor, summary, tables, deadline):
        check_deadline(deadline)
        if set(tables) != {'user', 'project', 'facet'}:
            raise ValueError('All reporting tables are required')
        if any(len(rows) > MAX_ROWS for rows in tables.values()):
            too_large()
        snapshot_id = secrets.token_urlsafe(32)
        expires = int(time.time()) + TTL_SECONDS
        summary = dict(
            summary,
            snapshot_id=snapshot_id,
            expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat(),
        )
        parts = {}
        for table, rows in tables.items():
            parts[table] = 0
            chunk = []
            size = 2
            for row in rows:
                check_deadline(deadline)
                row_size = len(encode(row).encode('utf-8')) + 1
                if row_size > MAX_CHUNK_BYTES:
                    too_large()
                if size + row_size > MAX_CHUNK_BYTES:
                    self._put(snapshot_id, f'{table}:{parts[table]}', chunk, expires)
                    parts[table] += 1
                    chunk, size = [], 2
                chunk.append(row)
                size += row_size
            if chunk:
                self._put(snapshot_id, f'{table}:{parts[table]}', chunk, expires)
                parts[table] += 1
        metadata = dict(
            actor=actor,
            cluster=self.context.cluster_name(),
            expires=expires,
            secret=secrets.token_hex(32),
            summary=summary,
            parts=parts,
        )
        check_deadline(deadline)
        self._put(snapshot_id, 'metadata', metadata, expires)
        check_deadline(deadline)
        return summary

    def _put(self, snapshot_id, record, value, expires):
        payload = encode(value)
        if len(payload.encode('utf-8')) > MAX_CHUNK_BYTES:
            too_large()
        self.table.put_item(
            Item=dict(
                snapshot_id=snapshot_id,
                record=record,
                payload=payload,
                expires_at=expires,
            ),
            ConditionExpression='attribute_not_exists(#r)',
            ExpressionAttributeNames={'#r': 'record'},
        )

    def _get(self, snapshot_id, record):
        item = self.table.get_item(
            Key=dict(snapshot_id=snapshot_id, record=record), ConsistentRead=True
        ).get('Item')
        return json.loads(item['payload']) if item else None

    def lookup(self, snapshot_id, actor, authorize):
        if not authorize():
            raise exceptions.unauthorized_access()
        metadata = self._get(snapshot_id, 'metadata')
        if (
            not metadata
            or metadata['actor'] != actor
            or metadata['cluster'] != self.context.cluster_name()
        ):
            fail(
                'REPORT_SNAPSHOT_NOT_FOUND',
                'Snapshot is unavailable. Create a new report.',
            )
        self.check_expiry(metadata)
        return metadata

    @staticmethod
    def check_expiry(metadata):
        if time.time() >= metadata['expires']:
            fail('REPORT_EXPIRED', 'Snapshot expired. Create a new report.')

    def rows(self, snapshot_id, metadata, table):
        self.check_expiry(metadata)
        if table not in metadata['parts']:
            raise exceptions.invalid_params('Unsupported table')
        rows = []
        for index in range(metadata['parts'][table]):
            chunk = self._get(snapshot_id, f'{table}:{index}')
            if chunk is None:
                fail(
                    'REPORT_SNAPSHOT_NOT_FOUND',
                    'Snapshot is incomplete. Create a new report.',
                )
            rows.extend(chunk)
        self.check_expiry(metadata)
        return rows

    @staticmethod
    def cursor(metadata, binding, offset):
        payload = encode(dict(binding, offset=offset)).encode()
        signature = hmac.new(
            bytes.fromhex(metadata['secret']), payload, hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(signature + payload).decode()

    @staticmethod
    def offset(metadata, binding, cursor):
        if cursor is None:
            return 0
        try:
            decoded = base64.b64decode(cursor, altchars=b'-_', validate=True)
            signature, payload = decoded[:32], decoded[32:]
            expected = hmac.new(
                bytes.fromhex(metadata['secret']), payload, hashlib.sha256
            ).digest()
            value = json.loads(payload)
            offset = value.pop('offset')
            if (
                not hmac.compare_digest(signature, expected)
                or value != binding
                or type(offset) is not int
                or offset < 0
            ):
                raise ValueError()
            return offset
        except (ValueError, KeyError, TypeError):
            raise exceptions.invalid_params('Invalid reporting cursor') from None
