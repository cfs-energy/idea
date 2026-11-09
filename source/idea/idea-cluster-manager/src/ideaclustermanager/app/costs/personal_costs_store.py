"""Durable personal projections. Every read pins one immutable generation."""

import json
import time
import uuid
from datetime import date, datetime
from decimal import Decimal

import arrow
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from ideadatamodel import (
    GetMyCostsResult,
    GetMyCostsSummaryResult,
    GetCostTickerResult,
    exceptions,
    locale,
)

SYSTEM = '!collector'
# One empty record per subject that has ever had a head (a head never expires, and a
# deleted account's stays readable) or has desktop history, which may have no head at all.
# Readers scan until the marker says it is complete.
HEAD_SUBJECT = 'head-subject:'
HEAD_SUBJECTS_SEEDED = 'head-subjects-seeded'
SEED_CHECKPOINT = 'seed-head-subjects:'
SEED_SEGMENTS = 8
SEED_BUDGET_SECONDS = 120


def window_storage(costs, start_date, end_date):
    """
    the stored daily storage amounts in a window, and the bytes behind the current month.
    a day with no stored amount adds nothing; no amount at all is unknown, not zero.
    """
    # ponytail: only this month and last are stored, so on march 1 the window's
    # first day (january 31) is never counted; read older generations if it matters.
    amounts = [
        day.amount
        for month in (costs.previous, costs.current)
        if month is not None
        for day in month.shared_storage.daily
        if start_date <= day.date <= end_date and day.amount is not None
    ]
    if not amounts:
        return None, None
    month = costs.current
    return round(sum(amounts), 4), round(
        sum(item.used_bytes or 0 for item in (month.storage if month else []))
        / (1024**3),
        2,
    )


class PersonalCostsStore:
    def __init__(self, context):
        self.context = context
        self.table = None
        self._head_subjects = None

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

    def records(self, subject, prefix, last=None):
        """records under a prefix, or between prefix and last inclusive when last is given."""
        record = (
            Key('record').begins_with(prefix)
            if last is None
            else Key('record').between(prefix, last)
        )
        request = {
            'KeyConditionExpression': Key('subject').eq(subject) & record,
            'ConsistentRead': True,
        }
        while True:
            page = self.table.query(**request)
            yield from page.get('Items', [])
            if not page.get('LastEvaluatedKey'):
                break
            request['ExclusiveStartKey'] = page['LastEvaluatedKey']

    def publish(self, subject, costs, summary, tickers=None, listing=None):
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
        self.register_head_subject(subject)
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
        if listing:
            head['listing'] = listing
        self.put(subject, 'head', head)
        self.prune(subject, generation)
        return head

    def _known_head_subjects(self):
        if self._head_subjects is None:
            self._head_subjects = {
                row['record'][len(HEAD_SUBJECT) :]
                for row in self.records(SYSTEM, HEAD_SUBJECT)
            }
        return self._head_subjects

    def register_head_subject(self, subject):
        # written before the head, so a head is never missing from the index
        if subject not in self._known_head_subjects():
            self.put(SYSTEM, HEAD_SUBJECT + subject, {})
            self._head_subjects.add(subject)

    def seed_head_subjects(
        self, on_progress=lambda: None, budget_seconds=SEED_BUDGET_SECONDS
    ):
        """
        once per table: every head written before the index existed. On a table grown under an
        earlier release this outlasts any run, so it spends a budget per run and each segment
        records where it reached. True once it finishes; readers scan until then.
        """
        if self.get(SYSTEM, HEAD_SUBJECTS_SEEDED):
            return False
        deadline = time.monotonic() + budget_seconds
        for segment in range(SEED_SEGMENTS):
            mark = f'{SEED_CHECKPOINT}{segment}'
            reached = self.get(SYSTEM, mark) or {}
            if reached.get('done'):
                continue
            request = dict(
                ConsistentRead=True,
                Segment=segment,
                TotalSegments=SEED_SEGMENTS,
                FilterExpression=Attr('record').eq('head'),
            )
            if reached.get('last_key'):
                request['ExclusiveStartKey'] = reached['last_key']
            while True:
                if time.monotonic() >= deadline:
                    return False
                page = self.table.scan(**request)
                for row in page.get('Items', []):
                    self.register_head_subject(row['subject'])
                last_key = page.get('LastEvaluatedKey')
                self.put(
                    SYSTEM, mark, {'last_key': last_key} if last_key else {'done': True}
                )
                on_progress()
                if not last_key:
                    break
                request['ExclusiveStartKey'] = last_key
        self.put(SYSTEM, HEAD_SUBJECTS_SEEDED, {'as_of': arrow.utcnow().isoformat()})
        for segment in range(SEED_SEGMENTS):
            self.table.delete_item(
                Key={'subject': SYSTEM, 'record': f'{SEED_CHECKPOINT}{segment}'}
            )
        return True

    def head_subjects(self):
        """every subject with a head, or None until the collector has seeded the index"""
        rows = list(self.records(SYSTEM, 'head-subject'))
        names = [row['record'] for row in rows]
        if HEAD_SUBJECTS_SEEDED not in names:
            return None
        return [
            name[len(HEAD_SUBJECT) :] for name in names if name.startswith(HEAD_SUBJECT)
        ]

    def batch_heads(self, subjects, check=lambda: None):
        """the head records of these subjects; a subject without one returns nothing"""
        keys = [{'subject': name, 'record': 'head'} for name in sorted(set(subjects))]
        name = self.table.name
        rows = []
        for offset in range(0, len(keys), 100):
            request = {
                name: {'Keys': keys[offset : offset + 100], 'ConsistentRead': True}
            }
            for attempt in range(8):
                check()
                if attempt:
                    time.sleep(min(0.05 * 2**attempt, 1))
                response = (
                    self.context.aws()
                    .dynamodb_table()
                    .batch_get_item(RequestItems=request)
                )
                rows.extend(response.get('Responses', {}).get(name, []))
                request = response.get('UnprocessedKeys') or {}
                if not request.get(name, {}).get('Keys'):
                    break
            else:
                # a short answer here would read as users without costs
                raise RuntimeError('Personal cost heads left unprocessed')
        return rows

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
        # a reader pins a generation for seconds and a report snapshot copies what it reads,
        # so an hour covers every reader. each refresh writes a generation.
        cutoff = int((time.time() - 3600) * 1000)
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
        # the seed runs inside a run and last_duration comes from runs without it, so its
        # time moves the estimate, up to its budget: a seed stuck past that is overdue
        seed = state.get('seed_seconds', 0)
        if state.get('seeding_since'):
            seed = (now - arrow.get(state['seeding_since'])).total_seconds()
        expected = next_run.shift(seconds=duration + min(seed, SEED_BUDGET_SECONDS))
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

    def listing_heads(self):
        """subject -> head or None for the admin listing; None until the index is seeded"""
        subjects = self.store.head_subjects()
        if subjects is None:
            return None
        # a subject with desktop history and no head is listed too: None, nothing stored
        heads = dict.fromkeys(subjects)
        heads.update(
            (row['subject'], json.loads(row['payload']))
            for row in self.store.batch_heads(subjects)
        )
        return heads

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
