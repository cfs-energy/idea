"""
qmove through the batch system hook (dev rc.16 6a).

every qmove was refused with an internal error: the hook read Executable, which the job
object of a movejob event does not carry (the OpenPBS server log of the dev cluster:
"'_job' object has no attribute 'Executable'"), so the scheduler never saw a move. the
double below holds what that job object carries: the attributes the hook matrix marks
readable on movejob, without Executable, and reading any other raises as PBS does.
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSJob
from test_validate_job_ncpus import (  # noqa: F401 (fixture)
    PINNED_DNS,
    SOURCE_DNS,
    _restricted_fsx_move,
    _source_queue,
    submit,
)

HOOK = Path(__file__).parents[1] / 'resources/openpbs/hooks/openpbs_hook_handler.py'
HOOK_EVENT_MOVEJOB = 0x08


class MoveJobObject:
    """a movejob job object: what is set reads back, anything else is not there"""

    def __init__(self, **attributes):
        self.__dict__.update(attributes)

    def __getattr__(self, name):
        if name == 'Executable':
            raise AttributeError(f"'_job' object has no attribute '{name}'")
        return None

    def __setattr__(self, name, value):
        raise AttributeError(f'{name} is read-only on movejob')


class ReadOnlyResources(dict):
    def __setitem__(self, key, value):
        raise ValueError(f'Resource_List.{key} is read-only on movejob')


@pytest.fixture
def hook_module(monkeypatch):
    monkeypatch.setitem(sys.modules, 'pbs', Mock())
    namespace = {}
    source = HOOK.read_text().split('\ne = pbs.event()\n', 1)[0]
    exec(compile(source, str(HOOK), 'exec'), namespace)
    namespace['log_fine'] = lambda message: None
    return namespace


def _move_event(fsx_lustre):
    job = MoveJobObject(
        id='514.scheduler',
        queue='job-shared',
        project='project-a',
        Job_Name='job',
        Job_Owner='researcher@scheduler',
        Resource_List=ReadOnlyResources(
            select='1:ncpus=4:compute_node=tbd',
            walltime='00:10:00',
            job_group='g27f8153e',
            fsx_lustre=fsx_lustre,
        ),
    )
    return SimpleNamespace(
        type=HOOK_EVENT_MOVEJOB,
        hook_name='validate_job',
        requestor='root',
        requestor_host='scheduler',
        hook_type='site',
        job=job,
        src_queue='old-q',
        accept=Mock(),
        reject=Mock(),
    )


def _payload(hook_module, event):
    return hook_module['OpenPBSEvent'](event=event).build()


def test_the_move_event_is_serialized_without_the_attributes_it_lacks(hook_module):
    payload = _payload(hook_module, _move_event(PINNED_DNS))
    assert payload['type'] == 'movejob'
    assert payload['src_queue'] == 'old-q'
    assert payload['job']['queue'] == 'job-shared'
    assert 'Executable' not in payload['job']
    assert payload['job']['Resource_List']['fsx_lustre'] == PINNED_DNS


def test_an_allowed_move_is_accepted_and_writes_nothing_to_the_job(
    hook_module,
    submit,  # noqa: F811
):
    event = _move_event(PINNED_DNS)
    job = OpenPBSJob(**_payload(hook_module, event)['job'])
    result, _ = _restricted_fsx_move(submit, PINNED_DNS, src_queue='normal', job=job)
    assert result.accept is True, result.formatted_user_message

    # the hook's entry point with the scheduler's answer: accepted, nothing written
    entrypoint = HOOK.read_text().split('\ne = pbs.event()\n', 1)[1]
    exec(
        compile(entrypoint, str(HOOK), 'exec'),
        {
            **hook_module,
            'e': event,
            'pbs': Mock(),
            'is_applicable': lambda e: True,
            'invoke_soca_scheduler': lambda e: {
                'success': True,
                'payload': result.model_dump(),
            },
            'time': time,
        },
    )
    event.reject.assert_not_called()
    event.accept.assert_called_once()


def test_a_move_keeping_the_source_queues_fsx_reaches_its_refusal(
    hook_module,
    submit,  # noqa: F811
):
    from ideadatamodel import SocaJobParams
    from ideadatamodel.scheduler import SocaQueueManagementParams

    job = OpenPBSJob(**_payload(hook_module, _move_event(SOURCE_DNS))['job'])
    result, _ = _restricted_fsx_move(
        submit,
        SOURCE_DNS,
        src_queue='old-q',
        source_profile=_source_queue(),
        job=job,
        defaults=SocaJobParams(instance_types=['c5n.18xlarge']),
        queue_settings={
            'queue_management_params': SocaQueueManagementParams(
                restricted_parameters=['fsx_lustre']
            )
        },
    )
    assert result.accept is False
    assert (
        f'uses the FSx for Lustre file system {SOURCE_DNS}, the default of queue old-q'
        in result.formatted_user_message
    )
