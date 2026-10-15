"""Unbaked micro instances must report failure before DNF starves SSM and cloud-init."""

import os
from pathlib import Path
import subprocess

import pytest

from test_bootstrap_shell_syntax import job
from test_image_bake_bootstrap import render

BOOTSTRAP = Path(__file__).resolve().parents[2] / 'idea-bootstrap'


@pytest.mark.parametrize('base_os', ['rocky9', 'amazonlinux2023', 'ubuntu2604'])
@pytest.mark.parametrize(
    'memory,swap,baked,accepted',
    [
        (720 * 1024, 0, None, False),
        (720 * 1024, 0, 'old-release', False),
        (720 * 1024, 0, 'test-release', True),
        (3579 * 1024, 0, None, True),
        (720 * 1024, 2 * 1024 * 1024, None, True),
        (2 * 1024 * 1024 - 1, 0, None, False),
        (2 * 1024 * 1024, 0, None, True),
    ],
)
def test_compute_memory_admission(tmp_path, base_os, memory, swap, baked, accepted):
    script = render(
        'compute-node/setup.sh.jinja2',
        base_os,
        variables={'job': job(), 'job_directory': '/apps/example/jobs/1'},
    )
    start = script.index('# Begin: Compute bootstrap memory check')
    end = script.index('# End: Compute bootstrap memory check')
    assert script.index('source "${SCRIPT_DIR}/../common/bootstrap_common.sh"') < start
    first_install = (
        'snap install ' if base_os.startswith('ubuntu') else 'package_transaction '
    )
    assert start < script.index(first_install)
    check = script[start:end]
    (tmp_path / 'meminfo').write_text(f'MemTotal: {memory} kB\nSwapTotal: {swap} kB\n')
    if baked:
        (tmp_path / 'baked-release').write_text(baked)
    check = check.replace('/proc/meminfo', str(tmp_path / 'meminfo')).replace(
        '/var/lib/idea/baked-release', str(tmp_path / 'baked-release')
    )
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'''source "{BOOTSTRAP}/common/bootstrap_common.sh"
BOOTSTRAP_DIR="{tmp_path}"
curl() {{ echo i-test; }}
aws() {{ printf '%s\\n' "$*" >> "$CALLS"; }}
timeout() {{ shift; "$@"; }}
{check}
echo admitted
''',
        ],
        env=dict(
            os.environ,
            IDEA_MODULE_VERSION='test-release',
            AWS_REGION='us-east-2',
            CALLS=str(tmp_path / 'tags'),
        ),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == (0 if accepted else 1), result.stdout + result.stderr
    assert ('admitted' in result.stdout) == accepted
    assert (tmp_path / 'bootstrap_failed.txt').exists() == (not accepted)
    if not accepted:
        assert (
            'Key=idea:BootstrapStatus,Value=insufficient-memory'
            in (tmp_path / 'tags').read_text()
        )
        assert 'larger instance or an image baked' in result.stdout
