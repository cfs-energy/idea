"""
Every reboot passes one check of what the boot loader will actually boot, whoever selected it, so
a kernel with no initramfs never reboots a host into a panic.
"""

from pathlib import Path
import os
import re
import shutil
import subprocess

import pytest

from test_bootstrap_shell_syntax import IDEA_BOOTSTRAP_DIR
from test_image_bake_bootstrap import render

COMMON = Path(IDEA_BOOTSTRAP_DIR, 'common/bootstrap_common.sh')
RUNNING = '5.14.0-687.5.1.el9_8.x86_64'
BROKEN = '5.14.0-687.64.1.el9_8.x86_64'
UBUNTU_RUNNING = '6.8.0-1050-aws'
UBUNTU_BROKEN = '6.8.0-1063-aws'


class Host:
    """an RPM host, or an Ubuntu one with no grubby, whose next boot is what the stubs say"""

    def __init__(self, tmp_path, rpm=True, grubby_can_select=True, dracut_builds=False):
        self.tmp = tmp_path
        self.boot = tmp_path / 'boot'
        self.boot.mkdir()
        self.bin = tmp_path / 'bin'
        self.bin.mkdir()
        self.calls = tmp_path / 'calls'
        self.calls.write_text('')
        self.default = tmp_path / 'default'
        self.bootstrap = tmp_path / 'bootstrap'
        self.bootstrap.mkdir()
        self.rpm = rpm
        log = f'echo "$(basename "$0") $*" >> {self.calls}\n'
        self.stub('sleep', '')
        self.stub('curl', 'exit 1')
        self.stub('aws', log)
        self.stub('systemctl', 'echo SubState=exited\necho Job=\n')
        self.stub('reboot', log)
        if rpm:
            self.stub('uname', f'echo {RUNNING}')
            self.stub(
                'grubby',
                log + 'case "$1" in\n'
                f'  --default-kernel) cat {self.default} ;;\n'
                + (
                    f'  --set-default) echo "$2" > {self.default} ;;\n'
                    if grubby_can_select
                    else '  --set-default) exit 1 ;;\n'
                )
                + 'esac',
            )
            self.select(BROKEN)
            self.image(RUNNING)
            self.stub(
                'dracut',
                log + ('echo x > "$2"' if dracut_builds else 'exit 1'),
            )
        else:
            self.stub('uname', f'echo {UBUNTU_RUNNING}')
            self.write_grub(UBUNTU_BROKEN)
            self.defaults = tmp_path / 'grub-defaults'
            self.defaults.write_text('GRUB_DEFAULT=0\n')
            self.stub('grub-editenv', 'exit 0')
            self.stub(
                'update-grub',
                log
                + f"""sed -n 's/^GRUB_DEFAULT=.*Linux \\(.*\\)"$/\\1/p' {self.defaults} > {tmp_path}/chosen
{shutil.which('python3')} - <<PY
from pathlib import Path
chosen = Path('{tmp_path}/chosen').read_text().strip()
Path('{tmp_path}/grub.cfg').write_text({self._grub_template()!r}.replace('CHOSEN', chosen))
PY
""",
            )
            self.stub('update-initramfs', log + 'exit 1')
            self.image(UBUNTU_RUNNING)

    @staticmethod
    def _grub_template():
        return (
            'set default="0"\n'
            "menuentry 'Ubuntu' --id top {\n\tlinux\t/boot/vmlinuz-CHOSEN root=x\n}\n"
        )

    def write_grub(self, kernel):
        (self.tmp / 'grub.cfg').write_text(
            self._grub_template().replace('CHOSEN', kernel)
        )

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def select(self, kernel):
        self.default.write_text(f'{self.boot}/vmlinuz-{kernel}\n')

    def image(self, kernel):
        name = f'initramfs-{kernel}.img' if self.rpm else f'initrd.img-{kernel}'
        (self.boot / name).write_text('x')

    def run(self, body):
        common = COMMON.read_text().replace('timeout 30 /sbin/reboot', 'reboot')
        script = self.tmp / 'script.sh'
        script.write_text(
            common
            + f'\nexport AWS_REGION=us-east-2\nBOOTSTRAP_DIR="{self.bootstrap}"\n'
            + body
        )
        environment = dict(
            PATH=f'{self.bin}:{os.environ["PATH"]}',
            BOOTSTRAP_DIR=str(self.bootstrap),
            KERNEL_BOOT_DIR=str(self.boot),
            GRUB_DIR=str(self.tmp),
            DRACUT_CONF_DIR=str(self.tmp / 'dracut'),
            CALLS=str(self.calls),
        )
        if not self.rpm:
            environment['GRUB_DEFAULTS_FILE'] = str(self.defaults)
            # this machine may have grubby; an Ubuntu host does not
            environment['PATH'] = os.pathsep.join(
                folder
                for folder in environment['PATH'].split(os.pathsep)
                if not Path(folder, 'grubby').exists()
            )
        result = subprocess.run(
            ['bash', str(script)], env=environment, capture_output=True, text=True
        )
        return result.returncode, self.calls.read_text()


