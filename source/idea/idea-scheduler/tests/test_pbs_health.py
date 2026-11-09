"""the PBS check reads the server through a bounded process, and reports what it found."""

from ideascheduler.app.metrics.pbs_health import PbsHealthCollector

import json
import sys

import pytest


class Recorder:
    def __init__(self):
        self.levels = []

    def level(self, name, value, **tags):
        self.levels.append((name, value, tags))


def run(payload, code=0):
    argv = [
        sys.executable,
        '-c',
        f'import sys; print({payload!r}); sys.exit({code})',
    ]
    metrics = Recorder()
    PbsHealthCollector(argv).collect(metrics)
    return metrics.levels


def test_a_live_server_reports_up_and_its_jobs_by_state():
    levels = run(
        json.dumps(
            {
                'Server': {
                    'host': {
                        'server_state': 'Active',
                        'state_count': 'Transit:0 Queued:4 Running:2 ',
                    }
                }
            }
        )
    )
    assert ('scheduler.pbs_up', 1, {'reason': 'ok'}) in levels
    assert ('scheduler.jobs', 4, {'state': 'queued'}) in levels
    assert ('scheduler.jobs', 2, {'state': 'running'}) in levels


def test_a_dead_server_reports_down_with_how():
    assert ('scheduler.pbs_up', 0, {'reason': 'failed'}) in run('', code=1)


def test_a_server_not_active_reports_down_with_its_state():
    levels = run(json.dumps({'Server': {'host': {'server_state': 'Hot_Start'}}}))
    assert levels[0] == ('scheduler.pbs_up', 0, {'reason': 'hot_start'})


def test_a_server_mid_cycle_is_up():
    levels = run(json.dumps({'Server': {'host': {'server_state': 'Scheduling'}}}))
    assert levels[0] == ('scheduler.pbs_up', 1, {'reason': 'ok'})


@pytest.mark.parametrize(
    'payload', ['not json', '[]', '{"Server": []}', '{"Server": {}}', '{"Server": {"h": 1}}']
)
def test_an_answer_with_no_server_reports_down_and_never_ok(payload):
    # a 0 tagged ok said the server was fine and down at once; a raise sent no point at all
    levels = run(payload)
    assert levels[0] == ('scheduler.pbs_up', 0, {'reason': 'unreadable'})


def test_a_server_with_no_state_reports_down():
    levels = run(json.dumps({'Server': {'host': {'state_count': 'Queued:1'}}}))
    assert levels[0] == ('scheduler.pbs_up', 0, {'reason': 'no_state'})
