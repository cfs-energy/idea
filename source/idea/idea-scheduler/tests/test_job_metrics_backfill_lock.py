"""CM-MET-18: the scheduler has no distributed lock; its job-metrics backfill must start once"""

from unittest.mock import Mock

import pytest

from ideadatamodel import exceptions
from ideascheduler.app.metrics.job_metrics_backfill import JobMetricsBackfill


def test_a_backfill_starts_and_a_second_start_is_refused(context, monkeypatch):
    assert context.distributed_lock() is None  # the scheduler's own context
    monkeypatch.setattr('ideasdk.metrics.history_backfill.threading.Thread', Mock())
    backfill = JobMetricsBackfill(context)
    monkeypatch.setattr(backfill, 'save', lambda: None)
    status = backfill.start(True)
    assert status['state'] == 'running'
    backfill.thread = Mock(is_alive=lambda: False)  # this task's thread is gone
    with pytest.raises(exceptions.SocaException) as refused:
        backfill.start(True)
    assert 'already running' in refused.value.message
    backfill.backfill_lock().release(key=backfill.key)
