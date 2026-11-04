"""Keep the reporting regression suite reachable from both CI and local test:all."""

from pathlib import Path
import tomllib

import yaml


ROOT = Path(__file__).resolve().parents[4]


def test_data_model_suite_is_in_ci_and_local_runners():
    workflow = yaml.safe_load((ROOT / '.github/workflows/unit_tests.yaml').read_text())
    assert 'data-model' in workflow['jobs']['suites']['strategy']['matrix']['suite']
    tasks = tomllib.loads((ROOT / 'mise.toml').read_text())['tasks']
    assert tasks['test:data-model']['run'] == (
        'node scripts/tooling/test-service.mjs data-model'
    )
    assert (
        "'data-model': 'ideadatamodel'"
        in (ROOT / 'scripts/tooling/test-service.mjs').read_text()
    )
    assert "'data-model'" in (ROOT / 'scripts/tooling/test-all.mjs').read_text()
