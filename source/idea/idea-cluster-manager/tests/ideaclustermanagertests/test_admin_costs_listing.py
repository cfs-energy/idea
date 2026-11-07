"""The admin listing reads the figures the collector stored and measures only what can
have moved, and answers exactly what measuring everything answers."""

from types import SimpleNamespace
from unittest.mock import Mock

import arrow
import pytest

from ideadatamodel import (
    GetMyCostsResult,
    MyCostsAmount,
    MyCostsDaily,
    MyCostsMonth,
    MyCostsStorageShare,
)
from ideaclustermanager.app.api.costs_api import CostsAPI
from ideaclustermanager.app.costs import my_costs_service, personal_costs_store
from ideaclustermanager.app.costs.my_costs_service import MyCostsService
from ideaclustermanager.app.costs.personal_costs_collector import (
    FACETS,
    PersonalCostsCollector,
    listing_entry,
)
from ideaclustermanager.app.costs.personal_costs_store import (
    HEAD_SUBJECTS_SEEDED,
    PersonalCostsStore,
    SYSTEM,
)
from ideaclustermanagertests.test_costs_api import FakeApiInvocationContext
from ideaclustermanagertests.test_personal_costs_collector import Table
from ideaclustermanagertests.test_reporting_service import PeriodIndex

NOW = arrow.get('2026-11-15T12:00:00Z')
HOUR = 3600000
DAY = 24 * HOUR


def ms(moment):
    return moment.int_timestamp * 1000


class SessionIndex(PeriodIndex):
    def __init__(self, docs):
        super().__init__(docs)
        self.document_reads = 0
        self.change_reads = 0

    def search(self, index, body, **kwargs):
        if index.endswith('_jobs'):
            return {
                'hits': {'total': {'value': 0}, 'hits': []},
                'aggregations': {'by_user': {'buckets': []}},
            }
        if 'aggs' not in body:
            self.document_reads += 1
            matched = [d for d in self.docs if self.matches(body['query'], d)]
            matched.sort(key=lambda d: d['created_on'], reverse=True)
            return {'hits': {'hits': [{'_source': d} for d in matched[: body['size']]]}}
        self.change_reads += 1
        latest = {}
        for doc in self.docs:
            if self.matches(body['query'], doc):
                latest[doc['owner']] = max(
                    latest.get(doc['owner'], 0), doc['updated_on']
                )
        buckets = [
            dict(key=dict(owner=owner), latest=dict(value=value))
            for owner, value in latest.items()
        ]
        composite = body['aggs']['owners']['composite']
        if composite.get('after'):
            return {'aggregations': {'owners': {'buckets': []}}}
        return {
            'aggregations': {
                'owners': {'buckets': buckets, 'after_key': {'owner': 'z'}}
            }
        }


def session(identity, owner, state, created, updated, **fields):
    return dict(
        idea_session_id=identity,
        name=identity,
        owner=owner,
        base_os='linux',
        state=state,
        created_on=ms(created),
        updated_on=ms(updated),
        server=dict(instance_type='m5.large', instance_id=f'i-{identity}'),
        **fields,
    )


def run(identity, owner, started, stopped=None):
    return dict(
        owner=owner,
        run_of=identity,
        started_on=ms(started),
        **({'stopped_on': ms(stopped)} if stopped else {}),
    )


def world():
    now = NOW
    docs = [
        # stopped days ago: the stored figure is the answer
        session(
            'settled',
            'settled-user',
            'STOPPED',
            now.shift(days=-5),
            now.shift(days=-4),
            stopped_on=ms(now.shift(days=-4)),
        ),
        # running: it moves every minute
        session('live', 'live-user', 'READY', now.shift(days=-2), now.shift(days=-2)),
        # a former account's last desktop, inside the window
        session(
            'former',
            'former-user',
            'STOPPED',
            now.shift(days=-20),
            now.shift(days=-19),
            stopped_on=ms(now.shift(days=-19)),
        ),
        # never had a head at all
        session(
            'headless',
            'headless-user',
            'STOPPED',
            now.shift(days=-3),
            now.shift(days=-2),
            stopped_on=ms(now.shift(days=-2)),
        ),
    ]
    history = [
        run('settled', 'settled-user', now.shift(days=-5), now.shift(days=-4)),
        run('live', 'live-user', now.shift(days=-2)),
        run('former', 'former-user', now.shift(days=-20), now.shift(days=-19)),
        run('headless', 'headless-user', now.shift(days=-3), now.shift(days=-2)),
        # terminated in the window: only the history table remembers it
        dict(
            owner='deleted-user',
            session_id='s1',
            idea_session_id='deleted',
            name='deleted',
            base_os='linux',
            created_on=ms(now.shift(days=-9)),
            deleted_on=ms(now.shift(days=-8)),
            stopped_on=ms(now.shift(days=-8)),
            instance_type='m5.large',
            instance_id='i-deleted',
        ),
        run('deleted', 'deleted-user', now.shift(days=-9), now.shift(days=-8)),
        # a run from long ago: the old listing gives this owner a row of zeros
        run('ancient', 'zero-user', now.shift(days=-300), now.shift(days=-299)),
    ]
    return docs, history


