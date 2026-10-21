"""Legacy qsub clients must reach the shell on newer OpenPBS execution hosts."""

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

HOOK = Path(__file__).parents[1] / 'resources/openpbs/hooks/openpbs_hook_handler.py'


def submit(variables, interactive=True, event_type='queuejob'):
    event = SimpleNamespace(
        type=event_type,
        job=SimpleNamespace(interactive=interactive, Variable_List=dict(variables)),
        accept=Mock(),
        reject=Mock(),
    )
    # Exercise the real hook entry point after successful scheduler admission.
    entrypoint = HOOK.read_text().split('\ne = pbs.event()\n', 1)[1]
    exec(
        compile(entrypoint, str(HOOK), 'exec'),
        {
            'e': event,
            'pbs': Mock(),
            'is_applicable': lambda e: True,
            'PRE_EXECUTION_HOOKS': ['queuejob', 'modifyjob', 'movejob'],
            'HOOK_EVENT_QUEUEJOB': 'queuejob',
            'HOOK_EVENT_MODIFYJOB': 'modifyjob',
            'invoke_soca_scheduler': lambda e: {
                'success': True,
                'payload': {'accept': True},
            },
            'time': time,
            'start_time': time.time() * 1000,
        },
    )
    event.accept.assert_called_once()
    event.reject.assert_not_called()
    return event.job.Variable_List


def test_legacy_interactive_client_gets_required_auth_fields():
    assert submit({'PBS_O_HOST': 'submit.example', 'USER_VALUE': 'preserved'}) == {
        'PBS_O_HOST': 'submit.example',
        'USER_VALUE': 'preserved',
        'PBS_O_INTERACTIVE_AUTH_METHOD': 'resvport',
        'PBS_O_INTERACTIVE_ENCRYPT_METHOD': '',
    }


@pytest.mark.parametrize(
    'auth,encrypt', [('resvport', ''), ('munge', ''), ('gss', 'gss')]
)
def test_client_auth_and_encryption_are_never_overridden(auth, encrypt):
    variables = {
        'PBS_O_INTERACTIVE_AUTH_METHOD': auth,
        'PBS_O_INTERACTIVE_ENCRYPT_METHOD': encrypt,
    }
    assert submit(variables) == variables


@pytest.mark.parametrize(
    'interactive,event_type',
    [(False, 'queuejob'), (None, 'queuejob'), (True, 'modifyjob'), (True, 'movejob')],
)
def test_other_jobs_and_events_are_unchanged(interactive, event_type):
    variables = {'PBS_O_HOST': 'submit.example'}
    assert submit(variables, interactive, event_type) == variables