def rebooted(calls):
    return any(line.startswith('reboot') for line in calls.splitlines())


# the chokepoint


def test_a_healthy_host_does_not_notice(tmp_path):
    host = Host(tmp_path)
    host.select(RUNNING)
    code, calls = host.run('complete_bootstrap_reboot\n')
    assert rebooted(calls), calls
    assert 'dracut' not in calls and '--set-default' not in calls


def test_a_healthy_new_kernel_is_rebooted_into(tmp_path):
    host = Host(tmp_path)
    host.image(BROKEN)
    _, calls = host.run('complete_bootstrap_reboot\n')
    assert rebooted(calls)
    assert host.default.read_text().strip().endswith(BROKEN)


def test_a_missing_initramfs_is_built_and_the_kernel_kept(tmp_path):
    host = Host(tmp_path, dracut_builds=True)
    _, calls = host.run('complete_bootstrap_reboot\n')
    assert rebooted(calls)
    assert 'dracut -f' in calls
    assert host.default.read_text().strip().endswith(BROKEN)


def test_one_that_cannot_be_built_goes_back_to_the_running_kernel(tmp_path):
    host = Host(tmp_path)
    _, calls = host.run('complete_bootstrap_reboot\n')
    assert rebooted(calls)
    assert host.default.read_text().strip().endswith(RUNNING)
    assert 'grubby --default-kernel' in calls


def test_a_default_that_cannot_be_put_back_fails_with_no_reboot(tmp_path):
    host = Host(tmp_path, grubby_can_select=False)
    code, calls = host.run('complete_bootstrap_reboot\n')
    assert code == 1
    assert not rebooted(calls)
    assert (host.bootstrap / 'reboot_required.txt').read_text() == 'no'
    assert 'would not boot' in (host.bootstrap / 'bootstrap_failed.txt').read_text()


def test_a_running_kernel_with_no_initramfs_fails_with_no_reboot(tmp_path):
    host = Host(tmp_path)
    host.select(RUNNING)
    (host.boot / f'initramfs-{RUNNING}.img').unlink()
    code, calls = host.run('complete_bootstrap_reboot\n')
    assert code == 1 and not rebooted(calls)


def test_a_kernel_that_cannot_be_read_is_reported_and_stops_nothing(tmp_path):
    host = Host(tmp_path)
    host.stub('grubby', 'exit 1')
    code, calls = host.run('complete_bootstrap_reboot\n')
    assert rebooted(calls)


def test_the_end_of_a_bootstrap_with_no_reboot_is_checked_too(tmp_path):
    host = Host(tmp_path, grubby_can_select=False)
    code, calls = host.run('ensure_boot_kernel_bootable\necho reached\n')
    assert code == 1


# ubuntu: what GRUB will boot


def test_ubuntu_healthy_host_does_not_notice(tmp_path):
    host = Host(tmp_path, rpm=False)
    host.write_grub(UBUNTU_RUNNING)
    _, calls = host.run('complete_bootstrap_reboot\n')
    assert rebooted(calls), calls
    assert 'update-grub' not in calls and 'update-initramfs' not in calls


def test_ubuntu_goes_back_to_the_running_kernel_when_no_initrd_can_be_built(tmp_path):
    host = Host(tmp_path, rpm=False)
    _, calls = host.run('complete_bootstrap_reboot\n')
    assert 'update-initramfs -c -k ' + UBUNTU_BROKEN in calls
    assert 'update-grub' in calls and rebooted(calls)
    assert f'Linux {UBUNTU_RUNNING}' in host.defaults.read_text()


def test_ubuntu_fails_with_no_reboot_when_grub_does_not_take_the_change(tmp_path):
    host = Host(tmp_path, rpm=False)
    host.stub('update-grub', 'exit 0')
    code, calls = host.run('complete_bootstrap_reboot\n')
    assert code == 1 and not rebooted(calls)


# the three ways a broken kernel got selected, end to end


def _function(text, name):
    found = re.search(rf'function {name} ?\(\) \{{\n.*?\n\}}\n', text, re.S)
    assert found, name
    return found[0]


def test_a_transaction_that_failed_halfway_never_reboots_into_its_kernel(tmp_path):
    host = Host(tmp_path)
    host.select(RUNNING)
    host.stub(
        'dnf',
        f'case "$1" in repoquery) echo {BROKEN} ;; '
        f'install) echo {host.boot}/vmlinuz-{BROKEN} > {host.default}; exit 1 ;; esac',
    )
    rendered = render('_templates/linux/set_kernel.jinja2', base_os='rocky9')
    function = _function(rendered, 'update_within_target_series')
    code, calls = host.run(
        'TARGET_KERNEL_VERSION="5.14.0-687*"\n'
        'package_transaction() { "$@"; }\n'
        + function
        + 'update_within_target_series\ncomplete_bootstrap_reboot\n'
    )
    assert rebooted(calls)
    assert host.default.read_text().strip().endswith(RUNNING), (
        'not the half-installed one'
    )