def month(start, end, amount):
    days = [
        MyCostsDaily(
            date=day.format('YYYY-MM-DD'), day=day.day, amount=amount, status='ready'
        )
        for day in arrow.Arrow.range('day', start, end)
    ]
    lines = {
        facet: MyCostsAmount(
            cost=amount, status='ready', daily=days if facet == 'shared_storage' else []
        )
        for facet in FACETS
    }
    return MyCostsMonth(
        start_date=start.format('YYYY-MM-DD'),
        end_date=end.format('YYYY-MM-DD'),
        total=amount,
        incomplete=False,
        storage=[MyCostsStorageShare(filesystem='data', used_bytes=2 * 1024**3)],
        **lines,
    )


def costs_for(username):
    # one user with storage and one known zero; the others have no stored amount
    amount = {'settled-user': 0.25, 'live-user': 0.0}.get(username)
    return GetMyCostsResult(
        currency='USD',
        state='ready',
        current=month(arrow.get('2026-11-01'), arrow.get('2026-11-15'), amount),
        previous=month(arrow.get('2026-10-01'), arrow.get('2026-10-31'), amount),
        refreshed_at=NOW.isoformat(),
    )


class Counts(dict):
    def hit(self, key):
        self[key] = self.get(key, 0) + 1


def build(monkeypatch, docs, history):
    monkeypatch.setattr(arrow, 'utcnow', lambda: NOW)
    monkeypatch.setattr('ideadatamodel.locale.get_currency_code', lambda: 'USD')
    my_costs_service._cpu_hours_cache.clear()
    counts = Counts()
    index = SessionIndex(docs)

    def query(KeyConditionExpression):
        counts.hit('session-history Query')
        owner = KeyConditionExpression.get_expression()['values'][1]
        return {'Items': [r for r in history if r['owner'] == owner]}

    def scan(**kwargs):
        counts.hit('session-history Scan')
        return {'Items': list(history)}

    def metric_data(**kwargs):
        counts.hit('GetMetricData')
        return {'MetricDataResults': []}

    table = Table()
    table.name = 'costs'

    def batch_get_item(RequestItems):
        counts.hit('personal-costs BatchGetItem')
        keys = RequestItems['costs']['Keys']
        return {
            'Responses': {
                'costs': [
                    table.rows[(k['subject'], k['record'])]
                    for k in keys
                    if (k['subject'], k['record']) in table.rows
                ]
            }
        }

    resource = SimpleNamespace(
        Table=lambda name: SimpleNamespace(scan=scan, query=query),
        batch_get_item=batch_get_item,
    )
    context = SimpleNamespace(
        cluster_name=lambda: 'test',
        logger=lambda *args: Mock(),
        module_id=lambda: 'cluster-manager',
        config=lambda: SimpleNamespace(
            get_string=lambda key, default=None, **kwargs: (
                'user_sessions' if key.endswith('dcv_session.alias') else default
            ),
            get_bool=lambda *args, **kwargs: False,
            is_module_enabled=lambda module: True,
            get_module_id=lambda module: 'module',
        ),
        analytics_service=lambda: SimpleNamespace(
            os_client=SimpleNamespace(os_client=index)
        ),
        aws=lambda: SimpleNamespace(
            dynamodb_table=lambda: resource,
            cloudwatch=lambda: SimpleNamespace(get_metric_data=metric_data),
        ),
        aws_util=lambda: SimpleNamespace(
            get_ec2_instance_type_unit_price=lambda kind: SimpleNamespace(ondemand=0.1)
        ),
        projects=SimpleNamespace(
            projects_dao=SimpleNamespace(
                list_projects=lambda request: SimpleNamespace(
                    listing=[], paginator=None
                )
            ),
            get_user_projects=lambda request: SimpleNamespace(projects=[]),
        ),
        storage_metrics=Mock(),
    )
    context.storage_metrics.configuration_status.return_value = dict(status='enabled')
    context.storage_metrics.is_enabled.return_value = True
    context.storage_metrics.targets.return_value = [object()]
    store = PersonalCostsStore(context)
    store.table = table
    store.put(SYSTEM, HEAD_SUBJECTS_SEEDED, {'as_of': NOW.isoformat()})
    context.personal_costs_store = store
    get_item = table.get_item

    def counted_get(Key, ConsistentRead):
        counts.hit('personal-costs GetItem')
        return get_item(Key=Key, ConsistentRead=ConsistentRead)

    table.get_item = counted_get
    return context, store, counts, index


