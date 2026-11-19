"""
Every gpg call a host makes goes through one safe invocation. Amazon Linux 2023 ships
gnupg2-minimal with no gpg-agent: a gpg that tries to start one exits 2 even when the import
itself worked, so a signature check reported "not signed" and every AL2023 bake and unbaked node
stopped. The first fix patched one call site (the AWS CLI in user data) and the next one failed at
the CloudWatch agent. The bootstrap templates and shared library use idea_gpg; the user data
builders, which run before that library exists, carry the same flags on each call.
"""

import re
import subprocess
from pathlib import Path

import pytest

from test_bootstrap_shell_syntax import BASH, IDEA_BOOTSTRAP_DIR
from test_image_bake_bootstrap import render

SOURCE = Path(IDEA_BOOTSTRAP_DIR).parents[0]
BUILDERS = [
    SOURCE
    / 'idea-sdk'
    / 'src'
    / 'ideasdk'
    / 'bootstrap'
    / 'bootstrap_userdata_builder.py',
    SOURCE / 'ideactl' / 'src' / 'cdk' / 'userdata.ts',
]
COMMON = Path(IDEA_BOOTSTRAP_DIR, 'common', 'bootstrap_common.sh')
SAFE = ('--batch', '--no-autostart', '--homedir')
# gpg or gpgv where a shell runs it: line start, after a pipe, a list operator, $( or a keyword
COMMAND = re.compile(
    r'(?:^|[;&|(]|\$\(|!|\b(?:if|then|do|else|sudo|exec|timeout\s+\d+))\s*(gpg2?|gpgv2?)(?=\s|$)'
)


def gpg_commands(text: str):
    """(line number, line) of every line that runs gpg or gpgv, comments left out"""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        code = line.split(' #', 1)[0]
        if code.lstrip().startswith(('#', '{#')):
            continue
        if COMMAND.search(code):
            found.append((number, line.strip()))
    return found


def bootstrap_files():
    root = Path(IDEA_BOOTSTRAP_DIR)
    return sorted(
        p
        for p in root.rglob('*')
        if p.is_file()
        and p.suffix in ('.jinja2', '.sh', '.ps1', '.psm1', '')
        and 'GPG-KEY' not in p.name
    )


def test_every_bootstrap_gpg_call_goes_through_the_helper():
    direct = []
    for path in bootstrap_files():
        for number, line in gpg_commands(path.read_text(errors='replace')):
            # the helper's own call is the one direct use
            if path == COMMON and line.startswith(
                'gpg --batch --no-autostart --homedir "${home}" "$@"'
            ):
                continue
            direct.append(f'{path.relative_to(IDEA_BOOTSTRAP_DIR)}:{number}: {line}')
    assert direct == []
    helper = re.search(
        r'function idea_gpg \(\) \{\n.*?\n\}\n', COMMON.read_text(), re.S
    )[0]
    for flag in SAFE:
        assert flag in helper


@pytest.mark.parametrize('builder', BUILDERS, ids=lambda p: p.name)
def test_every_user_data_gpg_call_carries_the_safe_flags(builder):
    calls = gpg_commands(builder.read_text())
    assert calls, 'the user data verifies the AWS CLI with gpg'
    for number, line in calls:
        for flag in SAFE:
            assert flag in line, f'{builder.name}:{number}: {line}'


@pytest.mark.parametrize(
    'template, base_os',
    [
        ('_templates/linux/cloudwatch_agent.jinja2', 'amazonlinux2023'),
        ('_templates/linux/dcv_server.jinja2', 'ubuntu2404'),
        ('_templates/linux/dcv_session_manager_agent.jinja2', 'ubuntu2404'),
        ('_templates/linux/dcv_package_trust.jinja2', 'ubuntu2204'),
        ('_templates/linux/set_kernel.jinja2', 'ubuntu2404'),
        ('_templates/linux/fsx_lustre_client.jinja2', 'ubuntu2404'),
    ],
)
def test_the_rendered_templates_run_no_bare_gpg(template, base_os):
    text = render(template, base_os, instance_type='g5.xlarge')
    # the branch that verifies or converts a key was rendered, and it uses the helper
    assert 'idea_gpg ' in text or 'verify_detached_signature ' in text
    assert gpg_commands(text) == []


FAKE_MINIMAL_GPG = r"""#!/bin/bash
# AL2023 gnupg2-minimal: importing a new key probes gpg-agent, which is not installed; with
# autostart on, the failed spawn is logged as an error and gpg exits 2 after importing
args=" $* "
if [[ "$args" == *" --import "* ]]; then
  if [[ "$args" != *" --no-autostart "* ]]; then
    echo "gpg: error running '/usr/bin/gpg-agent': probably not installed" >&2
    exit 2
  fi
  exit 0
fi
if [[ "$args" == *" --verify "* ]]; then
  echo "[GNUPG:] VALIDSIG 0000 2026-11-19 0 4 0 1 10 00 937616F3450B7D806CBD9725D58167303B789C72"
  exit 0
fi
exit 0
"""


@pytest.mark.skipif(BASH is None, reason='bash not installed')
@pytest.mark.parametrize('call, expected', [('helper', 0), ('bare', 1)])
def test_a_signature_check_passes_with_the_minimal_gpg(tmp_path, call, expected):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'gpg').write_text(FAKE_MINIMAL_GPG)
    (bin_dir / 'gpg').chmod(0o755)
    for name in ('key.asc', 'pkg.rpm.sig', 'pkg.rpm'):
        (tmp_path / name).write_text('x')
    common = COMMON.read_text()
    if call == 'bare':
        # the positive control: the call as it was before the helper
        common = common.replace(
            'gpg --batch --no-autostart --homedir', 'gpg --batch --homedir'
        )
    script = (
        common
        + '\nlog_info() { echo "$*"; }\nlog_error() { echo "$*"; }\n'
        + f'verify_detached_signature {tmp_path}/key.asc 937616F3450B7D806CBD9725D58167303B789C72 '
        + f'{tmp_path}/pkg.rpm.sig {tmp_path}/pkg.rpm\n'
    )
    result = subprocess.run(
        [BASH, '-c', script],
        capture_output=True,
        text=True,
        env={'PATH': f'{bin_dir}:/usr/bin:/bin'},
    )
    assert result.returncode == expected, result.stdout + result.stderr
