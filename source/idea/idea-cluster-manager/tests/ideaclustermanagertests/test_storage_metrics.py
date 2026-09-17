"""
Test Cases for the storage metrics collector
ONTAP answers are recorded shapes of the two REST collections; nothing reaches a file system.
"""

from ideaclustermanager.app.metrics.storage_metrics_service import (
    OntapClient,
    StorageMetrics,
    StorageMetricsService,
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
        'host': 'test-cluster',
        'user': 'alice',
        'volume': 'data',
        'svm': 'svm1',
        'filesystem': 'fs-09753a84872d3209b',
    }
    assert context.dimensions(used['bob'])['qtree'] == 'proj'
    assert all(e['MetricType'] == 'Gauge' for e in context.published())
    assert all(
        context.dimensions(e)['host'] == context.cluster_name()
        for e in context.published()
    )
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

    def fake_get(url, auth=None, verify=None, timeout=None):
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


def test_missing_password_leaves_storage_checkpoint_unset():
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
    assert any('no password' in line for line in context.logger().lines)