def collect(context, store, usernames):
    for username in usernames:
        reader = MyCostsService(context)
        summary = reader.get_summary(username)
        costs = costs_for(username)
        store.publish(
            username,
            costs,
            summary,
            listing=listing_entry(
                summary,
                reader.desktop_records,
                costs,
                arrow.utcnow(),
                reader.desktops_complete,
            ),
        )


def view(context, heads='stored'):
    api = CostsAPI(context)
    if heads is None:
        api.monthly_costs.listing_heads = lambda: None
    call = FakeApiInvocationContext('Costs.ListUserCosts', 'admin', elevated=True)
    api.list_user_costs(call)
    return call.response_payload


def rows(result):
    payload = result.model_dump()
    payload['listing'] = sorted(payload['listing'], key=lambda row: row['username'])
    return payload


COLLECTED = (
    'settled-user',
    'live-user',
    'deleted-user',
    'zero-user',
    'idle-user',
    'quiet-a',
    'quiet-b',
    'quiet-c',
    'quiet-d',
)


def test_stored_listing_matches_measuring_everything_as_time_moves_on(monkeypatch):
    docs, history = world()
    context, store, counts, index = build(monkeypatch, docs, history)
    collect(context, store, COLLECTED + ('former-user',))
    # the account is deleted and a later window has gone by, then ten quiet minutes
    former = store.get('former-user', 'head')
    former['listing']['start_date'] = '2026-09-01'
    store.put('former-user', 'head', former)
    moved = NOW.shift(minutes=10)
    monkeypatch.setattr(arrow, 'utcnow', lambda: moved)
    counts.clear()
    expected = view(context, heads=None)
    assert counts.get('session-history Scan') == 1
    counts.clear()
    index.document_reads = 0
    actual = view(context)
    assert rows(actual) == rows(expected)

    by_name = {row.username: row for row in expected.listing}
    # not vacuous: every case is in the answer and has the value that makes it one
    assert by_name['settled-user'].desktop_hours == pytest.approx(24)
    assert by_name['settled-user'].storage_cost == pytest.approx(30 * 0.25)
    assert by_name['live-user'].desktop_hours == pytest.approx(48 + 1 / 6, abs=0.001)
    assert by_name['live-user'].storage_cost == 0.0
    assert by_name['former-user'].desktop_hours == pytest.approx(24)
    assert by_name['headless-user'].desktop_hours == pytest.approx(24)
    assert by_name['deleted-user'].desktop_hours == pytest.approx(24)
    assert by_name['zero-user'].desktop_hours == 0
    assert 'idle-user' not in by_name

    # nothing scanned, and only the users whose figure can have moved are read again
    assert 'session-history Scan' not in counts
    assert counts['session-history Query'] == 3  # live, former, headless
    assert index.document_reads == 1
    # the users without a stored window: former-user and headless-user
    assert counts.get('personal-costs GetItem', 0) <= 8


