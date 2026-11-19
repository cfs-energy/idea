"""The DCV storage root in the owner's home is made and handed over without following a link."""

import os
import subprocess
from pathlib import Path

import pytest

from test_dcv_host_ami_builder_bootstrap import IDEA_BOOTSTRAP_DIR

TEMPLATE = (
    Path(IDEA_BOOTSTRAP_DIR) / 'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2'
)


def storage_root_block() -> str:
    text = TEMPLATE.read_text()
    start = text.index('  local DCV_STORAGE_ROOT=')
    end = text.index('  local GL_DISPLAYS_VALUE=')
    return text[start:end]


def run_block(home: Path) -> str:
    """runs the template's block with the home moved under tmp and chown recorded, not run"""
    block = storage_root_block().replace('/data/home/', f'{home}/')
    assert f'{home}/' in block
    script = (
        'log_warning() { echo "WARN $*"; }\n'
        'chown() { echo "CHOWN $*"; }\n'
        f'f() {{\n{block}\n}}\n'
        'f\n'
    )
    result = subprocess.run(
        ['bash', '-c', script],
        env={'IDEA_SESSION_OWNER': 'user-a', 'PATH': os.environ['PATH']},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def home(tmp_path):
    (tmp_path / 'user-a').mkdir()
    return tmp_path


def test_a_missing_storage_root_is_made_and_handed_to_the_owner(home):
    out = run_block(home)
    assert (home / 'user-a/storage-root').is_dir()
    assert out.strip() == f'CHOWN -h user-a {home}/user-a/storage-root'


def test_a_link_in_place_of_the_storage_root_is_left_alone(home, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp('elsewhere')
    (home / 'user-a/storage-root').symlink_to(elsewhere)
    out = run_block(home)
    assert 'CHOWN' not in out
    assert out.startswith('WARN ')


def test_a_file_in_place_of_the_storage_root_is_left_alone(home):
    (home / 'user-a/storage-root').write_text('')
    out = run_block(home)
    assert 'CHOWN' not in out
    assert out.startswith('WARN ')


def test_an_existing_storage_root_directory_is_not_touched(home):
    (home / 'user-a/storage-root').mkdir()
    assert run_block(home) == ''
