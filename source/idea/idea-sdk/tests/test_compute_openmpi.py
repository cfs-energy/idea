"""Exercise rendered MPI bootstrap without installing packages or touching host paths."""

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

import pytest

from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils
from test_bootstrap_shell_syntax import Config, COMPUTE_BASE_OS, IDEA_BOOTSTRAP_DIR

BASH = shutil.which('bash')
pytestmark = pytest.mark.skipif(BASH is None, reason='bash is not available')
MPI_TEMPLATE = 'compute-node/_templates/openmpi.jinja2'
PBS_TEMPLATE = 'compute-node/_templates/configure_openpbs_compute_node.jinja2'


def render(template=MPI_TEMPLATE, base_os='amazonlinux2023', instance_type='m7i.large'):
    return (
        Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
        .get_template(template)
        .render(
            context=BootstrapContext(
                config=Config(),
                module_name='scheduler',
                module_id='scheduler',
                module_set='default',
                base_os=base_os,
                instance_type=instance_type,
            )
        )
    )


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/bash\n' + text)
    path.chmod(0o755)


def mpi(prefix, version):
    executable(prefix / 'bin/mpirun', f'echo "mpirun (Open MPI) {version}"\n')


def sandbox(text, root):
    for path in ('/opt/idea', '/opt/amazon', '/root/bootstrap', '/etc/profile.d'):
        text = text.replace(path, str(root) + path)
    return text


def prepare(root, efa=True):
    (root / 'etc/profile.d').mkdir(parents=True)
    if efa:
        mpi(root / 'opt/amazon/openmpi', '4.1.8')
        mpi(root / 'opt/amazon/openmpi5', '5.0.8')
    fake = root / 'bin'
    for cmd in (
        'yum',
        'apt',
        'apt-get',
        'gcc',
        'g++',
        'gfortran',
        'curl',
        'wget',
        'tar',
        'make',
    ):
        executable(fake / cmd, 'echo "$0" >> "$TEST_ROOT/forbidden"; exit 1\n')
    return dict(os.environ, PATH=f'{fake}:{os.environ["PATH"]}', TEST_ROOT=str(root))


