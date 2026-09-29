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
    jobs = [
        SocaJob(job_id=str(index), owner='user', queue_type='normal', state=state)
        for index, state in enumerate(
            (
                SocaJobState.QUEUED,
                SocaJobState.HELD,
                SocaJobState.RUNNING,
                SocaJobState.WAITING,
            )
        )
    ]
    ctx.scheduler.list_jobs.return_value = jobs
    ctx.scheduler.get_job.side_effect = lambda job_id: jobs[int(job_id)]
    sweep_disabled_jobs(ctx)
    assert [call.args[0] for call in ctx.scheduler.delete_job.call_args_list] == [
        '0',
        '1',
        '3',
    ]
    ctx.accounts_client.get_user.assert_called_once()
    assert ctx.queue_profiles.get_provisioning_queue.return_value.delete.call_count == 3


def test_dispatch_since_snapshot_is_left_running():
    ctx = context()
    ctx.scheduler.list_jobs.return_value = [
        SocaJob(job_id='1', owner='user', state=SocaJobState.QUEUED)
    ]
    ctx.scheduler.get_job.return_value = SocaJob(
        job_id='1', owner='user', state=SocaJobState.RUNNING
    )
    sweep_disabled_jobs(ctx)
    ctx.scheduler.delete_job.assert_not_called()


def test_lookup_failure_does_not_delete_jobs():
    ctx = context()
    ctx.accounts_client.get_user.side_effect = RuntimeError('unavailable')
    ctx.scheduler.list_jobs.return_value = [
        SocaJob(job_id='1', owner='user', state=SocaJobState.QUEUED)
    ]
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


@pytest.mark.parametrize('error_code', ['AUTH_USER_NOT_FOUND', 'GENERAL_ERROR'])
def test_deleted_or_unavailable_owner_does_not_block_later_jobs(error_code):
    from ideadatamodel import errorcodes

    ctx = context()
    jobs = [
        SocaJob(
            job_id=str(i),
            owner=f'user{i}',
            queue_type='normal',
            state=SocaJobState.QUEUED,
        )
        for i in range(2)
    ]
    ctx.scheduler.list_jobs.return_value = jobs
    ctx.scheduler.get_job.side_effect = lambda job_id: jobs[int(job_id)]
    ctx.accounts_client.get_user.side_effect = [
        exceptions.soca_exception(
            error_code=getattr(errorcodes, error_code), message='lookup failed'
        ),
        SimpleNamespace(user=SimpleNamespace(enabled=False)),
    ]
    sweep_disabled_jobs(ctx)
    assert [call.args[0] for call in ctx.scheduler.delete_job.call_args_list] == (
        ['0', '1'] if error_code == 'AUTH_USER_NOT_FOUND' else ['1']
    )


def test_accounts_service_failure_still_rejects_admission():
    ctx = context()
    ctx.accounts_client.get_user.side_effect = RuntimeError('unavailable')
    with pytest.raises(RuntimeError):
        require_enabled(ctx, 'user')


def test_sweep_failure_does_not_skip_queue_adoption():
    from ideascheduler.app.provisioning.job_monitor.job_monitor import JobMonitor

    monitor = JobMonitor.__new__(JobMonitor)
    monitor._context = context()
    monitor._context.scheduler.list_jobs.side_effect = RuntimeError('unavailable')
    monitor._context.queue_profiles.list_queue_profiles.return_value = [
        SimpleNamespace(enabled=True, queues=['normal'])
    ]
    monitor._logger = Mock()
    monitor._exit = Mock()
    monitor._exit.is_set.return_value = False
    monitor._reconcile_queue = Mock(return_value=1)
    monitor._job_reconciler()
    monitor._reconcile_queue.assert_called_once_with(
        queue='normal', log_tag='job-reconciler'
    )


def test_disabled_owner_snapshot_and_reason_survive_deletion():
    ctx = context()
    job = SocaJob(job_id='1', owner='user', state=SocaJobState.QUEUED)
    ctx.scheduler.list_jobs.return_value = [job]
    ctx.scheduler.get_job.return_value = job

    def deleted(job_id):
        ctx.job_cache.sync.assert_called_once_with(jobs=[job])
        ctx.job_cache.record_deleted_job.assert_not_called()

    ctx.scheduler.delete_job.side_effect = deleted
    sweep_disabled_jobs(ctx)
    ctx.scheduler.delete_job.assert_called_once_with('1')
    recorded = ctx.job_cache.record_deleted_job.call_args.kwargs
    assert recorded['job'] is job
    assert recorded['error_code'] == 'JOB_DELETED_DISABLED_OWNER'
    assert 'owner is disabled' in recorded['message']


def test_failed_deletion_does_not_record_a_terminal_reason():
    ctx = context()
    job = SocaJob(job_id='1', owner='user', state=SocaJobState.QUEUED)
    ctx.scheduler.list_jobs.return_value = [job]
    ctx.scheduler.get_job.return_value = job
    ctx.scheduler.delete_job.side_effect = RuntimeError('scheduler unavailable')
    sweep_disabled_jobs(ctx)
    ctx.job_cache.record_deleted_job.assert_not_called()
    assert job.disposition is None


def test_qsub_receives_validation_error_without_queueing():
    from ideadatamodel import (
        JobValidationResult,
        JobValidationResultEntry,
        SubmitJobResult,
    )

    api = OpenPBSAPI.__new__(OpenPBSAPI)
    api.context = context(True)
    api.context.is_ready.return_value = True
    hook = Mock()
    hook.job.owner = 'user'
    hook.is_valid.return_value = False
    hook.job_validation_result = JobValidationResult(
        results=[
            JobValidationResultEntry(
                error_code='INVALID_PARAMS',
                message='Requested instances run arm64, but the image is x86_64.',
            )
        ]
    )
    hook.incidentals_validation_result = JobValidationResult(results=[])
    hook.job_submission_result = SubmitJobResult(validations=hook.job_validation_result)
    api.hook_validate_job(hook)
    result = hook.api_context.success.call_args.args[0]
    assert result.accept is False
    assert (
        'Requested instances run arm64, but the image is x86_64.'
        in result.formatted_user_message
    )
    assert hook.job_submission_result.accepted is False
    api.context.job_monitor.job_queued.assert_not_called()
    hook.check_incidentals.assert_not_called()


def test_provisioner_records_disabled_owner_deletion():
    from ideascheduler.app.provisioning.job_provisioner.job_provisioner import (
        JobProvisioner,
    )

    provisioner = JobProvisioner.__new__(JobProvisioner)
    provisioner._context = context()
    provisioner._logger = Mock()
    job = SocaJob(job_id='1', owner='user', state=SocaJobState.QUEUED)
    provisioner._context.scheduler.get_job.return_value = job
    assert provisioner._is_job_provisionable(job) is False
    provisioner._context.scheduler.delete_job.assert_called_once_with('1')
    saved = provisioner._context.job_cache.record_deleted_job.call_args.kwargs
    assert saved['error_code'] == 'JOB_DELETED_DISABLED_OWNER'
    assert 'owner is disabled' in saved['message']