def test_optional_lustre_cannot_turn_a_broken_default_into_a_reboot(tmp_path):
    host = Host(tmp_path, grubby_can_select=False)
    rendered = render(
        '_templates/linux/fsx_lustre_client.jinja2', base_os='rocky9', lustre=True
    )
    functions = _function(rendered, 'fail_lustre_client') + _function(
        rendered, 'lustre_kernel_unsupported'
    )
    code, calls = host.run(
        'LUSTRE_REQUIRED=no\n'
        + functions
        + 'lustre_kernel_unsupported "no client"\ncomplete_bootstrap_reboot\n'
    )
    assert code == 1
    assert not rebooted(calls)


def test_a_gpu_preparation_update_cannot_reboot_into_its_kernel(tmp_path):
    # dnf update selects a kernel with no initramfs, headers for the running one are there, and
    # the driver install asks for a reboot
    host = Host(tmp_path, grubby_can_select=False)
    host.select(RUNNING)
    host.stub('dnf', f'echo {host.boot}/vmlinuz-{BROKEN} > {host.default}')
    code, calls = host.run(
        'package_transaction() { "$@"; }\n'
        'package_transaction dnf update -y\n'
        'set_reboot_required "Installed NVIDIA Grid Driver"\n'
        'complete_bootstrap_reboot\n'
    )
    assert code == 1
    assert not rebooted(calls)


# nothing gets around it


REBOOT = re.compile(
    r'(?:^\s*|[;&|(]\s*|\bsudo\s+|\bexec\s+|\btimeout\s+\S+\s+|\bthen\s+|\bdo\s+)'
    r'(/usr)?(/s?bin/)?'
    r'(reboot|poweroff|halt|kexec|telinit\s+6|init\s+6|shutdown\s+-[rh]'
    r'|systemctl\s+(--\S+\s+)*(reboot|poweroff|halt|kexec))(\s|$|[;&|)])'
)


def linux_bootstrap_files():
    for path in Path(IDEA_BOOTSTRAP_DIR).rglob('*'):
        if (
            path.is_file()
            and path.suffix not in ('.ps1', '.pyc')
            and 'windows' not in str(path.relative_to(IDEA_BOOTSTRAP_DIR))
            and not path.name.endswith('.ps1.jinja2')
        ):
            yield path


def test_the_only_reboot_in_any_linux_bootstrap_file_is_behind_the_check():
    found = []
    for path in linux_bootstrap_files():
        for number, line in enumerate(path.read_text(errors='ignore').splitlines()):
            text = line.split('#', 1)[0]
            # what a script says about a reboot is not one
            if re.match(
                r'\s*(log_\w+|echo|set_reboot_required|check_reboot_loop)\b', text
            ):
                continue
            text = re.sub(r'"[^"]*"', '""', text)
            if REBOOT.search(text) or 'reboot_instances' in text:
                found.append((path.name, line.strip()))
    assert found == [
        (
            'bootstrap_common.sh',
            'timeout 30 /sbin/reboot || fail_bootstrap reboot-failed "Bootstrap reboot request failed"',
        )
    ]
    body = COMMON.read_text()
    worker = body[body.index('function complete_bootstrap_reboot') :]
    assert worker.index('ensure_boot_kernel_bootable') < worker.index('/sbin/reboot')


def test_the_check_would_see_a_direct_reboot():
    for line in (
        'reboot',
        'sudo /sbin/reboot now',
        'shutdown -r now',
        'systemctl reboot',
    ):
        assert REBOOT.search(line), line
    assert not REBOOT.search('reboot_after_cloud_init')
    assert not REBOOT.search('set_reboot_required "x"')


@pytest.mark.parametrize(
    'template',
    [
        'compute-node/compute_node_post_reboot.sh.jinja2',
        'compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2',
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
        'openldap-server/setup.sh.jinja2',
        'virtual-desktop-host-linux/configure_dcv_host_post_reboot.sh.jinja2',
    ],
)
def test_every_bootstrap_that_ends_without_a_reboot_checks_first(template):
    assert (
        'ensure_boot_kernel_bootable' in Path(IDEA_BOOTSTRAP_DIR, template).read_text()
    )


def test_every_bootstrap_that_can_end_or_reboot_is_listed_above():
    # a new top level script that stops at the end of its own file is a new place to check
    ends = set()
    for path in linux_bootstrap_files():
        text = path.read_text(errors='ignore')
        if 'reboot_required.txt)' in text and 'reboot_after_cloud_init' in text:
            ends.add(path.relative_to(IDEA_BOOTSTRAP_DIR).as_posix())
    assert ends == {
        'compute-node/compute_node.sh.jinja2',
        'compute-node/compute_node_post_reboot.sh.jinja2',
        'compute-node-ami-builder/compute_node_ami_builder.sh.jinja2',
        'compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2',
        'dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2',
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
        'openldap-server/setup.sh.jinja2',
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
        'virtual-desktop-host-linux/setup.sh.jinja2',
        '_templates/linux/mount_shared_storage.jinja2',
    }
