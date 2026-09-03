"""
Test Cases for the maintenance window gate on Scheduler.SubmitJob.

The gate lives in submit_job rather than the OpenPBS submit hook: the hook runs inside the
PBS server, so once the scheduler is stopped nothing runs it and qsub fails with a generic
connection error instead.
"""

from ideadatamodel import constants, errorcodes, exceptions
from ideasdk.api import ApiInvocationContext
from ideasdk.utils import Utils, GroupNameHelper
from ideascheduler.app.api.scheduler_api import (
    SchedulerAPI,
    DEFAULT_MAINTENANCE_MESSAGE,
    MAINTENANCE_ENABLED_CONFIG_KEY,
    MAINTENANCE_MESSAGE_CONFIG_KEY,
)

from typing import Dict
import pytest


CONTEXT_USER = 'testuser'
MAINTENANCE_MESSAGE = 'Scheduler closed for the 26.09 upgrade. Back Monday 09:00.'


def build_invocation_context(context, payload: Dict) -> ApiInvocationContext:
    return ApiInvocationContext(
        context=context,
        request={
            'header': {
                'namespace': 'Scheduler.SubmitJob',
                'request_id': Utils.uuid(),
            },
            'payload': payload,
        },
        invocation_source=constants.API_INVOCATION_SOURCE_HTTP,
        group_name_helper=GroupNameHelper(context=context),
        logger=context.logger(),
    )


@pytest.fixture()
def scheduler_api(context, monkeypatch):
    monkeypatch.setattr(
        ApiInvocationContext, 'get_username', lambda _: CONTEXT_USER, raising=False
    )
    return SchedulerAPI(context=context)


@pytest.fixture()
def maintenance_window(context):
    """
    Opens the window for one test and closes it again, so the key does not leak into the rest of
    the suite through the shared mock config.
    """

    def open_window(message: str = MAINTENANCE_MESSAGE):
        context.config().put(MAINTENANCE_ENABLED_CONFIG_KEY, True)
        context.config().put(MAINTENANCE_MESSAGE_CONFIG_KEY, message)

    yield open_window
    context.config().put(MAINTENANCE_ENABLED_CONFIG_KEY, False)
    context.config().put(MAINTENANCE_MESSAGE_CONFIG_KEY, '')


def test_submit_job_is_refused_while_the_window_is_open(
    scheduler_api, context, maintenance_window
):
    maintenance_window()
    api_context = build_invocation_context(
        context,
        {
            'job_owner': CONTEXT_USER,
            'job_script': Utils.base64_encode('#!/bin/bash\necho hello\n'),
            'job_script_interpreter': 'pbs',
        },
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler_api.submit_job(api_context)

    assert exc_info.value.error_code == errorcodes.JOB_SUBMISSION_FAILED
    assert exc_info.value.message == f'Cluster is in maintenance: {MAINTENANCE_MESSAGE}'


def test_submit_job_names_the_cluster_state_before_naming_a_bad_request(
    scheduler_api, context, maintenance_window
):
    """
    the gate runs before request validation: a user submitting into a closed cluster has to hear
    that the cluster is closed, not that some field of their request was malformed.
    """
    maintenance_window()
    api_context = build_invocation_context(context, {'job_owner': CONTEXT_USER})

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler_api.submit_job(api_context)

    assert exc_info.value.message == f'Cluster is in maintenance: {MAINTENANCE_MESSAGE}'
    assert 'job_script is required' not in exc_info.value.message


def test_submit_job_falls_back_to_a_generic_message(
    scheduler_api, context, maintenance_window
):
    """the banner and this rejection share the setting, so an empty message must still read"""
    maintenance_window(message='')
    api_context = build_invocation_context(context, {'job_owner': CONTEXT_USER})

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler_api.submit_job(api_context)

    assert (
        exc_info.value.message
        == f'Cluster is in maintenance: {DEFAULT_MAINTENANCE_MESSAGE}'
    )


def test_submit_job_is_unaffected_while_the_window_is_closed(scheduler_api, context):
    """
    with no window open, validation proceeds and stops on the missing job_script.
    """
    api_context = build_invocation_context(context, {'job_owner': CONTEXT_USER})

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler_api.submit_job(api_context)

    assert 'job_script is required' in exc_info.value.message
    assert 'maintenance' not in exc_info.value.message


def test_submit_job_is_unaffected_when_the_setting_is_absent(scheduler_api, context):
    """
    a cluster upgraded from a release without these keys has no maintenance entry at all. the
    read must default to off rather than raising.
    """
    assert (
        context.config().get_bool(MAINTENANCE_ENABLED_CONFIG_KEY, default=False)
        is False
    )
    api_context = build_invocation_context(context, {'job_owner': CONTEXT_USER})

    with pytest.raises(exceptions.SocaException) as exc_info:
        scheduler_api.submit_job(api_context)

    assert 'job_script is required' in exc_info.value.message
