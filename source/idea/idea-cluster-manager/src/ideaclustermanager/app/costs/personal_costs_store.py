"""Durable personal projections. Every read pins one immutable generation."""

import json
import time
import uuid
from datetime import date, datetime
from decimal import Decimal

import arrow
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from ideadatamodel import (
    GetMyCostsResult,
    GetMyCostsSummaryResult,
    GetCostTickerResult,
    exceptions,
    locale,
)

SYSTEM = '!collector'


class PersonalCostsStore:
    def __init__(self, context):
        self.context = context
        self.table = None

    def get_table_name(self):
        return f'{self.context.cluster_name()}.cluster-manager.personal-costs'

    def initialize(self):
        name = self.get_table_name()
        if not self.context.aws_util().dynamodb_check_table_exists(name, True):
            self.context.aws_util().dynamodb_create_table(
                create_table_request={
                    'TableName': name,
                    'AttributeDefinitions': [
                        {'AttributeName': key, 'AttributeType': 'S'}
                        for key in ('subject', 'record')
                    ],
                    'KeySchema': [
                        {'AttributeName': 'subject', 'KeyType': 'HASH'},
                        {'AttributeName': 'record', 'KeyType': 'RANGE'},
                    ],
                    'BillingMode': 'PAY_PER_REQUEST',
                },
                wait=True,
                ttl=True,
                ttl_attribute_name='expires_at',
            )
        self.table = self.context.aws().dynamodb_table().Table(name)

    def get(self, subject, record):
        item = self.table.get_item(
            Key={'subject': subject, 'record': record}, ConsistentRead=True
        ).get('Item')
        return json.loads(item['payload']) if item else None

    @staticmethod
    def encode(value):
        # boto3 hands back Decimal and tz-aware datetime (AttachTime, LaunchTime); store both as JSON scalars.
        def default(item):
            if isinstance(item, Decimal):
                return float(item)
            if isinstance(item, (datetime, date)):
                return item.isoformat()
            raise TypeError(f'Unsupported source value: {type(item).__name__}')

        return json.dumps(
            value,
            default=default,
            allow_nan=False,
            separators=(',', ':'),
            ensure_ascii=True,
        )

    def put(self, subject, record, value, immutable=False, source=False):
        payload = self.encode(value)
        if len(payload.encode()) > 300000:
            raise ValueError('Personal cost record exceeds the bounded item size')
        kwargs = (
            {
                'ConditionExpression': 'attribute_not_exists(#r)',
                'ExpressionAttributeNames': {'#r': 'record'},
            }
            if immutable
            else {}
        )
        item = {'subject': subject, 'record': record, 'payload': payload}
        if source:
            item['expires_at'] = int(time.time()) + 400 * 86400
        self.table.put_item(Item=item, **kwargs)

    def put_source(self, subject, record, value):
        encoded = self.encode(value)
        if len(encoded) <= 200000:
            self.put(subject, record, value, source=True)
            return
        generation = uuid.uuid4().hex
        keys = []
        for index, offset in enumerate(range(0, len(encoded), 100000)):
            key = f'source:{generation}:{index}'
            self.put(
                subject,
                key,
                encoded[offset : offset + 100000],
                immutable=True,
                source=True,
            )
            keys.append(key)
        self.put(subject, record, {'source_chunks': keys}, source=True)

    def resolve_source(self, subject, value):
        if value and 'source_chunks' in value:
            return json.loads(
                ''.join(self.get(subject, key) for key in value['source_chunks'])
            )
        return value

    def records(self, subject, prefix):
        request = {
            'KeyConditionExpression': Key('subject').eq(subject)
            & Key('record').begins_with(prefix),
            'ConsistentRead': True,
        }
        while True:
            page = self.table.query(**request)
            yield from page.get('Items', [])
            if not page.get('LastEvaluatedKey'):
                break
            request['ExclusiveStartKey'] = page['LastEvaluatedKey']

    def publish(self, subject, costs, summary, tickers=None):
        """A failed part write leaves the old head intact; no active record expires."""
        generation = f'{int(time.time() * 1000):013d}-{uuid.uuid4().hex}'
        costs = costs.model_copy(deep=True)
        costs.generation = generation
        payloads = {
            'costs': costs.model_dump(mode='json'),
            'summary': summary.model_dump(mode='json'),
        }
        manifest = {}
        for kind, value in payloads.items():
            # Internal chunks bound large compatibility summaries, without detail APIs.
            encoded = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
            parts = [encoded[i : i + 100000] for i in range(0, len(encoded), 100000)]
            manifest[kind] = len(parts)
            for index, part in enumerate(parts):
                self.put(
                    subject, f'g:{generation}:{kind}:{index}', part, immutable=True
                )
        head = dict(
            generation=generation,
            parts=manifest,
            currency=costs.currency,
            timezone=costs.timezone,
            as_of=costs.refreshed_at,
            total=costs.current.total,
            incomplete=costs.current.incomplete,
            tickers=tickers or {},
        )
        self.put(subject, 'head', head)
        self.prune(subject, generation)
        return head

    def projection(self, subject, head, kind):
        return json.loads(
            ''.join(
                self.get(subject, f'g:{head["generation"]}:{kind}:{i}')
                for i in range(head['parts'][kind])
            )
        )

    def prune(self, subject, current):
        rows = list(self.records(subject, 'g:'))
        generations = sorted(
            {row['record'].split(':')[1] for row in rows}, reverse=True
        )
        keep = set(generations[:2]) | {current}
        cutoff = int((time.time() - 86400) * 1000)
        for row in rows:
            generation = row['record'].split(':')[1]
            if generation not in keep and int(generation.split('-')[0]) < cutoff:
                self.table.delete_item(
                    Key={'subject': subject, 'record': row['record']}
                )

    def request_refresh(self, subject):
        try:
            self.put(
                subject,
                'refresh',
                {'requested_at': arrow.utcnow().isoformat()},
                immutable=True,
            )
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
        return self.get(subject, 'refresh')

    def acknowledge(self, subject, request):
        # A request arriving after the run began is left for the next minute check.
        if request and self.get(subject, 'refresh') == request:
            self.table.delete_item(Key={'subject': subject, 'record': 'refresh'})

    def collecting(self):
        now = arrow.utcnow()
        state = self.get(SYSTEM, 'state') or {}
        next_run = arrow.get(
            state.get('run_started') or state.get('next_run', now.isoformat())
        )
        duration = state.get('last_duration', 1200)
        expected = next_run.shift(seconds=duration)
        heartbeat = (self.get(SYSTEM, 'heartbeat') or {}).get('as_of') or state.get(
            'heartbeat'
        )
        delayed = bool(state) and (
            expected < now
            or not heartbeat
            or (now - arrow.get(heartbeat)).total_seconds() > 180
        )
        if expected < now:
            expected = now.shift(seconds=duration)
        return GetMyCostsResult(
            currency=locale.get_currency_code(),
            state='collecting',
            expected_ready_at=expected.isoformat(),
            collecting_delayed=delayed,
            collecting_reason='Collector overdue; retry estimate shown.'
            if delayed
            else None,
        )


