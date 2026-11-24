"""The directory join nonce is 128 random bits, with or without openssl on the host."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_bootstrap_shell_syntax import BASH, IDEA_BOOTSTRAP_DIR

LINUX = Path(IDEA_BOOTSTRAP_DIR, '_templates/linux/join_activedirectory.jinja2')
WINDOWS = Path(IDEA_BOOTSTRAP_DIR, '_templates/windows/join_activedirectory.jinja2')


def nonce_lines() -> str:
    head = LINUX.read_text().split('AD_AUTHORIZATION_INSTANCE_ID=')[0]
    return '\n'.join(
        line for line in head.splitlines() if 'AD_AUTHORIZATION_NONCE' in line
    )


def make_nonce(path: str) -> str:
    script = (
        f'set -euo pipefail\n{nonce_lines()}\nprintf %s "${{AD_AUTHORIZATION_NONCE}}"\n'
    )
    return subprocess.run(
        [BASH, '-c', script],
        env={'PATH': path},
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def path_without_openssl(tmp_path) -> str:
    # only the tools the fallback needs
    for tool in ('od', 'tr'):
        (tmp_path / tool).symlink_to(shutil.which(tool))
    return str(tmp_path)


@pytest.mark.parametrize('openssl', [True, False])
def test_the_linux_nonce_is_32_hex_characters_and_differs_each_time(tmp_path, openssl):
    path = (
        '/usr/bin:/bin:/opt/homebrew/bin' if openssl else path_without_openssl(tmp_path)
    )
    if openssl and not shutil.which('openssl', path=path):
        pytest.skip('no openssl on this machine')
    first, second = make_nonce(path), make_nonce(path)
    assert re.fullmatch(r'[0-9a-f]{32}', first), first
    assert first != second


def test_the_windows_nonce_is_a_random_guid():
    text = WINDOWS.read_text()
    assert "$AD_AUTHORIZATION_NONCE = [guid]::NewGuid().ToString('N')" in text
    assert 'Get-Random -Maximum 32767' not in text
