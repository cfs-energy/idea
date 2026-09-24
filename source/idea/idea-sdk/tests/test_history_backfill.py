import time
from unittest.mock import Mock

from ideasdk.metrics.history_backfill import MetricsHistoryBackfill


def test_expired_owned_lease_is_interrupted_without_a_finish():
    context = Mock()
    backfill = MetricsHistoryBackfill(context, 'cost')
    value = dict(
        backfill.empty_status(),
        state='running',
        owner='old-task',
        heartbeat_at=time.time() - 120,
        lease_expires_at=time.time() - 60,
    )
    context.config().db.cluster_settings_table.get_item.return_value = {
        'Item': {'value': value}
    }
    status = backfill.status()
    assert status['state'] == 'interrupted'
    assert status['finished_at'] is None
    assert value['state'] == 'running'
    value['lease_expires_at'] = time.time() + 60
    assert backfill.status()['state'] == 'running'


def test_active_run_has_an_owner_and_heartbeat(monkeypatch):
    context = Mock()
    context.config().db = None
    backfill = MetricsHistoryBackfill(context, 'cost')
    monkeypatch.setattr('ideasdk.metrics.history_backfill.threading.Thread', Mock())
    status = backfill.start(True)
    assert status['owner']
    assert status['lease_expires_at'] > status['heartbeat_at']
    stopped = Mock()
    stopped.wait.side_effect = [False, True]
    backfill.heartbeat(stopped)
    assert backfill.status()['heartbeat_at'] >= status['heartbeat_at']


def test_saved_status_has_no_floats_for_the_settings_table():
    from unittest.mock import Mock
    from ideasdk.metrics.history_backfill import MetricsHistoryBackfill

    context = Mock()
    saved = {}
    context.config.return_value.db.set_config_entry.side_effect = (
        lambda key, value: saved.update({key: value})
    )
    backfill = MetricsHistoryBackfill(context, 'jobs')
    backfill.collect = lambda *args, **kwargs: None
    backfill.start(dry_run=True)
    backfill.thread.join(5)
    assert saved
    for status in saved.values():
        assert not any(isinstance(v, float) for v in status.values()), status