class StoredPersonalCostsService:
    """Request-only facade. No calculator or source clients are constructed here."""

    def __init__(self, context):
        self.context = context

    @property
    def store(self):
        return self.context.personal_costs_store

    @staticmethod
    def authorize(username):
        if not username:
            raise exceptions.unauthorized_access()

    def get_costs(self, username):
        self.authorize(username)
        head = self.store.get(username, 'head')
        result = (
            GetMyCostsResult(**self.store.projection(username, head, 'costs'))
            if head
            else self.store.collecting()
        )
        result.refresh_pending = self.store.get(username, 'refresh') is not None
        if result.refresh_pending and head:
            result.state = 'refreshing'
        return result

    def refresh(self, username):
        self.authorize(username)
        self.store.request_refresh(username)
        result = self.get_costs(username)
        result.refresh_acknowledged = True
        return result

    def get_summary(self, username):
        self.authorize(username)
        head = self.store.get(username, 'head')
        if head:
            return GetMyCostsSummaryResult(
                **self.store.projection(username, head, 'summary')
            )
        return GetMyCostsSummaryResult(username=username, window='last_30_days')

    def get_ticker(self, username):
        self.authorize(username)
        config = self.context.config()
        if not config.get_bool('cluster-manager.web_portal.cost_ticker.enabled', False):
            return GetCostTickerResult(enabled=False)
        period = config.get_string(
            'cluster-manager.web_portal.cost_ticker.period', 'mtd'
        )
        if period not in ('mtd', 'wtd', 'qtd', 'ytd'):
            period = 'mtd'
        head = self.store.get(username, 'head')
        if not head:
            return GetCostTickerResult(enabled=True, period=period.upper())
        values = head if period == 'mtd' else head.get('tickers', {}).get(period, {})
        return GetCostTickerResult(
            enabled=True,
            period=period.upper(),
            total=values.get('total'),
            incomplete=values.get('incomplete', True),
            currency=head['currency'],
            as_of=head['as_of'],
        )
