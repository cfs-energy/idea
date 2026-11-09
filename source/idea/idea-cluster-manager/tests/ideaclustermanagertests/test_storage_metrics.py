"""
Test Cases for the storage metrics collector
ONTAP answers are recorded shapes of the two REST collections; nothing reaches a file system.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ideaclustermanager.app.metrics.storage_metrics_service import (
    FileContentsRefused,
    OntapClient,
    StorageMetrics,
    StorageMetricsService,
    StorageTarget,
    fs_id_from_host,
    normalize_user,
    publish_storage,
    user_usage,
)
from ideaclustermanagertests.metrics_fakes import FakeContext

ENDPOINT = 'svm-064990494a2dbd4c2.fs-09753a84872d3209b.fsx.us-east-1.amazonaws.com'


def quota(user, volume, used, files, qtree='', kind='user', user_id=''):
    return {
        'svm': {'name': 'svm1'},
        'volume': {'name': volume},
        'qtree': {'name': qtree},
        'type': kind,
        'users': [{'name': user, 'id': user_id}] if user is not None else [],
        'space': {'used': {'total': used}},
        'files': {'used': {'total': files}},
    }


def volume(name, size, used, ssd=0, pool=0):
    return {
        'name': name,
        'svm': {'name': 'svm1'},
        'space': {
            'size': size,
            'used': used,
            'performance_tier_footprint': ssd,
            'capacity_tier_footprint': pool,
        },
    }


def test_identities_fold_into_one_user():
    assert normalize_user('CORP\\Alice', '') == 'alice'
    assert normalize_user('alice', '1001') == 'alice'
    assert normalize_user('*', '') == ''
    assert normalize_user('', 'S-1-5-21-1') == 'sid:S-1-5-21-1'
    assert normalize_user('', '1001') == 'uid:1001'
    assert fs_id_from_host(ENDPOINT) == 'fs-09753a84872d3209b'
    assert fs_id_from_host('storage.example.invalid') == ''


def test_user_usage_sums_the_user_rows_and_drops_the_rest():
    reports = [
        quota('CORP\\Alice', 'data', 100, 3),
        quota('alice', 'data', 50, 2),  # unix identity of the same human
        quota('bob', 'data', 10, 1, qtree='proj'),
        quota('*', 'data', 999, 9),  # default rule
        quota('alice', 'data', 5, 1, kind='tree'),
        quota(None, 'data', 7, 1),
    ]
    usage = user_usage(reports)
    assert usage[('svm1', 'data', '', 'alice')] == (150, 5)
    assert usage[('svm1', 'data', 'proj', 'bob')] == (10, 1)
    assert len(usage) == 2


def test_publish_storage_renders_gauges_with_the_tier_split_only_where_served():
    context = FakeContext({'metrics.provider': 'dogstatsd'})
    metrics = StorageMetrics(context)
    count = publish_storage(
        metrics,
        'fs-09753a84872d3209b',
        [volume('data', 1000, 600, ssd=500, pool=100), volume('scratch', 200, 50)],
        [quota('alice', 'data', 150, 5), quota('bob', 'data', 10, 1, qtree='proj')],
    )
    assert count == 2 * 2 + 2 * 2 + 2

    used = {
        context.dimensions(e)['user']: e
        for e in context.published('storage.used_bytes')
    }
    assert used['alice']['Value'] == 150.0
    assert context.dimensions(used['alice']) == {
        'user': 'alice',
        'volume': 'data',
        'svm': 'svm1',
        'filesystem': 'fs-09753a84872d3209b',
    }
    assert context.dimensions(used['bob'])['qtree'] == 'proj'
    assert all(e['MetricType'] == 'Gauge' for e in context.published())
    assert all('host' not in context.dimensions(e) for e in context.published())
    tiers = {
        (context.dimensions(e)['volume'], context.dimensions(e)['tier']): e['Value']
        for e in context.published('storage.volume_tier_bytes')
    }
    assert tiers == {('data', 'ssd'): 500.0, ('data', 'capacity_pool'): 100.0}
    sizes = {
        context.dimensions(e)['volume']: e['Value']
        for e in context.published('storage.volume_size_bytes')
    }
    assert sizes == {'data': 1000.0, 'scratch': 200.0}


def test_targets_are_the_ontap_entries_with_credentials():
    context = FakeContext(
        {
            'metrics.provider': 'dogstatsd',
            'cluster-manager.metrics.storage.enabled': True,
            'shared-storage': {
                'apps': {'provider': 'efs'},
                'data': {'provider': 'fsx_netapp_ontap'},
                'home': {'provider': 'fsx_netapp_ontap'},
            },
            'shared-storage.home.fsx_netapp_ontap.metrics.username': 'idea-metrics',
            'shared-storage.home.fsx_netapp_ontap.svm.management_dns': ENDPOINT,
            'shared-storage.home.fsx_netapp_ontap.metrics.password_secret_arn': 'arn:secret',
        },
        secrets={'arn:secret': 'pw'},
    )
    service = StorageMetricsService(context)
    assert service.is_enabled()
    targets = service.targets()
    assert [(t.name, t.endpoint, t.username) for t in targets] == [
        ('home', ENDPOINT, 'idea-metrics')
    ]
    assert context.config().get_secret(targets[0].password_key) == 'pw'


def test_configuration_status_is_settings_only(monkeypatch):
    context = FakeContext(
        {
            'metrics.provider': 'cloudwatch',
            'cluster-manager.metrics.storage.enabled': True,
            'shared-storage': {
                'data': {'provider': 'fsx_netapp_ontap'},
                'apps': {'provider': 'efs'},
            },
            'shared-storage.data.fsx_netapp_ontap.metrics.username': 'reader',
            'shared-storage.data.fsx_netapp_ontap.svm.management_dns': 'storage.example.invalid',
            'shared-storage.data.fsx_netapp_ontap.metrics.password_secret_arn': 'secret-reference',
        }
    )
    monkeypatch.setattr(
        context.config(),
        'get_secret',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('secret read')),
    )
    monkeypatch.setattr(
        'ideaclustermanager.app.metrics.storage_metrics_service.requests.get',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('network read')),
    )

    status = StorageMetricsService(context).configuration_status()

    assert status == {
        'status': 'enabled',
        'reason': 'configured',
        'provider': 'cloudwatch',
        'has_efs': True,
    }


def test_configuration_status_distinguishes_setup_states():
    cases = [
        (
            {
                'metrics.provider': 'cloudwatch',
                'shared-storage': {'data': {'provider': 'fsx_netapp_ontap'}},
            },
            ('disabled', 'metrics_disabled'),
        ),
        (
            {
                'metrics.provider': 'custom',
                'cluster-manager.metrics.storage.enabled': True,
                'shared-storage': {'data': {'provider': 'fsx_netapp_ontap'}},
            },
            ('unsupported', 'unsupported_provider'),
        ),
        (
            {
                'metrics.provider': 'dogstatsd',
                'cluster-manager.metrics.storage.enabled': True,
                'shared-storage': {'apps': {'provider': 'efs'}},
            },
            ('not_configured', 'efs_only'),
        ),
        (
            {
                'metrics.provider': 'dogstatsd',
                'cluster-manager.metrics.storage.enabled': True,
                'shared-storage': {'data': {'provider': 'fsx_netapp_ontap'}},
            },
            ('not_configured', 'missing_credentials'),
        ),
    ]
    for values, expected in cases:
        status = StorageMetricsService(FakeContext(values)).configuration_status()
        assert (status['status'], status['reason']) == expected


def test_targets_require_username_secret_reference_and_endpoint():
    base = {
        'metrics.provider': 'cloudwatch',
        'cluster-manager.metrics.storage.enabled': True,
        'shared-storage': {'data': {'provider': 'fsx_netapp_ontap'}},
        'shared-storage.data.fsx_netapp_ontap.metrics.username': 'reader',
        'shared-storage.data.fsx_netapp_ontap.svm.management_dns': 'storage.example.invalid',
        'shared-storage.data.fsx_netapp_ontap.metrics.password_secret_arn': 'secret-reference',
    }
    keys = [
        'shared-storage.data.fsx_netapp_ontap.metrics.username',
        'shared-storage.data.fsx_netapp_ontap.metrics.password_secret_arn',
        'shared-storage.data.fsx_netapp_ontap.svm.management_dns',
    ]
    for key in keys:
        values = dict(base)
        del values[key]
        assert StorageMetricsService(FakeContext(values)).targets() == []


def test_client_follows_pages_and_refuses_a_failure(monkeypatch):
    calls = []

    class Response:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
            self.text = str(body)

        def json(self):
            return self._body

    pages = {
        '/api/one': Response(
            200,
            {'records': [{'a': 1}], '_links': {'next': {'href': '/api/one?page=2'}}},
        ),
        '/api/one?page=2': Response(200, {'records': [{'a': 2}]}),
        '/api/bad': Response(401, {'error': 'no'}),
    }

    def fake_get(url, auth=None, verify=None, timeout=None, headers=None):
        calls.append((url, auth, verify))
        return pages[url.split('https://' + ENDPOINT, 1)[1]]

    monkeypatch.setattr(
        'ideaclustermanager.app.metrics.storage_metrics_service.requests.get', fake_get
    )
    client = OntapClient('https://' + ENDPOINT + '/', 'u', 'p')
    assert client.fs_id == 'fs-09753a84872d3209b'
    assert client.get('/api/one') == [{'a': 1}, {'a': 2}]
    assert calls[0][1] == ('u', 'p') and calls[0][2] is False
    try:
        client.get('/api/bad')
        assert False, 'a non-200 must raise'
    except RuntimeError as e:
        assert '401' in str(e)


def test_missing_password_reference_leaves_storage_checkpoint_unset():
    context = FakeContext(
        {
            'metrics.provider': 'dogstatsd',
            'shared-storage': {'data': {'provider': 'fsx_netapp_ontap'}},
            'shared-storage.data.fsx_netapp_ontap.metrics.username': 'reader',
            'shared-storage.data.fsx_netapp_ontap.svm.management_dns': ENDPOINT,
        }
    )
    StorageMetricsService(context).run_once()
    assert context.config().db.writes == []
    assert context.published() == []
    assert context.distributed_lock().held == []
    assert any(
        'no configured storage metrics targets' in line
        for line in context.logger().lines
    )


def test_latest_reports_are_retained_per_target_and_joined_by_user(monkeypatch):
    context = FakeContext(
        {
            'metrics.provider': 'dogstatsd',
            'shared-storage': {'data': {'provider': 'fsx_netapp_ontap'}},
            'shared-storage.data.fsx_netapp_ontap.metrics.username': 'reader',
            'shared-storage.data.fsx_netapp_ontap.svm.management_dns': ENDPOINT,
            'shared-storage.data.fsx_netapp_ontap.metrics.password_secret_arn': 'arn:secret',
        },
        secrets={'arn:secret': 'pw'},
    )
    alice = quota('CORP\\Alice', 'data', 123, 4)
    alice['space']['hard_limit'] = 1024
    reports = [
        alice,
        quota('bob', 'data', 99, 2),
        quota('*', 'data', 999, 9),
        quota('alice', 'data', 999, 9, kind='group'),
    ]
    reports.extend(
        [
            quota('root', 'data', 40, 1),
            quota('', 'data', 20, 1, user_id='1002'),
            quota('', 'data', 10, 1, user_id='S-1-5-21-2'),
        ]
    )
    volumes = [
        volume('data', 1000, 100, ssd=60, pool=40),
        volume('scratch', 200, 50, ssd=30, pool=20),
    ]
    monkeypatch.setattr(OntapClient, 'volumes', lambda _: volumes)
    monkeypatch.setattr(OntapClient, 'quota_reports', lambda _: reports)
    service = StorageMetricsService(context)
    service.run_once()
    snapshot = service.usage_by_filesystem()[fs_id_from_host(ENDPOINT)]
    assert snapshot['filesystem_id'] == fs_id_from_host(ENDPOINT)
    assert snapshot['total_bytes'] == sum(snapshot['users'].values()) == 292
    assert snapshot['users']['root'] == 40
    assert snapshot['users']['uid:1002'] == 20
    assert snapshot['users']['sid:S-1-5-21-2'] == 10
    assert snapshot['complete'] is True and snapshot['zero_when_absent'] is True
    assert snapshot['capacity_pool_bytes'] == 60 and snapshot['ssd_bytes'] == 90
    assert snapshot['allocation_pool']
    for records in (
        [],
        [quota('*', 'data', 0, 0, kind='group')],
        [quota('user-a', 'data', 1, 1)],
    ):
        service._collected_reports['data'] = (snapshot['measured_at'], records)
        assert (
            service.usage_by_filesystem()[fs_id_from_host(ENDPOINT)]['zero_when_absent']
            is False
        )
    service._quota_reports['data'] = (snapshot['measured_at'], reports)
    rows = service.get_user_quotas('alice')
    assert len(rows) == 1
    assert rows[0]['target'] == 'data'
    assert rows[0]['used_bytes'] == 123
    assert rows[0]['files'] == 4
    assert rows[0]['limit_bytes'] == 1024
    assert rows[0]['measured_at'] > 0
    assert service.get_user_quotas('nobody') == []
    service._quota_reports['scratch'] = (123, [quota('alice', 'scratch', 5, 1)])
    service._quota_reports['data'] = (124, [quota('alice', 'data', 200, 6)])
    rows = service.get_user_quotas('ALICE')
    assert [row['used_bytes'] for row in rows] == [200, 5]
    assert rows[0]['limit_bytes'] is None


@pytest.mark.parametrize(
    'path',
    [
        # the content read as ONTAP documents it, and the ways around a plain check
        '/api/storage/volumes/u/files/a%2Fb?byte_offset=0&length=100',
        '/api/storage/volumes/u/files/a?type=directory&byte_offset=0',
        '/api/storage/volumes/u/files/a?type=directory&LENGTH=100',
        '/api/storage/volumes/u/files/a?type=directory&stream_name=x',
        '/api/storage/volumes/u/files/a?type=directory&byte%5Foffset=0',
        '/api/storage/volumes/u/files/a?type=directory;length=1',
        # a file's own read with none of those: not a listing, so no
        '/api/storage/volumes/u/files/a%2Fb',
        '/api/storage/volumes/u/files/a%2Fb?fields=size',
        '/API/storage/volumes/u/FILES/a',
        '/api/storage/volumes/u/%66iles/a',
    ],
)
@pytest.mark.parametrize('method', ['get', 'first_page'])
def test_no_call_can_read_file_contents(monkeypatch, path, method):
    sent = []
    monkeypatch.setattr(
        'ideaclustermanager.app.metrics.storage_metrics_service.requests.get',
        lambda url, **kwargs: sent.append(url),
    )
    with pytest.raises(FileContentsRefused):
        getattr(OntapClient('host', 'user', 'password'), method)(path)
    assert sent == []


def test_a_next_link_cannot_lead_to_file_contents(monkeypatch):
    sent = []

    def fake_get(url, **kwargs):
        sent.append(url)
        next_path = '/api/storage/volumes/u/files/a?byte_offset=0&length=1'
        return SimpleNamespace(
            status_code=200,
            json=lambda: {'records': [], '_links': {'next': {'href': next_path}}},
        )

    monkeypatch.setattr(
        'ideaclustermanager.app.metrics.storage_metrics_service.requests.get', fake_get
    )
    with pytest.raises(FileContentsRefused):
        OntapClient('host', 'user', 'password').get('/api/storage/volumes')
    assert len(sent) == 1


@pytest.mark.parametrize(
    'path',
    [
        '/api/storage/volumes?fields=name',
        '/api/storage/quota/reports',
        '/api/storage/volumes/u/files/%2F?return_metadata=true&fields=analytics',
        '/api/storage/volumes/u/files/a%2Fb?type=directory&fields=analytics.bytes_used'
        '&order_by=analytics.bytes_used%20desc&max_records=1000&return_timeout=10',
        '/api/storage/volumes/u/files/a?start.iter_key=abc&max_records=1000',
    ],
)
def test_what_the_collectors_ask_for_still_goes_and_only_as_json(monkeypatch, path):
    sent = []

    def fake_get(url, **kwargs):
        sent.append(kwargs['headers'])
        return SimpleNamespace(status_code=200, json=lambda: {'records': []})

    monkeypatch.setattr(
        'ideaclustermanager.app.metrics.storage_metrics_service.requests.get', fake_get
    )
    client = OntapClient('host', 'user', 'password')
    client.get(path)
    client.first_page(path)
    assert [h['Accept'] for h in sent] == ['application/json'] * 2


def test_nothing_reaches_ontap_except_through_the_guard():
    from ideaclustermanager.app.metrics import storage_metrics_service as module

    source = Path(module.__file__).read_text()
    assert source.count('requests.get(') == 1
    assert 'requests.post(' not in source and 'requests.request(' not in source


def test_the_same_volume_name_on_two_file_systems_is_two_quotas():
    service = StorageMetricsService(FakeContext({}))
    service.targets = lambda: [
        SimpleNamespace(name='data', endpoint='svm.fs-1.example'),
        SimpleNamespace(name='apps', endpoint='svm.fs-2.example'),
    ]
    service._quota_reports = {
        'data': (10, [quota('alice', 'vol1', 100, 1)]),
        'apps': (20, [quota('alice', 'vol1', 7, 1)]),
    }
    found = sorted(service.get_user_quotas('alice'), key=lambda row: row['used_bytes'])
    assert [row['used_bytes'] for row in found] == [7, 100]
    assert [row['endpoint'] for row in found] == [
        'svm.fs-2.example',
        'svm.fs-1.example',
    ]


def test_user_quota_sums_nfs_and_smb_rows_and_ignores_duplicate_targets():
    service = StorageMetricsService(FakeContext({}))
    rows = [
        quota('alice', 'profiles', 100, 3),
        quota('DOMAIN\\Alice', 'profiles', 20, 2),
        quota('bob', 'profiles', 999, 9),
    ]
    service._quota_reports = {'apps': (10, rows), 'data': (20, rows)}
    found = service.get_user_quotas('alice')
    assert len(found) == 1
    assert found[0]['target'] == 'data'
    assert found[0]['used_bytes'] == 120
    assert found[0]['files'] == 5


def test_unattributed_bytes_come_from_the_file_system_asked_about():
    service = StorageMetricsService(FakeContext({}))
    service.targets = lambda: [
        SimpleNamespace(name='home', endpoint='svm.fs-1'),
        SimpleNamespace(name='other', endpoint='svm.fs-2'),
    ]
    service._quota_reports = {
        'home': (5, [quota('S-1-5-21-9', 'vol1', 10, 1)]),
        'other': (9, [quota('S-1-5-21-9', 'vol1', 999, 9)]),
    }
    assert service.unattributed_quota('vol1', 'svm.fs-1') == dict(bytes=10, files=1)
    assert service.unattributed_quota('vol1', 'svm.fs-2') == dict(bytes=999, files=9)


def test_owners_known_only_as_a_sid_are_counted_as_unattributed():
    service = StorageMetricsService(FakeContext({}))
    service._quota_reports = {
        'data': (
            5,
            [
                quota('S-1-5-21-9', 'profiles', 70, 2),
                quota('alice', 'profiles', 10, 1),
                quota('S-1-5-21-9', 'apps', 500, 5),
            ],
        )
    }
    assert service.unattributed_quota('profiles') == dict(bytes=70, files=2)
    assert service.get_user_quotas('s-1-5-21-9')[0]['used_bytes'] == 70


def test_any_task_can_read_quotas_and_failure_is_soft(monkeypatch):
    context = FakeContext(
        {
            'cluster-manager.metrics.storage.enabled': True,
            'metrics.provider': 'cloudwatch',
            'shared-storage': {
                'data': {'provider': 'fsx_netapp_ontap'},
            },
            'shared-storage.data.fsx_netapp_ontap.metrics.username': 'reader',
            'shared-storage.data.fsx_netapp_ontap.metrics.password_secret_arn': 'secret',
            'shared-storage.data.fsx_netapp_ontap.svm.management_dns': ENDPOINT,
        }
    )
    service = StorageMetricsService(context)
    calls = []

    def read(_):
        calls.append(1)
        return [quota('alice', 'profiles', 7, 1)]

    monkeypatch.setattr(OntapClient, 'quota_reports', read)
    monkeypatch.setattr(context.config(), 'get_secret', lambda _: 'pw', raising=False)
    service.refresh_quota_reports()
    service.refresh_quota_reports()
    assert len(calls) == 1
    assert service.get_user_quotas('alice')[0]['used_bytes'] == 7

    def fail(_):
        raise RuntimeError('unreachable')

    monkeypatch.setattr(OntapClient, 'quota_reports', fail)
    service._quota_reports = {}
    service._quota_attempted = 0
    service.refresh_quota_reports()
    assert service.get_user_quotas('alice') == []


def test_filesystem_merges_targets_and_requires_every_report(monkeypatch):
    service = StorageMetricsService(FakeContext({}))
    targets = [
        StorageTarget('data', 'svm-a.fs-test.fsx.example.invalid', 'reader', ''),
        StorageTarget('home', 'svm-b.fs-test.fsx.example.invalid', 'reader', ''),
    ]
    monkeypatch.setattr(service, 'targets', lambda: targets)
    service._collected_reports = {
        'data': (100, [quota('user-a', 'data', 10, 1), quota('*', 'data', 0, 0)]),
        'home': (
            200,
            [
                quota('user-a', 'home', 20, 1),
                quota('user-b', 'home', 30, 1),
                quota('*', 'home', 0, 0),
            ],
        ),
    }
    service._volumes = {
        'data': [volume('data', 100, 50, ssd=30, pool=20)],
        'home': [volume('home', 200, 100, ssd=60, pool=40)],
    }
    snapshot = service.usage_by_filesystem()['fs-test']
    assert snapshot['users'] == {'user-a': 30, 'user-b': 30}
    assert snapshot['total_bytes'] == 60
    assert snapshot['capacity_pool_bytes'] == 60
    assert snapshot['ssd_bytes'] == 90
    assert snapshot['measured_at'] == 100
    assert snapshot['complete'] and snapshot['zero_when_absent']

    service._collected_reports['home'][1].pop()
    snapshot = service.usage_by_filesystem()['fs-test']
    assert snapshot['complete'] and not snapshot['zero_when_absent']
    del service._collected_reports['home']
    snapshot = service.usage_by_filesystem()['fs-test']
    assert not snapshot['complete'] and not snapshot['zero_when_absent']
    assert snapshot['users'] == {'user-a': 10}


def test_filesystem_deduplicates_targets_with_the_same_endpoint(monkeypatch):
    service = StorageMetricsService(FakeContext({}))
    targets = [
        StorageTarget(name, 'svm-a.fs-test.fsx.example.invalid', 'reader', '')
        for name in ('data', 'home')
    ]
    monkeypatch.setattr(service, 'targets', lambda: targets)
    for target in targets:
        service._collected_reports[target.name] = (
            100,
            [quota('user-a', 'data', 10, 1), quota('*', 'data', 0, 0)],
        )
        service._volumes[target.name] = [volume('data', 100, 50, ssd=30, pool=20)]
    snapshot = service.usage_by_filesystem()['fs-test']
    assert snapshot['users'] == {'user-a': 10}
    assert snapshot['total_bytes'] == 10
    assert snapshot['capacity_pool_bytes'] == 20
    assert snapshot['ssd_bytes'] == 30
    del service._collected_reports['data']
    assert service.usage_by_filesystem()['fs-test'] == snapshot


def test_saved_snapshot_stands_until_this_process_has_every_report(monkeypatch):
    import json

    saved = {
        'fs-test': dict(
            filesystem_id='fs-test',
            measured_at=100,
            users={'user-a': 30},
            total_bytes=30,
            complete=True,
            zero_when_absent=True,
            capacity_pool_bytes=20,
            ssd_bytes=30,
            allocation_pool='Quota bytes; unattributed rows remain unassigned',
        )
    }
    service = StorageMetricsService(
        FakeContext(
            {'cluster-manager.metrics.storage.usage_snapshot': json.dumps(saved)}
        )
    )
    targets = [
        StorageTarget('data', 'svm-a.fs-test.fsx.example.invalid', 'reader', ''),
        StorageTarget('home', 'svm-b.fs-test.fsx.example.invalid', 'reader', ''),
    ]
    monkeypatch.setattr(service, 'targets', lambda: targets)
    assert service.usage_by_filesystem() == saved
    service._collected_reports = {
        'data': (200, [quota('user-a', 'data', 10, 1), quota('*', 'data', 0, 0)])
    }
    assert service.usage_by_filesystem() == saved
    service._collected_reports['home'] = (
        200,
        [quota('user-b', 'home', 5, 1), quota('*', 'home', 0, 0)],
    )
    snapshot = service.usage_by_filesystem()['fs-test']
    assert snapshot['complete'] and snapshot['users'] == {'user-a': 10, 'user-b': 5}


def test_a_pane_quota_read_leaves_the_cost_snapshot_alone(monkeypatch):
    import json

    saved = {
        'fs-09753a84872d3209b': dict(
            filesystem_id='fs-09753a84872d3209b',
            measured_at=100,
            users={'alice': 30},
            total_bytes=30,
            complete=True,
            zero_when_absent=True,
            capacity_pool_bytes=5 * 2**40,
            ssd_bytes=2**40,
            allocation_pool='Quota bytes; unattributed rows remain unassigned',
        )
    }
    context = FakeContext(
        {
            'cluster-manager.metrics.storage.enabled': True,
            'cluster-manager.metrics.storage.usage_snapshot': json.dumps(saved),
            'metrics.provider': 'cloudwatch',
            'shared-storage': {'data': {'provider': 'fsx_netapp_ontap'}},
            'shared-storage.data.fsx_netapp_ontap.metrics.username': 'reader',
            'shared-storage.data.fsx_netapp_ontap.metrics.password_secret_arn': 'secret',
            'shared-storage.data.fsx_netapp_ontap.svm.management_dns': ENDPOINT,
        },
        secrets={'secret': 'pw'},
    )
    reports = [quota('alice', 'data', 7, 1), quota('*', 'data', 0, 0)]
    monkeypatch.setattr(OntapClient, 'quota_reports', lambda _: reports)
    service = StorageMetricsService(context)
    # a task that has not collected: the pane reads quotas, never volumes
    service.refresh_quota_reports()
    assert service.get_user_quotas('alice')[0]['used_bytes'] == 7
    assert service.usage_by_filesystem() == saved

    # one that has: the pane's later quota read does not move the snapshot's users or its time
    monkeypatch.setattr(
        OntapClient, 'volumes', lambda _: [volume('data', 100, 50, ssd=30, pool=20)]
    )
    service.run_once()
    collected = service.usage_by_filesystem()
    assert collected['fs-09753a84872d3209b']['capacity_pool_bytes'] == 20
    assert collected['fs-09753a84872d3209b']['users'] == {'alice': 7}
    later = [quota('alice', 'data', 9, 1), quota('*', 'data', 0, 0)]
    monkeypatch.setattr(OntapClient, 'quota_reports', lambda _: later)
    service._quota_attempted = 0
    service.refresh_quota_reports(max_age=0)
    assert service.get_user_quotas('alice')[0]['used_bytes'] == 9
    assert service.usage_by_filesystem() == collected


def test_volume_quota_excludes_only_matching_user_qtrees():
    reports = [
        quota('user-a', 'data', 40, 4, qtree='project'),
        quota('user-a', 'data', 100, 10),
        quota('user-a', 'data', 20, 2, qtree='other'),
        quota('user-b', 'data', 30, 3, qtree='project'),
        quota('user-a', 'scratch', 50, 5, qtree='project'),
        dict(quota('user-a', 'data', 60, 6, qtree='project'), svm={'name': 'svm2'}),
    ]
    expected = {
        ('svm1', 'data', '', 'user-a'): (100, 10),
        ('svm1', 'data', 'project', 'user-b'): (30, 3),
        ('svm1', 'scratch', 'project', 'user-a'): (50, 5),
        ('svm2', 'data', 'project', 'user-a'): (60, 6),
    }
    assert user_usage(reports) == expected
    assert user_usage(list(reversed(reports))) == expected