def test_a_desktop_started_after_the_head_is_measured_now(monkeypatch):
    docs, history = world()
    context, store, counts, _ = build(monkeypatch, docs, history)
    collect(context, store, COLLECTED + ('former-user', 'headless-user'))
    started = NOW.shift(minutes=5)
    docs.append(session('new', 'quiet-a', 'READY', started, started))
    history.append(run('new', 'quiet-a', started))
    monkeypatch.setattr(arrow, 'utcnow', lambda: NOW.shift(minutes=10))
    expected = rows(view(context, heads=None))
    actual = rows(view(context))
    assert actual == expected
    quiet = next(r for r in actual['listing'] if r['username'] == 'quiet-a')
    assert quiet['desktop_hours'] == pytest.approx(5 / 60, abs=0.001)


def test_many_live_desktops_cost_one_search_and_one_metric_read(monkeypatch):
    docs, history = world()
    live = [f'live-{i:02d}' for i in range(20)]
    # plenty of quiet users, so the listing measures only the live ones
    quiet = [f'quiet-{i:02d}' for i in range(40)]
    start = NOW.shift(days=-2)
    for name in live:
        # its first hours have no run: only the CPU metric can say how long it ran
        docs.append(session(name, name, 'READY', start, start))
        history.append(run(name, name, start.shift(hours=2)))
    for name in quiet:
        docs.append(
            session(
                name,
                name,
                'STOPPED',
                NOW.shift(days=-10),
                NOW.shift(days=-9),
                stopped_on=ms(NOW.shift(days=-9)),
            )
        )
        history.append(run(name, name, NOW.shift(days=-10), NOW.shift(days=-9)))
    context, store, counts, index = build(monkeypatch, docs, history)
    collect(context, store, live + quiet)
    monkeypatch.setattr(arrow, 'utcnow', lambda: NOW.shift(minutes=10))
    my_costs_service._cpu_hours_cache.clear()
    counts.clear()
    index.document_reads = 0
    result = rows(view(context))
    assert index.document_reads == 1
    assert counts['GetMetricData'] == 1
    assert {r['username'] for r in result['listing']} >= set(live)


def many_stopped_desktops(owner, count):
    # two index generations of each desktop: the summary's 200 documents are 100 desktops
    start = NOW.shift(days=-5)
    return [
        session(
            f'many-{i}',
            owner,
            'STOPPED',
            start.shift(minutes=i),
            start.shift(hours=1, minutes=i, seconds=generation),
            stopped_on=ms(start.shift(hours=1, minutes=i)),
        )
        for i in range(count)
        for generation in (0, 1)
    ], [
        run(f'many-{i}', owner, start.shift(minutes=i), start.shift(hours=1, minutes=i))
        for i in range(count)
    ]


def test_a_truncated_summary_is_never_the_listing_total(monkeypatch):
    docs, history = many_stopped_desktops('busy-user', 101)
    context, store, counts, _ = build(monkeypatch, docs, history)
    collect(context, store, ('busy-user', 'quiet-a', 'quiet-b', 'quiet-c', 'quiet-d'))
    listing = store.get('busy-user', 'head')['listing']
    # the summary itself stops at its cap and has to say so
    assert listing['session_count'] == 100
    assert listing['complete'] is False
    monkeypatch.setattr(arrow, 'utcnow', lambda: NOW.shift(minutes=10))
    actual = rows(view(context))
    assert actual['listing'][0]['username'] == 'busy-user'
    assert actual['listing'][0]['desktop_session_count'] == 101


def test_a_summary_under_the_cap_is_complete(monkeypatch):
    docs, history = many_stopped_desktops('busy-user', 99)
    context, store, counts, _ = build(monkeypatch, docs, history)
    collect(context, store, ('busy-user',))
    listing = store.get('busy-user', 'head')['listing']
    assert listing['session_count'] == 99
    assert listing['complete'] is True


def test_a_figure_stored_before_completeness_was_recorded_is_measured(monkeypatch):
    docs, history = many_stopped_desktops('busy-user', 101)
    context, store, counts, _ = build(monkeypatch, docs, history)
    collect(context, store, ('busy-user', 'quiet-a', 'quiet-b', 'quiet-c', 'quiet-d'))
    head = store.get('busy-user', 'head')
    del head['listing']['complete']
    store.put('busy-user', 'head', head)
    monkeypatch.setattr(arrow, 'utcnow', lambda: NOW.shift(minutes=10))
    actual = rows(view(context))
    assert actual['listing'][0]['desktop_session_count'] == 101