def bootstrap(root, env, base_os='amazonlinux2023', instance_type='m7i.large'):
    script = """
set -e
function exit_fail() { echo "exit_fail" >> "$TEST_ROOT/forbidden"; exit 1; }
function package_transaction() { echo "package_transaction" >> "$TEST_ROOT/forbidden"; exit 1; }
function apt_update() { echo "apt_update" >> "$TEST_ROOT/forbidden"; exit 1; }
"""
    script += sandbox(render(base_os=base_os, instance_type=instance_type), root)
    script += '\necho scheduler-started\n'
    result = subprocess.run(
        [BASH, '-c', script], env=env, text=True, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'scheduler-started' in result.stdout
    assert not (root / 'forbidden').exists()
    return result


@pytest.mark.parametrize('base_os', COMPUTE_BASE_OS)
@pytest.mark.parametrize('instance_type', ['m7i.large', 'm7g.large'])
@pytest.mark.parametrize('efa', [True, False])
def test_selection_and_repeat_bootstrap(tmp_path, base_os, instance_type, efa):
    env = prepare(tmp_path, efa)
    before = sorted(tmp_path.rglob('*'))
    bootstrap(tmp_path, env, base_os, instance_type)
    profile = tmp_path / 'etc/profile.d/zz_idea_openmpi.sh'
    if efa:
        for major, target in [(4, 'openmpi'), (5, 'openmpi5')]:
            link = tmp_path / f'opt/idea/openmpi{major}'
            assert link.is_symlink()
            assert link.resolve() == tmp_path / f'opt/amazon/{target}'
        content = profile.read_text()
        assert profile.stat().st_mode & 0o777 == 0o644
        bootstrap(tmp_path, env, base_os, instance_type)
        assert profile.read_text() == content
    else:
        bootstrap(tmp_path, env, base_os, instance_type)
        assert sorted(tmp_path.rglob('*')) == before
        assert not profile.exists()
        assert not (tmp_path / 'opt/idea').exists()


@pytest.mark.parametrize('efa5', ['absent', 'not-executable'])
def test_without_executable_efa5_cleans_stale_links_only(tmp_path, efa5):
    env = prepare(tmp_path)
    launcher = tmp_path / 'opt/amazon/openmpi5/bin/mpirun'
    if efa5 == 'absent':
        launcher.unlink()
    else:
        launcher.chmod(0o644)
    prefix = tmp_path / 'opt/idea'
    prefix.mkdir()
    for major in (4, 5):
        (prefix / f'openmpi{major}').symlink_to(f'/missing/openmpi{major}')
    profile = tmp_path / 'etc/profile.d/zz_idea_openmpi.sh'
    profile.write_text('stale')
    efa4 = tmp_path / 'opt/amazon/openmpi/bin/mpirun'
    before = efa4.read_bytes()
    bootstrap(tmp_path, env)
    assert not profile.exists()
    assert not any(prefix.iterdir())
    assert efa4.read_bytes() == before
    # Cleanup must not delete real installations.
    for major in (4, 5):
        mpi(prefix / f'openmpi{major}', f'{major}.0.0')
    bootstrap(tmp_path, env)
    assert all((prefix / f'openmpi{major}/bin/mpirun').exists() for major in (4, 5))


def test_efa5_without_efa4(tmp_path):
    env = prepare(tmp_path)
    shutil.rmtree(tmp_path / 'opt/amazon/openmpi')
    bootstrap(tmp_path, env)
    assert (tmp_path / 'opt/idea/openmpi5').is_symlink()
    assert not (tmp_path / 'opt/idea/openmpi4').is_symlink()
    assert (tmp_path / 'etc/profile.d/zz_idea_openmpi.sh').exists()


def test_does_not_execute_efa_launcher(tmp_path):
    env = prepare(tmp_path)
    executable(tmp_path / 'opt/amazon/openmpi5/bin/mpirun', 'exit 1\n')
    bootstrap(tmp_path, env)
    assert (tmp_path / 'opt/idea/openmpi5').is_symlink()


@pytest.mark.parametrize('command', ['mkdir', 'ln', 'cat', 'chmod', 'rm'])
def test_setup_and_cleanup_errors_never_fail_node(tmp_path, command):
    env = prepare(tmp_path, efa=command != 'rm')
    executable(tmp_path / f'bin/{command}', 'exit 1\n')
    if command == 'rm':
        (tmp_path / 'etc/profile.d/zz_idea_openmpi.sh').write_text('stale')
    bootstrap(tmp_path, env)


def test_no_build_or_fatal_commands():
    script = render()
    for forbidden in (
        'build_compute_openmpi',
        '4.1.8',
        'http',
        'checksum',
        'gcc',
        'gfortran',
        'apt',
        'yum',
        'make',
        'exit_fail',
    ):
        assert forbidden not in script


def test_non_efa_pbs_keeps_system_path():
    defaults = re.search(r'echo -e "PATH=([^\n]+)', render(PBS_TEMPLATE)).group(1)
    assert defaults == '/opt/idea/openmpi5/bin:/bin:/usr/bin'
    libraries = re.search(r'LD_LIBRARY_PATH=([^\n]+)', render(PBS_TEMPLATE)).group(1)
    assert libraries == '/opt/idea/openmpi5/lib64:/opt/idea/openmpi5/lib'


@pytest.mark.parametrize('kind', ['login', 'pbs-batch', 'pbs-interactive'])
def test_default_environment_and_documented_opt_out(tmp_path, kind):
    env = prepare(tmp_path)
    assert bootstrap(tmp_path, env).returncode == 0
    # Use the actual rendered MoM environment, not a test copy of its defaults.
    pbs = sandbox(render(PBS_TEMPLATE), tmp_path)
    defaults = re.search(r'echo -e "(PATH=[^\n]+\nLD_LIBRARY_PATH=[^\n]+)', pbs).group(
        1
    )
    job_env = dict(line.split('=', 1) for line in defaults.splitlines())
    command = ''
    if kind == 'login':
        job_env = {
            'PATH': f'{tmp_path}/opt/amazon/openmpi/bin:/usr/bin:/bin',
            'LD_LIBRARY_PATH': '/custom/lib',
        }
        profile = shlex.quote(str(tmp_path / 'etc/profile.d/zz_idea_openmpi.sh'))
        command = f'. {profile}\n. {profile}\n'
    command += 'mpirun --version\nprintf "%s\\n" "$LD_LIBRARY_PATH"\n'
    docs = (
        Path(IDEA_BOOTSTRAP_DIR).parents[2]
        / 'docs/modules/hpc-workloads/user-documentation/submit-a-job.md'
    )
    opt_out = re.search(
        r'```bash\n(export PATH="/opt/idea/openmpi4.*?)```', docs.read_text(), re.S
    ).group(1)
    command += sandbox(opt_out, tmp_path)
    result = subprocess.run(
        [BASH, '--noprofile', '--norc', '-c', command],
        env=job_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert 'Open MPI) 5.' in lines[0]
    assert lines[1].startswith(
        f'{tmp_path}/opt/idea/openmpi5/lib64:{tmp_path}/opt/idea/openmpi5/lib'
    )
    assert lines[1].count(f'{tmp_path}/opt/idea/openmpi5/lib64') == 1
    if kind == 'login':
        assert lines[1].endswith(':/custom/lib')
    assert 'Open MPI) 4.' in lines[2]
