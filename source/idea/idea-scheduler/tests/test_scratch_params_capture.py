"""
The scratch parameters the scheduler writes for each kind of job, as the compute-node bootstrap
receives them. idea-sdk/tests/fixtures/scheduler-scratch-params.json holds this output; the
bootstrap scratch tests render from it, so they see what the scheduler really writes. Run with
IDEA_CAPTURE_SCRATCH_PARAMS=1 to rewrite the fixture after a scheduler change.
"""

import json
import os
from pathlib import Path

from test_job_param_builder import build_and_validate

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / 'idea-sdk'
    / 'tests'
    / 'fixtures'
    / 'scheduler-scratch-params.json'
)
CASES = {
    'default': {},
    # -l scratch_size reaches the builder as scratch_storage_size, resolved to memory (openpbs_converter)
    'ebs': {'scratch_storage_size': {'value': 100, 'unit': 'gb'}},
    'new_fsx': {'fsx_lustre': 'true'},
    'existing_fsx': {'fsx_lustre': 'fs-0123456789abcdef0.fsx.us-east-1.amazonaws.com'},
    'explicit_opt_out': {'enable_scratch': 'false'},
}


def scratch_params(context, extra):
    result = build_and_validate(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro', **extra},
    )
    assert result.success, result.validation_result
    params = result.job_params
    return {
        'enable_scratch': params.enable_scratch,
        'scratch_storage_size': (
            None
            if params.scratch_storage_size is None
            else {
                'value': params.scratch_storage_size.value,
                'unit': params.scratch_storage_size.unit.value,
            }
        ),
        'fsx_lustre': (
            None
            if params.fsx_lustre is None
            else params.fsx_lustre.dict(exclude_none=True)
        ),
    }


def test_scratch_params_fixture_matches_the_scheduler(context):
    captured = {name: scratch_params(context, extra) for name, extra in CASES.items()}
    if os.environ.get('IDEA_CAPTURE_SCRATCH_PARAMS') == '1':
        FIXTURE.write_text(json.dumps(captured, indent=2, sort_keys=True) + '\n')
    assert json.loads(FIXTURE.read_text()) == captured
