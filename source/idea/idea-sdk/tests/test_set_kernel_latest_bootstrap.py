"""
Kernel management installs the newest kernel the host can run instead of keeping the one its
image booted: a security fix (CVE-2026-98432) ships as a new release of the same series, and
a bake that kept the stock kernel would snapshot the vulnerable one.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)
TEMPLATE = '_templates/linux/set_kernel.jinja2'
DNF_FAMILY = (
    'amazonlinux2023',
    'rhel8',
    'rhel9',
    'rhel10',
    'rocky8',
    'rocky9',
    'rocky10',
)


def render(config, base_os: str, lustre=False, **variables) -> str:
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    context = BootstrapContext(
        config=config,
        module_name='vdc',
        module_id='vdc',
        module_set='default',
        base_os=base_os,
        instance_type='m6a.large',
    )
    if lustre:
        context.has_storage_provider = lambda provider: provider == 'fsx_lustre'
    return env.get_template(TEMPLATE).render(context=context, **variables)


@pytest.mark.parametrize('base_os', DNF_FAMILY)
def test_a_host_in_the_target_series_updates_before_it_is_locked(context, base_os):
    rendered = render(context.config(), base_os)
    branch = rendered[rendered.index('Confirmed running target kernel series') :]
    assert branch.index('update_within_target_series') < branch.index(
        'apply_kernel_holds'
    )
    # the lock follows the kernel the host will boot
    assert 'current_kernel="${TARGET_KERNEL_RELEASE:-${current_kernel}}"' in rendered


def test_ubuntu_bake_takes_the_newest_lustre_kernel_not_the_running_one(context):
    rendered = render(
        context.config(), 'ubuntu2404', lustre=True, ubuntu_desktop_bake=True
    )
    selection = rendered[rendered.index('NEWEST_FSX_KERNEL_ABI=""') :]
    selection = selection[: selection.index('if [[ -z "${SELECTED_KERNEL_ABI}" ]]')]
    assert '== "${TARGET_KERNEL_ABI}"' not in selection
    assert 'SELECTED_KERNEL_ABI="${candidate_abi}"\n        break' in selection


def test_ubuntu_bake_without_lustre_takes_the_newest_kernel_of_its_series(context):
    rendered = render(context.config(), 'ubuntu2404', ubuntu_desktop_bake=True)
    block = rendered[rendered.index('a bake takes the newest kernel') :]
    block = block[: block.index('TARGET_KERNEL_VERSION=')]
    assert 'sort -Vr' in block
    assert 'TARGET_KERNEL_ABI="${candidate_abi}"' in block


def function_body(rendered: str, name: str) -> str:
    found = re.search(rf'function {name}\(\) {{\n.*?\n}}\n', rendered, re.S)
    assert found, name
    return found[0]


class Host:
    """update_within_target_series against stub dnf, grubby and uname"""

    def __init__(
        self,
        tmp_path: Path,
        running: str,
        newest: str,
        install_ok=True,
        initramfs=True,
        dracut_ok=False,
        grubby_ok=True,
        package_default=False,
    ):
        self.boot = tmp_path / 'boot'
        self.bin = tmp_path / 'bin'
        self.calls = tmp_path / 'calls'
        self.boot.mkdir()
        self.bin.mkdir()
        self.calls.write_text('')
        for name in (
            'cat',
            'printf',
            'sort',
            'tail',
            'touch',
            'basename',
            'timeout',
            'mkdir',
            'rm',
        ):
            (self.bin / name).symlink_to(shutil.which(name))
        log = f'echo "$(basename "$0") $*" >> {self.calls}\n'
        self.stub('uname', f'echo {running}')
        self.stub('rpm', 'echo kernel6.12')
        image = f'{self.boot}/initramfs-{newest}.img'
        installed = (
            f'touch {self.boot}/vmlinuz-{newest}'
            + (f'; echo x > {image}' if initramfs else '')
            if install_ok
            else 'exit 1'
        )
        self.stub('dracut', log + (f'echo x > {image}' if dracut_ok else 'exit 1'))
        self.stub(
            'dnf',
            log + 'case "$1" in\n'
            f'  repoquery) echo {newest} ;;\n'
            f'  install) {installed} ;;\n'
            'esac',
        )
        default = tmp_path / 'default_kernel'
        # the kernel package may have selected itself as the default already
        default.write_text(
            f'{self.boot}/vmlinuz-{newest if package_default else running}\n'
        )
        self.default = default
        self.stub(
            'grubby',
            log + 'case "$1" in\n'
            f'  --default-kernel) cat {default} ;;\n'
            + (
                f'  --set-default) echo "$2" > {default} ;;\n'
                if grubby_ok
                else '  --set-default) exit 1 ;;\n'
            )
            + 'esac',
        )

    def stub(self, name, body):
        (self.bin / name).write_text('#!/bin/sh\n' + body + '\n')
        (self.bin / name).chmod(0o755)

    def run(self, rendered: str, exit_code=0):
        common = Path(IDEA_BOOTSTRAP_DIR, 'common/bootstrap_common.sh').read_text()
        function = (
            ''.join(
                re.search(rf'function {name} \(\) {{\n.*?\n}}\n', common, re.S)[0]
                for name in (
                    'kernel_initramfs_ready',
                    'boot_loader_default_kernel',
                    'restore_default_kernel',
                    'drop_rescue_images',
                )
            )
            + function_body(rendered, 'keep_running_kernel_default')
            + function_body(rendered, 'update_within_target_series')
        ).replace('/boot/', f'{self.boot}/')
        script = (
            'log_warning() { echo "warning $*" >> "$CALLS"; }\n'
            'set_reboot_required() { echo "reboot $*" >> "$CALLS"; }\n'
            'package_transaction() { "$@"; }\n'
            'fail_kernel_bootstrap() { echo "fail $1" >> "$CALLS"; exit 1; }\n'
            'TARGET_KERNEL_VERSION="5.14.0-687*"\n'
            + function
            + 'update_within_target_series\necho "release ${TARGET_KERNEL_RELEASE}" >> "$CALLS"\n'
        )
        result = subprocess.run(
            [shutil.which('bash')],
            input=script,
            text=True,
            capture_output=True,
            env={
                'PATH': str(self.bin),
                'CALLS': str(self.calls),
                'KERNEL_BOOT_DIR': str(self.boot),
                'DRACUT_CONF_DIR': str(self.boot / 'dracut.conf.d'),
            },
        )
        assert result.returncode == exit_code, result.stdout + result.stderr
        return self.calls.read_text().splitlines()


OLD = '5.14.0-687.10.1.el9_8.x86_64'
FIXED = '5.14.0-687.64.1.el9_8.x86_64'


def test_rocky9_installs_and_boots_the_newest_release_of_its_series(context, tmp_path):
    calls = Host(tmp_path, running=OLD, newest=FIXED).run(
        render(context.config(), 'rocky9')
    )
    assert 'dnf versionlock delete kernel*' in calls
    assert (
        f'dnf install -y kernel-{FIXED} kernel-core-{FIXED} kernel-devel-{FIXED}'
        in calls
    )
    assert any(
        c.startswith('dnf repoquery') and 'kernel-core-5.14.0-687*' in c for c in calls
    )
    assert f'grubby --set-default {tmp_path}/boot/vmlinuz-{FIXED}' in calls
    assert f'reboot Kernel update to {FIXED}' in calls
    assert f'release {FIXED}' in calls


def test_al2023_takes_the_newest_kernel_of_its_family_from_the_latest_release(
    context, tmp_path
):
    old, fixed = '6.12.100-130.1.amzn2023.x86_64', '6.12.113-138.209.amzn2023.x86_64'
    calls = Host(tmp_path, running=old, newest=fixed).run(
        render(context.config(), 'amazonlinux2023')
    )
    assert any(
        c.startswith('dnf repoquery') and '--releasever=latest' in c for c in calls
    )
    assert (
        f'dnf install -y --releasever=latest kernel6.12-{fixed} kernel6.12-devel-{fixed}'
        in calls
    )
    assert f'reboot Kernel update to {fixed}' in calls


def test_a_kernel_with_no_initramfs_gets_one_built_before_it_boots(context, tmp_path):
    calls = Host(
        tmp_path, running=OLD, newest=FIXED, initramfs=False, dracut_ok=True
    ).run(render(context.config(), 'rocky9'))
    assert any(c.startswith('dracut -f') and FIXED in c for c in calls)
    assert any(c.startswith('grubby --set-default') for c in calls)
    assert any(c.startswith('reboot') for c in calls)


def test_a_kernel_that_cannot_get_an_initramfs_is_never_booted(context, tmp_path):
    calls = Host(tmp_path, running=OLD, newest=FIXED, initramfs=False).run(
        render(context.config(), 'amazonlinux2023')
    )
    assert not any(c.startswith('grubby --set-default') and FIXED in c for c in calls)
    # the package may have made the new kernel the default: a later reboot must not boot it
    assert any(c.startswith('grubby --set-default') and OLD in c for c in calls)
    assert not any(c.startswith('reboot') for c in calls)
    assert any('has no initramfs; keeping' in c for c in calls)


def test_a_default_that_cannot_be_restored_fails_the_bootstrap(context, tmp_path):
    host = Host(
        tmp_path,
        running=OLD,
        newest=FIXED,
        initramfs=False,
        grubby_ok=False,
        package_default=True,
    )
    calls = host.run(render(context.config(), 'rocky9'), exit_code=1)
    assert 'fail kernel-boot-mismatch' in calls
    assert not any(c.startswith('reboot') for c in calls)
    assert 'release ' not in ' '.join(calls)


def test_a_restore_that_leaves_the_new_kernel_selected_fails_the_bootstrap(
    context, tmp_path
):
    # grubby exits 0 and the default is still the kernel with no initramfs
    host = Host(
        tmp_path, running=OLD, newest=FIXED, initramfs=False, package_default=True
    )
    host.stub(
        'grubby',
        f'echo "grubby $*" >> {host.calls}\n'
        f'[ "$1" = --default-kernel ] && cat {host.default}\nexit 0',
    )
    calls = host.run(render(context.config(), 'rocky9'), exit_code=1)
    assert 'fail kernel-boot-mismatch' in calls
    assert any(c.startswith('grubby --default-kernel') for c in calls)
    assert not any(c.startswith('reboot') for c in calls)


def test_a_restored_default_is_read_back_and_bootstrap_continues(context, tmp_path):
    host = Host(
        tmp_path, running=OLD, newest=FIXED, initramfs=False, package_default=True
    )
    calls = host.run(render(context.config(), 'rocky9'))
    assert host.default.read_text().strip().endswith(f'vmlinuz-{OLD}')
    assert any(c.startswith('grubby --default-kernel') for c in calls)
    assert not any(c.startswith(('fail', 'reboot')) for c in calls)


def test_a_new_kernel_with_no_vmlinuz_does_not_stay_selected(context, tmp_path):
    host = Host(tmp_path, running=OLD, newest=FIXED, package_default=True)
    host.stub(
        'dnf',
        f'echo "dnf $*" >> {host.calls}\n'
        'case "$1" in\n'
        f'  repoquery) echo {FIXED} ;;\n'
        f'  install) echo x > {host.boot}/initramfs-{FIXED}.img ;;\n'
        'esac',
    )
    calls = host.run(render(context.config(), 'rocky9'))
    assert host.default.read_text().strip().endswith(f'vmlinuz-{OLD}')
    assert not any(c.startswith('reboot') for c in calls)


def test_the_newest_already_running_changes_nothing(context, tmp_path):
    calls = Host(tmp_path, running=FIXED, newest=FIXED).run(
        render(context.config(), 'rocky9')
    )
    assert not [c for c in calls if c.startswith(('dnf install', 'grubby', 'reboot'))]
    assert 'release ' in calls


def test_an_older_repository_kernel_is_never_a_downgrade(context, tmp_path):
    calls = Host(tmp_path, running=FIXED, newest=OLD).run(
        render(context.config(), 'rocky9')
    )
    assert not [c for c in calls if c.startswith(('dnf install', 'grubby', 'reboot'))]


def test_a_failed_install_keeps_the_running_kernel_and_does_not_reboot(
    context, tmp_path
):
    calls = Host(tmp_path, running=OLD, newest=FIXED, install_ok=False).run(
        render(context.config(), 'rocky9')
    )
    assert any(c.startswith('warning Unable to install kernel') for c in calls)
    assert not [c for c in calls if c.startswith(('grubby', 'reboot'))]
    assert 'release ' in calls


def eol_guard(rendered: str) -> str:
    start = rendered.index(
        'if [[ -n "${BAKE_STAGE:-}" ]]; then\n  TARGET_KERNEL_SERIES='
    )
    return rendered[start : rendered.index('TARGET_KERNEL_VERSION=', start)]


def run_eol_guard(rendered: str, abi: str, bake=True) -> subprocess.CompletedProcess:
    script = (
        'fail_kernel_bootstrap() { echo "fail $1"; exit 1; }\n'
        f'TARGET_KERNEL_ABI={abi}\n'
        + ('BAKE_STAGE=dcv\n' if bake else '')
        + eol_guard(rendered)
        + 'echo passed\n'
    )
    return subprocess.run(['bash'], input=script, text=True, capture_output=True)


@pytest.mark.parametrize('lustre', (False, True))
@pytest.mark.parametrize(
    'base_os,abi,refused',
    [
        ('ubuntu2404', '6.14.0-1018-aws', True),
        ('ubuntu2404', '6.17.0-1020-aws', True),
        ('ubuntu2404', '7.0.0-1017-aws', False),
        ('ubuntu2404', '6.8.0-1057-aws', False),
        ('ubuntu2204', '6.5.0-1024-aws', True),
        ('ubuntu2204', '6.8.0-1069-aws', False),
        ('ubuntu2204', '5.15.0-1109-aws', False),
    ],
)
def test_an_ubuntu_bake_refuses_an_end_of_life_kernel_series(
    context, base_os, abi, refused, lustre
):
    rendered = render(
        context.config(), base_os, lustre=lustre, ubuntu_desktop_bake=True
    )
    result = run_eol_guard(rendered, abi)
    if refused:
        assert result.stdout.strip() == 'fail kernel-series-end-of-life'
    else:
        assert result.stdout.strip() == 'passed'
    # only a bake: an unbaked host keeps the kernel its image boots
    assert run_eol_guard(rendered, abi, bake=False).stdout.strip() == 'passed'


def test_the_deny_list_comes_from_the_cluster_config(context):
    config = context.config()
    key = 'global-settings.kernel_hardening.eol_kernel_series.ubuntu2404'
    config.put(key, ['7.0'])
    try:
        rendered = render(config, 'ubuntu2404', ubuntu_desktop_bake=True)
    finally:
        config.put(key, None)
    assert (
        run_eol_guard(rendered, '7.0.0-1017-aws').stdout.strip()
        == 'fail kernel-series-end-of-life'
    )
    assert run_eol_guard(rendered, '6.14.0-1018-aws').stdout.strip() == 'passed'


def test_a_release_with_no_end_of_life_series_has_no_guard(context):
    assert 'kernel-series-end-of-life' not in render(
        context.config(), 'ubuntu2604', ubuntu_desktop_bake=True
    )
    assert 'kernel-series-end-of-life' not in render(context.config(), 'ubuntu2404')
