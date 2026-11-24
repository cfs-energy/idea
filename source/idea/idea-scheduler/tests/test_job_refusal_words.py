"""
What a person reads when a job is refused or the scheduler fails: words, no error
codes, no internals.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import sys

from ideadatamodel import errorcodes, exceptions
from ideadatamodel.aws import CheckServiceQuotaResult, ServiceQuota
from ideadatamodel.scheduler import (
    JobValidationResult,
    JobValidationResultEntry,
    SocaJob,
    SocaJobLicenseAsk,
    SocaJobParams,
)
from ideascheduler.app.scheduler.openpbs import (
    openpbs_api_invocation_context as hook_context,
)

HOOK = Path(__file__).parents[1] / 'resources/openpbs/hooks/openpbs_hook_handler.py'


def test_validation_result_lists_messages_without_codes():
    result = JobValidationResult(
        results=[
            JobValidationResultEntry(error_code='cpus', message='Ask for 2 or fewer.'),
            JobValidationResultEntry(error_code='memory', message='Ask for less.'),
        ]
    )
    assert str(result).splitlines() == ['- Ask for 2 or fewer.', '- Ask for less.']


def _run_hook_failing_with(error):
    source = HOOK.read_text()
    event = SimpleNamespace(
        type='queuejob',
        hook_name='idea-hook',
        job=SimpleNamespace(),
        accept=Mock(),
        reject=Mock(),
    )
    pbs = Mock()
    pbs.event.return_value = event
    message_block = source.split('# what a person submitting a job reads', 1)[1]
    message_block = message_block.split('def sanitize_error_message', 1)[0]
    namespace = {
        'e': event,
        'pbs': pbs,
        'sys': sys,
        'SocaException': type('SocaException', (Exception,), {}),
        'is_applicable': Mock(side_effect=error),
        'is_always_success_event': lambda e: False,
        'sanitize_error_message': lambda message: message.replace('"', ''),
    }
    exec('#' + message_block, namespace)
    entrypoint = source.split('\ne = pbs.event()\n', 1)[1]
    exec(compile(entrypoint, str(HOOK), 'exec'), namespace)
    return event, pbs


def test_hook_failure_logs_the_detail_and_tells_the_person_what_to_do():
    event, pbs = _run_hook_failing_with(KeyError('/var/spool/internal-path'))
    event.reject.assert_called_once_with(
        'The scheduler hit an internal error. Try again; if it repeats, tell an '
        'administrator and quote the job name.'
    )
    logged = ' '.join(str(call.args[1]) for call in pbs.logmsg.call_args_list)
    assert 'internal-path' in logged


def _hook_context():
    job = SocaJob(
        job_id='1',
        name='plasma',
        queue='normal',
        params=SocaJobParams(licenses=[SocaJobLicenseAsk(name='comsol', count=1)]),
    )
    return SimpleNamespace(
        job=job,
        app_context=Mock(),
        get_quotas_as_table=lambda quotas: 'table',
        _job_submission_result=SimpleNamespace(),
        add_incidentals_validation_entry=Mock(),
        check_licenses=Mock(),
    )


def test_a_license_that_is_not_set_up_says_what_to_do_without_the_raw_error():
    hook = _hook_context()
    hook.app_context.license_service.get_license_resource.side_effect = (
        exceptions.soca_exception(errorcodes.INVALID_PARAMS, 'row not found: secret')
    )
    hook_context.OpenPBSAPIInvocationContext.check_licenses(hook, Mock())
    hook.add_incidentals_validation_entry.assert_called_once_with(
        error_code='Licenses.NotFound',
        message="License comsol isn't set up on this cluster, so this job could never "
        'start. Remove it from your job script, or ask an administrator.',
    )


def test_a_low_quota_refusal_has_no_table(monkeypatch):
    insufficient = CheckServiceQuotaResult(
        quotas=[
            ServiceQuota(quota_name='Standard', available=0, desired=64, consumed=0)
        ]
    )
    util = Mock()
    util.check_service_quota.side_effect = exceptions.SocaException(
        error_code=errorcodes.SERVICE_QUOTA_NOT_AVAILABLE,
        message='quota',
        ref=SimpleNamespace(find_insufficient_quotas=lambda: insufficient.quotas),
    )
    monkeypatch.setattr(hook_context, 'JobProvisioningUtil', lambda **_kwargs: util)
    hook = _hook_context()
    hook_context.OpenPBSAPIInvocationContext.check_incidentals(hook)
    messages = [
        call.kwargs['message']
        for call in hook.add_incidentals_validation_entry.call_args_list
        if call.kwargs['error_code'] == 'ServiceQuota'
    ]
    assert messages == [
        'This job needs more AWS capacity than this cluster is allowed. '
        'Ask an administrator to request a higher quota.'
    ]