def test_a_stale_window_reads_everything_and_says_the_same(monkeypatch):
    docs, history = world()
    context, store, counts, _ = build(monkeypatch, docs, history)
    collect(context, store, COLLECTED)
    # midnight: every head is for yesterday's window until the collector's next run
    monkeypatch.setattr(arrow, 'utcnow', lambda: NOW.shift(hours=13))
    expected = rows(view(context, heads=None))
    counts.clear()
    assert rows(view(context)) == expected
    assert counts['session-history Scan'] == 1


def test_no_index_yet_reads_everything(monkeypatch):
    docs, history = world()
    context, store, counts, _ = build(monkeypatch, docs, history)
    collect(context, store, COLLECTED)
    store.table.delete_item(Key=dict(subject=SYSTEM, record=HEAD_SUBJECTS_SEEDED))
    counts.clear()
    assert rows(view(context)) == rows(view(context, heads=None))
    assert counts['session-history Scan'] == 2


def test_a_failed_change_read_reads_everything(monkeypatch):
    docs, history = world()
    context, store, counts, sessions = build(monkeypatch, docs, history)
    collect(context, store, COLLECTED + ('former-user', 'headless-user'))
    expected = rows(view(context, heads=None))
    search = sessions.search

    def failing(index, body, **kwargs):
        if 'aggs' in body and 'owners' in body['aggs']:
            raise RuntimeError('search down')
        return search(index, body, **kwargs)

    sessions.search = failing
    counts.clear()
    assert rows(view(context)) == expected
    assert counts['session-history Scan'] == 1


def test_a_head_listing_stays_inside_one_write_unit(monkeypatch):
    docs, history = world()
    context, store, _, _ = build(monkeypatch, docs, history)
    collect(context, store, ('settled-user',))
    item = store.table.rows[('settled-user', 'head')]
    assert len(item['payload']) < 1000
    assert store.get('settled-user', 'head')['listing']['has_records'] is True


def test_the_collector_stores_the_listing_figures_in_the_head(monkeypatch):
    docs, history = world()
    context, store, _, _ = build(monkeypatch, docs, history)
    context.accounts = SimpleNamespace(
        list_users=lambda request: SimpleNamespace(
            listing=[SimpleNamespace(username='settled-user')], paginator=None
        )
    )
    context.is_leader = lambda: True
    context.distributed_lock = lambda: Mock(spec=['acquire', 'release', 'assert_held'])
    store.seed_head_subjects = lambda: False
    calculator = Mock()
    calculator.month.return_value = month(NOW.floor('month'), NOW, 0.25)
    worker = PersonalCostsCollector(context, store, calculator)
    worker.activity.capture = lambda *args: None
    worker.check()
    listing = store.get('settled-user', 'head')['listing']
    assert listing['session_count'] == 1 and listing['hours'] == pytest.approx(24)
    # the mock's one month, read as both this month and last
    assert listing['storage_cost'] == pytest.approx(30 * 0.25)
    assert listing['live'] is False


def test_fifty_users_listing_requests_per_view(monkeypatch, capsys):
    docs, history = world()
    names = [f'quiet-{index:02d}' for index in range(46)]
    context, store, counts, index = build(monkeypatch, docs, history)
    for name in names:
        docs.append(
            session(
                name,
                name,
                'STOPPED',
                NOW.shift(days=-10),
                NOW.shift(days=-9),
                stopped_on=ms(NOW.shift(days=-9)),
            )
        )
        history.append(run(name, name, NOW.shift(days=-10), NOW.shift(days=-9)))
    collect(
        context,
        store,
        names + ['settled-user', 'live-user', 'deleted-user', 'zero-user'],
    )
    monkeypatch.setattr(arrow, 'utcnow', lambda: NOW.shift(minutes=10))
    before_counts, after_counts = {}, {}
    for heads, target in ((None, before_counts), ('stored', after_counts)):
        counts.clear()
        index.document_reads = index.change_reads = 0
        result = view(context, heads=heads)
        target.update(
            counts, documents=index.document_reads, changes=index.change_reads
        )
        target['result'] = rows(result)
    assert after_counts['result'] == before_counts['result']
    before_counts.pop('result'), after_counts.pop('result')
    with capsys.disabled():
        print(f'\nlisting view, 50 users: {sorted(before_counts.items())}')
        print(f'listing view, 50 users, stored: {sorted(after_counts.items())}')
    assert 'session-history Scan' not in after_counts
    assert after_counts.get('personal-costs GetItem', 0) <= 8
    assert before_counts['personal-costs GetItem'] >= 50


