"""
EL images have a 1 GB /boot. A bake that adds the newest kernel holds two initramfs images of
over 260 MB, and a package that triggers a rebuild writes a third beside them, which ended a
rocky9 bake with no space left. Rescue images go before the new kernel arrives; the kernel
the image came with goes once the bake runs the new one.
"""

import re
import subprocess
from pathlib import Path

import pytest

from test_bootstrap_shell_syntax import BASH, IDEA_BOOTSTRAP_DIR
from test_image_bake_bootstrap import render

RUNNING = '5.14.0-687.15.1.el9_8.x86_64'
NEWEST = '5.14.0-687.65.1.el9_8.x86_64'
EL = ['rhel8', 'rhel9', 'rhel10', 'rocky8', 'rocky9', 'rocky10']


def _update_function(tmp_path, base_os):
    script = render('_templates/linux/set_kernel.jinja2', base_os)
    start = script.index('function update_within_target_series()')
    end = script.index('function set_kernel_version()', start)
    common = Path(IDEA_BOOTSTRAP_DIR, 'common/bootstrap_common.sh').read_text()
    rescue = re.search(r'function drop_rescue_images \(\) \{\n.*?\n\}\n', common, re.S)[
        0
    ]
    ready = re.search(
        r'function kernel_initramfs_ready \(\) \{\n.*?\n\}\n', common, re.S
    )[0]
    return (
        (rescue + ready + script[start:end])
        .replace('${KERNEL_BOOT_DIR:-/boot}', f'{tmp_path}/boot')
        .replace('${DRACUT_CONF_DIR:-/etc/dracut.conf.d}', f'{tmp_path}/dracut.conf.d')
        .replace('/boot/', f'{tmp_path}/boot/')
        .replace('/etc/dracut.conf.d', f'{tmp_path}/dracut.conf.d')
    )


def _run_update(tmp_path, base_os, newest):
    boot = tmp_path / 'boot'
    (boot / 'loader' / 'entries').mkdir(parents=True)
    for name in (
        f'vmlinuz-{RUNNING}',
        f'initramfs-{RUNNING}.img',
        'vmlinuz-0-rescue-abc',
        '.vmlinuz-0-rescue-abc.hmac',
        'initramfs-0-rescue-abc.img',
        'loader/entries/abc-0-rescue.conf',
        f'loader/entries/abc-{RUNNING}.conf',
        f'vmlinuz-{newest}',
    ):
        (boot / name).touch()
    calls = tmp_path / 'calls'
    stubs = f"""
uname() {{ echo {RUNNING}; }}
log_warning() {{ echo "warning $*" >> {calls}; }}
set_reboot_required() {{ echo "reboot $*" >> {calls}; }}
grubby() {{ echo "grubby $*" >> {calls}; }}
dnf() {{
  case "$*" in *repoquery*) echo {newest} ;; esac
}}
package_transaction() {{
  echo "rescue images at install: $(ls -A {boot} | grep -c rescue)" >> {calls}
  echo "install $*" >> {calls}
}}
TARGET_KERNEL_VERSION='5.14.0-687*'
"""
    result = subprocess.run(
        [
            BASH,
            '-c',
            stubs
            + _update_function(tmp_path, base_os)
            + '\nupdate_within_target_series\n',
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return boot, calls.read_text() if calls.exists() else ''


@pytest.mark.parametrize('base_os', EL)
def test_rescue_images_make_room_before_a_new_kernel_is_installed(tmp_path, base_os):
    boot, calls = _run_update(tmp_path, base_os, NEWEST)
    assert 'rescue images at install: 0' in calls
    assert f'kernel-core-{NEWEST}' in calls
    assert not [p for p in boot.rglob('*') if 'rescue' in p.name]
    # the kernels themselves stay: the running one is what this boot depends on
    assert (boot / f'initramfs-{RUNNING}.img').exists()
    assert (boot / f'loader/entries/abc-{RUNNING}.conf').exists()
    conf = tmp_path / 'dracut.conf.d' / '90-idea-no-rescue.conf'
    assert conf.read_text() == 'dracut_rescue_image="no"\n'


def test_a_host_already_on_the_newest_kernel_changes_nothing(tmp_path):
    boot, calls = _run_update(tmp_path, 'rocky9', RUNNING)
    assert 'install' not in calls
    assert (boot / 'initramfs-0-rescue-abc.img').exists()
    assert not (tmp_path / 'dracut.conf.d').exists()


def _run_removal(tmp_path, base_os, installed, status=0):
    script = render('_templates/linux/remove_superseded_kernels.jinja2', base_os)
    calls = tmp_path / 'calls'
    calls.write_text('')
    listing = tmp_path / 'installed'
    listing.write_text(''.join(line + '\n' for line in installed))
    stubs = f"""
rpm() {{ cat {listing}; }}
uname() {{ echo {NEWEST}; }}
df() {{ echo "df $*" >> {calls}; }}
log_warning() {{ echo "warning $*" >> {calls}; }}
package_transaction() {{ echo "$*" >> {calls}; return {status}; }}
"""
    result = subprocess.run(
        [BASH, '-euo', 'pipefail', '-c', stubs + script],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return calls.read_text()


@pytest.mark.parametrize('base_os', EL)
def test_the_superseded_kernel_goes_and_the_running_one_stays(tmp_path, base_os):
    calls = _run_removal(
        tmp_path,
        base_os,
        [
            f'kernel-{RUNNING}',
            f'kernel-core-{RUNNING}',
            f'kernel-devel-{RUNNING}',
            f'kernel-{NEWEST}',
            f'kernel-core-{NEWEST}',
            'package kernel-modules-core is not installed',
        ],
    )
    removal = next(line for line in calls.splitlines() if line.startswith('dnf remove'))
    assert '--noautoremove' in removal
    assert f'kernel-core-{RUNNING}' in removal and f'kernel-devel-{RUNNING}' in removal
    assert NEWEST not in removal and 'is not installed' not in removal
    assert 'df -h /boot' in calls


def test_a_single_kernel_removes_nothing_and_a_failed_removal_only_warns(tmp_path):
    assert 'dnf remove' not in _run_removal(tmp_path, 'rocky9', [f'kernel-{NEWEST}'])
    calls = _run_removal(tmp_path, 'rocky9', [f'kernel-{RUNNING}'], status=1)
    assert 'warning Unable to remove superseded kernels' in calls


def test_other_distributions_have_nothing_to_remove():
    for base_os in ('amazonlinux2023', 'ubuntu2404'):
        assert 'dnf remove' not in render(
            '_templates/linux/remove_superseded_kernels.jinja2', base_os
        )
