"""
Interim kernel hardening on every Linux host IDEA bootstraps and on running desktops through the
refresh: algif_aead, esp4 and esp6 cannot be loaded (a modprobe.d install rule), behind a flag. A
module that is already loaded is reported, never unloaded. A re-run must change nothing.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)
TEMPLATE = '_templates/linux/kernel_module_hardening.jinja2'
FLAG_KEY = 'global-settings.kernel_hardening.block_modules'
CONF = 'etc/modprobe.d/90-idea-kernel-modules.conf'
LINUX = (
    'amazonlinux2023',
    'rhel8',
    'rhel9',
    'rhel10',
    'rocky8',
    'rocky9',
    'rocky10',
    'ubuntu2204',
    'ubuntu2404',
    'ubuntu2604',
)
RULES = [
    'install algif_aead /bin/false',
    'install esp4 /bin/false',
    'install esp6 /bin/false',
]


def render(config, base_os: str, **variables) -> str:
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    return env.get_template(TEMPLATE).render(
        context=BootstrapContext(
            config=config,
            module_name='vdc',
            module_id='vdc',
            module_set='default',
            base_os=base_os,
            instance_type='m6a.large',
        ),
        **variables,
    )


@pytest.mark.parametrize(
    'script',
    [
        'virtual-desktop-host-linux/setup.sh.jinja2',
        'compute-node/setup.sh.jinja2',
        'openldap-server/setup.sh.jinja2',
    ],
)
def test_every_linux_host_bootstrap_includes_it(script):
    assert (
        f"{{% include '{TEMPLATE}' %}}" in Path(IDEA_BOOTSTRAP_DIR, script).read_text()
    )


def test_windows_renders_nothing(context):
    assert 'modprobe' not in render(context.config(), 'windows2022')


def run(tmp_path: Path, shell: str, block: str, loaded=()):
    root = tmp_path / 'root'
    for module in loaded:
        (root / 'sys/module' / module).mkdir(parents=True)
    (root / 'etc').mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    for name in 'cat mkdir printf rm'.split():
        if not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(shutil.which(name))
    script = 'log_warning() { echo "$@"; }\n' + block.replace(
        '/etc/', f'{root}/etc/'
    ).replace('/sys/', f'{root}/sys/')
    result = subprocess.run(
        [shell],
        input=script,
        text=True,
        capture_output=True,
        env={'PATH': str(bin_dir)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return root, result.stdout


SHELLS = [s for s in ('bash', 'dash') if shutil.which(s)]


@pytest.mark.parametrize('shell', SHELLS)
@pytest.mark.parametrize('base_os', LINUX)
def test_the_rule_is_written_once_and_a_rerun_changes_nothing(
    context, tmp_path, shell, base_os
):
    block = render(context.config(), base_os)
    root, _ = run(tmp_path, shutil.which(shell), block)
    conf = root / CONF
    assert conf.read_text().splitlines()[1:] == RULES
    before = conf.stat().st_mtime_ns
    run(tmp_path, shutil.which(shell), block)
    assert conf.stat().st_mtime_ns == before


@pytest.mark.parametrize('shell', SHELLS)
def test_a_loaded_module_is_reported_and_left_alone(context, tmp_path, shell):
    block = render(context.config(), 'ubuntu2404', bootstrap_refresh=True)
    assert 'rmmod' not in block and 'modprobe -r' not in block
    root, output = run(tmp_path, shutil.which(shell), block, loaded=('esp4',))
    assert 'esp4 is already loaded; it stays loaded until the next boot' in output
    assert 'algif_aead is already loaded' not in output
    assert (root / 'sys/module/esp4').is_dir()


@pytest.mark.parametrize('shell', SHELLS)
def test_turning_the_flag_off_removes_the_rule(context, tmp_path, shell):
    root, _ = run(tmp_path, shutil.which(shell), render(context.config(), 'rocky9'))
    assert (root / CONF).exists()
    run(
        tmp_path,
        shutil.which(shell),
        render(context.config(), 'rocky9', block_kernel_modules=False),
    )
    assert not (root / CONF).exists()


def test_the_flag_is_read_from_the_cluster_config(context):
    config = context.config()
    config.put(FLAG_KEY, False)
    try:
        rendered = render(config, 'rocky9')
    finally:
        config.put(FLAG_KEY, True)
    assert 'install esp4' not in rendered
    assert 'rm -f /etc/modprobe.d/90-idea-kernel-modules.conf' in rendered