def test_a_former_user_with_only_desktop_history_keeps_their_row(monkeypatch, capsys):
    docs, history = world()
    context, store, counts, _ = build(monkeypatch, docs, history)
    # accounts gone before any head was written, their documents gone from the window
    history.extend(
        [
            dict(
                owner='history-only',
                session_id='s2',
                idea_session_id='gone',
                name='gone',
                base_os='linux',
                created_on=ms(NOW.shift(days=-7)),
                deleted_on=ms(NOW.shift(days=-6)),
                stopped_on=ms(NOW.shift(days=-6)),
                instance_type='m5.large',
                instance_id='i-gone',
            ),
            run('gone', 'history-only', NOW.shift(days=-7), NOW.shift(days=-6)),
            run('older', 'history-zero', NOW.shift(days=-300), NOW.shift(days=-299)),
        ]
    )
    collect(context, store, COLLECTED)
    context.accounts = SimpleNamespace(
        list_users=lambda request: SimpleNamespace(
            listing=[SimpleNamespace(username='settled-user')], paginator=None
        )
    )
    context.is_leader = lambda: True
    context.distributed_lock = lambda: Mock(spec=['acquire', 'release', 'assert_held'])
    calculator = Mock()
    calculator.month.return_value = month(NOW.floor('month'), NOW, 0.25)
    worker = PersonalCostsCollector(context, store, calculator)
    worker.activity.capture = lambda *args: None
    worker.check()

    expected = rows(view(context, heads=None))
    by_name = {row['username']: row for row in expected['listing']}
    assert by_name['history-only']['desktop_hours'] == pytest.approx(24)
    assert by_name['history-only']['desktop_cost'] == pytest.approx(2.4)
    assert by_name['history-zero']['desktop_hours'] == 0
    counts.clear()
    assert rows(view(context)) == expected
    # no scan on the view: the collector's daily read found them
    assert 'session-history Scan' not in counts
    with capsys.disabled():
        print(f'\nlisting view with 2 history-only users: {sorted(counts.items())}')

    # once a day: the next run of the same day does not read the table again
    counts.clear()
    worker.check()
    assert 'session-history Scan' not in counts
    monkeypatch.setattr(arrow, 'utcnow', lambda: NOW.shift(days=1))
    counts.clear()
    worker.check()
    assert counts['session-history Scan'] == 1


def failing_history(table_query):
    def query(**kwargs):
        raise RuntimeError('throttled')

    return query


def seeding_table(store, pages):
    """a table scanned a page at a time; each segment answers its pages in order"""
    calls = []

    def scan(**kwargs):
        calls.append(kwargs)
        segment = kwargs.get('Segment', 0)
        start = (kwargs.get('ExclusiveStartKey') or {}).get('page', 0)
        items = pages.get(segment, [[]])
        answer = {'Items': [dict(subject=s, record='head') for s in items[start]]}
        if start + 1 < len(items):
            answer['LastEvaluatedKey'] = {'page': start + 1}
        return answer

    store.table.scan = scan
    return calls


def test_the_head_index_seed_resumes_where_it_stopped(monkeypatch):
    docs, history = world()
    _, store, _, _ = build(monkeypatch, docs, history)
    store.table.delete_item(Key=dict(subject=SYSTEM, record=HEAD_SUBJECTS_SEEDED))
    calls = seeding_table(store, {0: [['a'], ['b']], 3: [['c']]})
    clock = [0.0]
    monkeypatch.setattr(personal_costs_store.time, 'monotonic', lambda: clock[0])

    def tick():
        clock[0] += 70

    assert store.seed_head_subjects(on_progress=tick, budget_seconds=50) is False
    assert store.head_subjects() is None, 'readers keep scanning until it is complete'
    assert len(calls) == 1
    calls.clear()
    assert store.seed_head_subjects(on_progress=tick, budget_seconds=100000) is True
    # not from the top: the second page of the first segment is where it left off
    assert calls[0]['Segment'] == 0 and calls[0]['ExclusiveStartKey'] == {'page': 1}
    assert set(store.head_subjects()) >= {'a', 'b', 'c'}
    assert store.seed_head_subjects() is False
    assert not list(store.records(SYSTEM, 'seed-head-subjects:'))


