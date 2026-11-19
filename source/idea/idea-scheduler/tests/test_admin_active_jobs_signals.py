"""SCHED-24: the admin active-jobs listing passes the caller's role to the waiting signals"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideascheduler.app.api import scheduler_admin_api as module


@pytest.mark.parametrize('administrator', [True, False])
def test_the_admin_listing_passes_the_callers_role(monkeypatch, administrator):
    seen = {}
    monkeypatch.setattr(
        module, 'list_active_job_page', lambda context, payload: ([], 0)
    )
    monkeypatch.setattr(
        module,
        'apply_waiting_signals',
        lambda context, jobs, is_administrator=False: seen.update(
            admin=is_administrator
        ),
    )
    api = module.SchedulerAdminAPI.__new__(module.SchedulerAdminAPI)
    api.context = Mock()
    request = Mock()
    request.get_request_payload_as.return_value = SimpleNamespace(
        page_size=10, page_start=0, filters=None
    )
    request.is_administrator.return_value = administrator
    api.list_active_jobs(request)
    assert seen == {'admin': administrator}
