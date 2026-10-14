"""The retired hook accepts without touching any job resources or dependencies."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

HOOK = Path(__file__).parents[1] / 'resources/openpbs/hooks/calculate_ncpus_hook.py'


@pytest.mark.parametrize(
    'resources',
    [
        {},
        {'select': '2:mem=8gb'},
        {'select': '1:ncpus=4+2:mem=8gb', 'instance_type': 'c7i.8xlarge'},
    ],
)
def test_pass_through(monkeypatch, resources):
    event = SimpleNamespace(
        job=SimpleNamespace(Resource_List=dict(resources)), accept=Mock()
    )
    monkeypatch.setitem(sys.modules, 'pbs', SimpleNamespace(event=lambda: event))
    exec(compile(HOOK.read_text(), str(HOOK), 'exec'), {})
    event.accept.assert_called_once_with()
    assert event.job.Resource_List == resources