def test_a_long_seed_does_not_read_as_a_stalled_collector(monkeypatch):
    docs, history = world()
    context, store, _, _ = build(monkeypatch, docs, history)
    store.table.delete_item(Key=dict(subject=SYSTEM, record=HEAD_SUBJECTS_SEEDED))
    moments = [NOW.shift(seconds=140 * step) for step in range(1, 9)]
    seen = []
    real_scan_pages = {segment: [[f's{segment}']] for segment in range(8)}
    calls = seeding_table(store, real_scan_pages)
    inner = store.table.scan

    def slow_scan(**kwargs):
        now = moments[min(len(calls), len(moments) - 1)]
        monkeypatch.setattr(arrow, 'utcnow', lambda: now)
        seen.append(store.collecting().collecting_delayed)
        return inner(**kwargs)

    store.table.scan = slow_scan
    context.accounts = SimpleNamespace(
        list_users=lambda request: SimpleNamespace(
            listing=[SimpleNamespace(username='settled-user')], paginator=None
        )
    )
    context.is_leader = lambda: True
    context.distributed_lock = lambda: Mock(spec=['acquire', 'release', 'assert_held'])
    worker = PersonalCostsCollector(context, store, Mock())
    worker.activity.capture = lambda *args: None
    worker._startup = True
    worker.calculator.month.return_value = month(NOW.floor('month'), NOW, 0.25)
    worker.check()
    assert len(seen) == 8 and not any(seen)


def test_a_failed_history_read_never_stores_a_complete_zero(monkeypatch):
    # every desktop this user had has been deleted, so only the history table knows them
    docs, history = world()
    context, store, _, _ = build(monkeypatch, docs, history)
    resource = context.aws().dynamodb_table()
    resource.Table = lambda name: SimpleNamespace(
        scan=failing_history(None), query=failing_history(None)
    )
    collect(context, store, ('deleted-user',))
    assert 'listing' not in store.get('deleted-user', 'head')


def test_a_failed_history_read_is_not_a_listing_of_zero(monkeypatch):
    docs, history = world()
    context, store, _, _ = build(monkeypatch, docs, history)
    collect(context, store, COLLECTED)
    resource = context.aws().dynamodb_table()
    resource.Table = lambda name: SimpleNamespace(
        scan=failing_history(None), query=failing_history(None)
    )
    monkeypatch.setattr(arrow, 'utcnow', lambda: NOW.shift(minutes=10))
    assert view(context).desktops_unavailable is True


def test_a_failed_history_read_leaves_readers_on_the_full_read(monkeypatch):
    docs, history = world()
    context, store, _, _ = build(monkeypatch, docs, history)
    store.table.delete_item(Key=dict(subject=SYSTEM, record=HEAD_SUBJECTS_SEEDED))
    store.table.scan = lambda **kwargs: dict(Items=[])
    context.accounts = SimpleNamespace(
        list_users=lambda request: SimpleNamespace(listing=[], paginator=None)
    )
    context.is_leader = lambda: True
    context.distributed_lock = lambda: Mock(spec=['acquire', 'release', 'assert_held'])
    worker = PersonalCostsCollector(context, store, Mock())
    worker.activity.capture = lambda *args: None
    monkeypatch.setattr(
        MyCostsService, 'history_owners', Mock(side_effect=RuntimeError('throttled'))
    )
    worker._startup = True
    context.accounts.list_users = lambda request: SimpleNamespace(
        listing=[SimpleNamespace(username='settled-user')], paginator=None
    )
    worker.calculator.month.return_value = month(NOW.floor('month'), NOW, 0.25)
    worker.check()
    # not seeded: no reader can trust an index missing these owners, and it is tried again
    assert store.head_subjects() is None
    assert 'history_owners_on' not in store.get(SYSTEM, 'state')
    monkeypatch.setattr(MyCostsService, 'history_owners', lambda self: {'history-only'})
    monkeypatch.setattr(arrow, 'utcnow', lambda: NOW.shift(minutes=16))
    worker.check()
    assert 'history-only' in store.head_subjects()
