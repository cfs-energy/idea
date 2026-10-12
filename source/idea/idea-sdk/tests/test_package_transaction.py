"""Exercise the shared helper with fake package managers; never touch host packages."""

import os
from pathlib import Path
import subprocess
import re

from test_image_bake_bootstrap import render

import pytest

COMMON = (
    Path(__file__).resolve().parents[2] / 'idea-bootstrap/common/bootstrap_common.sh'
)


@pytest.mark.parametrize('manager', ['dnf', 'yum'])
@pytest.mark.parametrize('pipefail', [False, True])
@pytest.mark.parametrize(
    'scenario,attempts,code',
    [
        ('transient', 3, 0),
        ('dependency recovers', 3, 0),
        ('disk recovers', 3, 0),
        ('silent failure', 4, 42),
        ('nothing provides', 5, 0),
        ('best candidate', 5, 0),
        ('permanent', 4, 42),
        ('missing package', 1, 42),
        ('nobest fails', 5, 42),
    ],
)
def test_package_transaction(tmp_path, manager, pipefail, scenario, attempts, code):
    executable = tmp_path / manager
    executable.write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
[[ "$1" == clean ]] && exit 0
count=$(cat "$COUNT" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$COUNT"
[[ ( "$SCENARIO" == transient || "$SCENARIO" == "dependency recovers" || "$SCENARIO" == "disk recovers" ) && $count -ge 3 ]] && { echo success; exit 0; }
[[ "$SCENARIO" == "silent failure" ]] && exit 42
[[ "$1" == --nobest && "$SCENARIO" != 'nobest fails' ]] && { echo success; exit 0; }
case "$SCENARIO" in
  'disk recovers') echo 'Error: No space left on device' >&2 ;;
  transient) echo 'Error: repository unavailable' >&2 ;;
  permanent) echo 'Error: Failed to download metadata for repo' >&2 ;;
  'missing package') echo 'Error: Unable to find a match: kernel-devel-6.12.55' >&2 ;;
  'best candidate') echo 'Error: cannot install the best candidate for the job' >&2 ;;
  *) echo 'Error: nothing provides openssl-libs = 1:3.5.8-2.el9_8' >&2 ;;
esac
exit 42
""")
    executable.chmod(0o700)
    sleep = tmp_path / 'sleep'
    sleep.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$SLEEPS"\n')
    sleep.chmod(0o700)
    env = dict(
        os.environ,
        PATH=f'{tmp_path}:{os.environ["PATH"]}',
        TMPDIR=str(tmp_path),
        SCENARIO=scenario,
        CALLS=str(tmp_path / 'calls'),
        COUNT=str(tmp_path / 'count'),
        SLEEPS=str(tmp_path / 'sleeps'),
    )
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'''set -eE {'-o pipefail' if pipefail else ''}
source "{COMMON}"
trap 'echo "ERR:$?" >&2' ERR
package_transaction {manager} --exclude=unwanted groupinstall -y "Server with GUI"
echo completed
''',
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == code, result.stdout + result.stderr
    calls = (tmp_path / 'calls').read_text().splitlines()
    transactions = [line for line in calls if not line.startswith('clean ')]
    assert len(transactions) == attempts
    assert all(
        line.endswith('--exclude=unwanted groupinstall -y Server with GUI')
        for line in transactions
    )
    assert '--skip-broken' not in '\n'.join(calls)
    fallback = scenario not in (
        'transient',
        'dependency recovers',
        'disk recovers',
        'permanent',
        'missing package',
        'silent failure',
    )
    retries = {
        'transient': 2,
        'dependency recovers': 2,
        'disk recovers': 2,
        'missing package': 0,
    }.get(scenario, 3)
    assert sum('--nobest' in line for line in transactions) == int(fallback)
    sleeps = tmp_path / 'sleeps'
    assert (sleeps.read_text().splitlines() if sleeps.exists() else []) == [
        '15',
        '30',
        '60',
    ][:retries]
    assert calls.count('clean packages metadata') == retries
    assert result.stdout.count('refreshing metadata and retrying') == retries
    assert result.stdout.count('retrying once with --nobest') == int(fallback)
    assert ('completed' in result.stdout) == (code == 0)
    assert ('success' in result.stdout.splitlines()) == (code == 0)
    assert len(re.findall(r'^Error:', result.stdout, re.M)) == int(
        code != 0 and scenario != 'silent failure'
    )
    assert ('ERR:42' in result.stderr) == (code != 0)
    assert len(re.findall(r'^\[retried\] Error:', result.stdout, re.M)) == (
        0 if scenario == 'silent failure' else attempts - 1
    )
    # Execute the real rendered bootstrap_ok, including its FATAL_LINE, for both builders.
    for kind, stage in [('dcv-host', 'dcv'), ('compute-node', 'drivers')]:
        rendered = render(f'{kind}-ami-builder/image_checks.sh.jinja2')
        fatal = next(
            line for line in rendered.splitlines() if line.startswith('FATAL_LINE=')
        )
        function = rendered.split('bootstrap_ok() {', 1)[1].split('\nkernel_ok()', 1)[0]
        state = tmp_path / kind
        state.mkdir()
        for marker in ('setup', 'packages', stage):
            (state / f'bake-{marker}.ok').touch()
        (state / 'transaction.log').write_text(result.stdout + result.stderr)
        scan = subprocess.run(
            ['bash', '-c', fatal + '\nbootstrap_ok() {' + function + '\nbootstrap_ok'],
            env=dict(
                env,
                STATE=str(state),
                BOOTSTRAP_DIR=str(state),
                IDEA_DCV_HOST_AMI_BUILDER_LOGS_DIR=str(state),
                IDEA_COMPUTE_NODE_AMI_BUILDER_LOGS_DIR=str(state),
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert scan.returncode == (1 if code else 0), scan.stdout + scan.stderr
    # The helper removes its temporary transcript on both success and final failure.
    assert not list(tmp_path.glob('tmp.*'))
