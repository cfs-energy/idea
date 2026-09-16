from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ideadatamodel import SocaJob, SocaJobState, exceptions
from ideascheduler.app.accounts_guard import require_enabled, sweep_disabled_jobs
from ideascheduler.app.api.scheduler_api import SchedulerAPI
from ideascheduler.app.api.opepbs_api import OpenPBSAPI


def context(enabled=False):
    result = Mock()
    result.accounts_client.get_user.return_value.user.enabled = enabled
    return result


def test_admission_refuses_disabled_owner():
    with pytest.raises(exceptions.SocaException):
        require_enabled(context(), 'user')
    require_enabled(context(True), 'user')


def test_queued_and_held_deleted_running_jobs_finish():
    ctx = context()
    jobs = [SocaJob(job_id=str(index), owner='user', queue_type='normal', state=state)
            for index, state in enumerate((SocaJobState.QUEUED, SocaJobState.HELD, SocaJobState.RUNNING, SocaJobState.WAITING))]
    ctx.scheduler.list_jobs.return_value = jobs
    ctx.scheduler.get_job.side_effect = lambda job_id: jobs[int(job_id)]
    sweep_disabled_jobs(ctx)
    assert [call.args[0] for call in ctx.scheduler.delete_job.call_args_list] == ['0', '1', '3']
    ctx.accounts_client.get_user.assert_called_once()
    assert ctx.queue_profiles.get_provisioning_queue.return_value.delete.call_count == 3


def test_dispatch_since_snapshot_is_left_running():
    ctx = context()
    ctx.scheduler.list_jobs.return_value = [SocaJob(job_id='1', owner='user', state=SocaJobState.QUEUED)]
    ctx.scheduler.get_job.return_value = SocaJob(job_id='1', owner='user', state=SocaJobState.RUNNING)
    sweep_disabled_jobs(ctx)
    ctx.scheduler.delete_job.assert_not_called()


def test_lookup_failure_does_not_delete_jobs():
    ctx = context()
    ctx.accounts_client.get_user.side_effect = RuntimeError('unavailable')
    ctx.scheduler.list_jobs.return_value = [SocaJob(job_id='1', owner='user', state=SocaJobState.QUEUED)]
    with pytest.raises(RuntimeError):
        sweep_disabled_jobs(ctx)
    ctx.scheduler.delete_job.assert_not_called()


def test_api_refuses_before_submission():
    api = SchedulerAPI.__new__(SchedulerAPI)
    api.context = context()
    api.check_maintenance = Mock()
    api._submit_job = Mock()
    invocation = Mock()
    invocation.get_username.return_value = 'user'
    invocation.get_request_payload_as.return_value = SimpleNamespace(job_owner='user')
    with pytest.raises(exceptions.SocaException):
        api.submit_job(invocation)
    api._submit_job.assert_not_called()


def test_pbs_hook_refuses_direct_qsub():
    api = OpenPBSAPI.__new__(OpenPBSAPI)
    api.context = context()
    api.context.is_ready.return_value = True
    hook = Mock()
    hook.job.owner = 'user'
    api.hook_validate_job(hook)
    assert hook.api_context.success.call_args.args[0].accept is False
    hook.check_incidentals.assert_not_called()
