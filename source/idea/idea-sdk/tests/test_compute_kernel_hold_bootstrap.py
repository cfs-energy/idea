"""
Compute on a cluster that uses FSx for Lustre keeps the kernel series its image boots. The
package list names `kernel`, which installs the newest kernel of any series, and the FSx
repository has no client for a series Red Hat has only just shipped: RHEL compute bakes moved
to 5.14.0-741 and 6.12.0-264 and came up with no Lustre module, one of them hanging for the
hour a builder is given to report ready.

The scripts are the rendered templates, run against a repository, a boot loader and a set of
kernel modules kept in files.
"""

import subprocess

import pytest

from ideadatamodel.scheduler.scheduler_model import SocaFSxLustreConfig
from ideasdk.aws.image_builds import classify_bake_failure
from test_bootstrap_shell_syntax import IDEA_BOOTSTRAP_DIR, job
from test_fsx_lustre_client_bootstrap import run_install
from test_image_bake_bootstrap import render

BAKE = 'compute-node-ami-builder/compute_node_ami_builder.sh.jinja2'
POST_REBOOT = 'compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2'
NODE = 'compute-node/compute_node.sh.jinja2'
EL = ('rhel8', 'rocky8', 'rhel9', 'rocky9', 'rhel10', 'rocky10')

# the image boots BASE; Red Hat's repository also has a newer kernel of that series and the
# first kernel of the next one, which FSx has no client for
BASE = '5.14.0-687.59.1.el9_8.x86_64'
SERIES_NEWEST = '5.14.0-687.66.1.el9_8.x86_64'
NEXT_SERIES = '5.14.0-741.14.1.el9_9.x86_64'

STUBS = r"""
sleep() { :; }
instance_id() { echo i-0; }
aws() { echo "aws $*" >> "$CALLS"; }
crontab() { :; }
systemctl() { return 1; }
dracut() { :; }
reboot_after_cloud_init() { echo "reboot into $(boot_loader_default_kernel)" >> "$CALLS"; }
uname() { if [[ "$1" == -r ]]; then echo "$RUNNING_KERNEL"; else echo x86_64; fi; }
# the client's module exists for the kernels its package was linked into
modinfo() { [[ " $LUSTRE_KERNELS " == *" $2 "* ]]; }
grubby() {
  case "$1" in
    --default-kernel) cat "$STATE/default" ;;
    --set-default) echo "$2" > "$STATE/default" ;;
  esac
}
rpm() {
  [[ "$1" == -qa ]] || return 0
  local kernel
  for kernel in "$STATE"/boot/vmlinuz-*; do echo "kernel-${kernel##*/vmlinuz-}"; done
}
install_kernel() {
  # an installed package is left alone, and so is the boot loader
  [[ ! -f "$STATE/boot/vmlinuz-$1" ]] || return 0
  touch "$STATE/boot/vmlinuz-$1"
  echo x > "$STATE/boot/initramfs-$1.img"
  # as the kernel package does: the newest installed kernel becomes the default
  echo "$STATE/boot/vmlinuz-$(cd "$STATE/boot" && ls vmlinuz-* | sed 's/^vmlinuz-//' | sort -V | tail -1)" > "$STATE/default"
}
dnf() {
  while [[ "$1" == -* ]]; do shift; done
  local command="$1" spec kernel newest=""
  shift
  case "$command" in
    versionlock)
      [[ -z "$NO_VERSIONLOCK" ]] || return 1
      if [[ "$1" == delete ]]; then : > "$STATE/lock"; return 0; fi
      echo "${1#kernel-}" > "$STATE/lock"
      ;;
    repoquery)
      for kernel in $REPO_KERNELS; do
        [[ "kernel-core-$kernel" == ${!#} ]] && newest="$kernel"
      done
      [[ -z "$newest" ]] || echo "$newest"
      ;;
    install)
      for spec in "$@"; do
        case "$spec" in
          kernel)
            # the package list's bare name: the newest kernel the lock allows
            newest=$(cat "$STATE/lock" 2>/dev/null)
            [[ -n "$newest" ]] || newest="${REPO_KERNELS##* }"
            echo "install kernel $newest" >> "$CALLS"
            install_kernel "$newest"
            ;;
          kernel-[0-9]*)
            echo "install kernel ${spec#kernel-}" >> "$CALLS"
            install_kernel "${spec#kernel-}"
            ;;
        esac
      done
      ;;
  esac
}
yum() { dnf "$@"; }
"""


def package_stage(template, base_os, lustre=True, drivers=None, job_params=None):
    """the kernel hold and the package list as the script runs them, then its reboot decision"""
    if drivers is None:
        drivers = ('fsx_lustre',) if lustre else ()
    variables = {}
    if template == NODE:
        variables = {'job': job(**(job_params or {})), 'job_directory': '/data/job'}
    text = render(
        template, base_os, lustre=lustre, drivers=drivers, variables=variables
    )
    packages = text[: text.index('# End: System Packages Install')]
    start = packages.find('# Begin: Kernel Version Management')
    if start == -1:
        start = packages.index('# Begin: System Packages Install')
    tail = ''
    if template == BAKE:
        tail = text[text.index('touch /var/lib/idea/bake-packages.ok') :]
    return text, packages[start:] + '\n' + tail


def run_stage(tmp_path, stage, running=BASE, lustre_kernels=None, **environment):
    state = tmp_path / 'state'
    (state / 'boot').mkdir(parents=True)
    (state / 'boot' / f'vmlinuz-{running}').write_text('')
    (state / 'boot' / f'initramfs-{running}.img').write_text('x')
    (state / 'default').write_text(f'{state}/boot/vmlinuz-{running}\n')
    (state / 'reboot_required.txt').write_text('no')
    for path in ('/var/lib/idea/', '/root/bootstrap/'):
        stage = stage.replace(path, f'{state}/')
    stage = stage.replace('/boot/', f'{state}/boot/')
    (state / 'image_checks.sh').write_text(
        f'echo "image_checks $(head -1 {state}/bake-failed)" >> "$CALLS"\nexit 1\n'
    )
    calls = tmp_path / 'calls'
    calls.write_text('')
    script = tmp_path / 'run.sh'
    script.write_text(
        f'source "{IDEA_BOOTSTRAP_DIR}/common/bootstrap_common.sh"\n'
        f'BOOTSTRAP_DIR="{state}"\nSCRIPT_DIR="{state}"\n'
        'IDEA_COMPUTE_NODE_AMI_BUILDER_LOGS_DIR="$BOOTSTRAP_DIR"\n' + STUBS + stage
    )
    if lustre_kernels is None:
        # weak-modules links the 9.8 client into every 9.8 kernel, and into no 9.9 one
        lustre_kernels = f'{BASE} {SERIES_NEWEST}'
    result = subprocess.run(
        ['bash', str(script)],
        env={
            'PATH': '/usr/bin:/bin',
            'CALLS': str(calls),
            'STATE': str(state),
            'RUNNING_KERNEL': running,
            'REPO_KERNELS': f'{BASE} {SERIES_NEWEST} {NEXT_SERIES}',
            'LUSTRE_KERNELS': lustre_kernels,
            'NO_VERSIONLOCK': '',
            'KERNEL_BOOT_DIR': str(state / 'boot'),
            'DRACUT_CONF_DIR': str(state / 'dracut.conf.d'),
            **environment,
        },
        capture_output=True,
        text=True,
    )
    lock = state / 'lock'
    return SimpleResult(
        code=result.returncode,
        out=result.stdout + result.stderr,
        calls=calls.read_text().splitlines(),
        default=(state / 'default').read_text().strip().rsplit('vmlinuz-', 1)[1],
        installed=sorted(
            p.name[len('vmlinuz-') :] for p in (state / 'boot').glob('vmlinuz-*')
        ),
        lock=lock.read_text().strip() if lock.exists() else '',
        failed=(state / 'bake-failed').read_text()
        if (state / 'bake-failed').exists()
        else '',
    )


class SimpleResult:
    def __init__(self, **values):
        self.__dict__.update(values)


def bake_row_error(failed_line: str) -> str:
    """the row's error as the scheduler writes it from the failing in-bake check"""
    return f'in-bake check bootstrap failed on builder i-0: a command failed: {failed_line}'


@pytest.mark.parametrize('template', [BAKE, NODE])
def test_the_newest_kernel_with_no_lustre_client_is_not_installed(tmp_path, template):
    _, stage = package_stage(template, 'rhel9')
    host = run_stage(tmp_path, stage)
    assert host.code == 0, host.out
    # the newest kernel of the series the client was installed for, and never the next series
    assert host.default == SERIES_NEWEST
    assert host.installed == [BASE, SERIES_NEWEST]
    assert host.lock == SERIES_NEWEST
    assert f'install kernel {NEXT_SERIES}' not in host.calls
    if template == BAKE:
        assert host.calls[-1] == f'reboot into {SERIES_NEWEST}'
        assert host.failed == ''


def test_a_series_kernel_the_client_has_no_module_for_is_not_booted(tmp_path):
    # the client was linked into the running kernel only: the newer one changed its symbols
    _, stage = package_stage(BAKE, 'rhel9')
    host = run_stage(tmp_path, stage, lustre_kernels=BASE)
    assert host.code == 0, host.out
    assert host.default == BASE and host.lock == BASE
    assert f'Kernel {SERIES_NEWEST} has no FSx for Lustre module; keeping' in host.out
    assert host.calls[-1] == f'reboot into {BASE}'
    assert host.failed == ''


def test_a_bake_whose_hold_did_not_hold_stops_before_the_reboot(tmp_path):
    # no versionlock: the package list moves the kernel as it did before the hold
    _, stage = package_stage(BAKE, 'rhel9')
    host = run_stage(tmp_path, stage, NO_VERSIONLOCK='1')
    assert host.code == 1
    assert host.default == NEXT_SERIES
    assert not [c for c in host.calls if c.startswith('reboot')]
    assert host.calls[-1].startswith(
        f'image_checks lustre: Kernel {NEXT_SERIES} has no FSx for Lustre client module'
    )
    assert classify_bake_failure(bake_row_error(host.failed.strip())) == 'lustre_kernel'


def test_an_image_ahead_of_the_fsx_repository_fails_with_the_lustre_reason(tmp_path):
    # the image already boots the series FSx has no client for: nothing older to keep
    code, out, calls, _ = run_install(
        tmp_path, 'compute-node-ami-builder/setup.sh.jinja2', 'rhel9', NEXT_SERIES
    )
    assert code == 1 and 'reboot' not in calls
    failed = (tmp_path / 'bake-failed').read_text().strip()
    assert 'FSx for Lustre' in failed and NEXT_SERIES in failed
    assert '(reason code: lustre-module-missing)' in failed
    assert calls.splitlines()[-1].startswith('image_checks lustre: ')
    assert classify_bake_failure(bake_row_error(failed)) == 'lustre_kernel'


def post_reboot_check(lustre=True, drivers=('fsx_lustre',), base_os='rhel9'):
    """the stage's Lustre check as rendered, or nothing when it has none"""
    text = render(POST_REBOOT, base_os, lustre=lustre, drivers=drivers)
    marker = 'LUSTRE_KERNEL="$(uname -r)"'
    if marker not in text:
        return ''
    return text[text.index(marker) : text.index('\nfi\n', text.index(marker)) + 4]


def test_a_bake_that_booted_a_kernel_with_no_lustre_module_says_so(tmp_path):
    check = post_reboot_check()
    assert check, 'the post-reboot stage does not look at the Lustre module'
    host = run_stage(
        tmp_path,
        check + 'echo next-stage >> "$CALLS"\n',
        running=NEXT_SERIES,
    )
    assert host.code == 1
    assert 'next-stage' not in host.calls
    assert host.calls[-1].startswith(
        f'image_checks lustre: Kernel {NEXT_SERIES} has no FSx for Lustre client module'
    )
    assert classify_bake_failure(bake_row_error(host.failed.strip())) == 'lustre_kernel'


def test_a_bake_on_a_kernel_with_the_module_goes_on_after_the_reboot(tmp_path):
    host = run_stage(
        tmp_path,
        post_reboot_check() + 'echo next-stage >> "$CALLS"\n',
        running=SERIES_NEWEST,
    )
    assert host.code == 0 and host.calls == ['next-stage'] and host.failed == ''


def test_the_check_after_the_reboot_runs_before_the_gpu_stage_on_every_pass():
    text = render(POST_REBOOT, 'rhel9', drivers=('fsx_lustre',))
    assert text.index('LUSTRE_KERNEL="$(uname -r)"') < text.index(
        '# a reboot may be required if GPU Drivers are installed.'
    )
    # shared storage alone is enough: the pipeline bakes the driver only with it
    assert 'LUSTRE_KERNEL="$(uname -r)"' in render(POST_REBOOT, 'ubuntu2404')
    # before the reboot only where the package list can move the kernel
    before = 'LUSTRE_KERNEL="$(boot_loader_default_kernel)"'
    assert before in render(BAKE, 'rocky10', drivers=('fsx_lustre',))
    assert before not in render(BAKE, 'ubuntu2404', drivers=('fsx_lustre',))
    assert before not in render(BAKE, 'amazonlinux2023', drivers=('fsx_lustre',))


@pytest.mark.parametrize('template', [BAKE, NODE])
@pytest.mark.parametrize('base_os', EL)
def test_a_cluster_without_lustre_is_left_as_it_was(tmp_path, template, base_os):
    text, stage = package_stage(template, base_os, lustre=False)
    for added in (
        'Kernel Version Management',
        'versionlock',
        'kernel_has_lustre_module',
    ):
        assert added not in text
    assert 'LUSTRE_KERNEL' not in render(POST_REBOOT, base_os, lustre=False)
    if base_os != 'rhel9':
        return
    # the package list still takes the newest kernel, of any series
    host = run_stage(tmp_path, stage, lustre_kernels='')
    assert host.code == 0, host.out
    assert host.default == NEXT_SERIES and host.lock == ''
    assert host.failed == ''


@pytest.mark.parametrize('template', [BAKE, NODE])
@pytest.mark.parametrize('base_os', EL)
def test_every_el_release_keeps_the_series_its_image_boots(template, base_os):
    text, _ = package_stage(template, base_os)
    assert 'TARGET_KERNEL_VERSION="$(uname -r | cut -d. -f1-3)*"' in text
    # not the series desktops are pinned to: the client is installed for the kernel that runs
    assert 'TARGET_KERNEL_VERSION="5.14.0-687*"' not in text
    assert 'TARGET_KERNEL_VERSION="4.18.0-553*"' not in text
    assert text.index('\nset_kernel_version\n') < text.index(
        '# Begin: System Packages Install'
    )


@pytest.mark.parametrize(
    'lustre, drivers, held',
    [(True, (), True), (False, ('fsx_lustre',), True), (False, (), False)],
)
def test_the_hold_follows_what_the_cluster_and_the_bake_use(lustre, drivers, held):
    text, _ = package_stage(BAKE, 'rocky9', lustre=lustre, drivers=drivers)
    assert ('Kernel Version Management' in text) == held


def test_a_job_with_lustre_scratch_holds_an_unbaked_node():
    text, _ = package_stage(
        NODE,
        'rocky9',
        lustre=False,
        job_params={'fsx_lustre': SocaFSxLustreConfig(enabled=True)},
    )
    assert 'Kernel Version Management' in text


@pytest.mark.parametrize('template', [BAKE, NODE])
@pytest.mark.parametrize(
    'base_os', ['amazonlinux2023', 'ubuntu2204', 'ubuntu2404', 'ubuntu2604']
)
def test_hosts_whose_package_list_cannot_move_the_kernel_get_no_hold(template, base_os):
    # AL2023 ships the module in every kernel package; the Ubuntu lists name no kernel
    text, _ = package_stage(template, base_os)
    assert 'Kernel Version Management' not in text


def test_a_failed_hold_in_a_bake_reports_through_the_in_bake_checks():
    text, _ = package_stage(BAKE, 'rhel9')
    body = text.split('function fail_kernel_bootstrap() {', 1)[1].split('\n}', 1)[0]
    assert '/var/lib/idea/bake-failed' in body and 'image_checks.sh' in body
    assert 'idea:BootstrapStatus' not in body
    # a node that is booting tags itself, as a desktop does
    node, _ = package_stage(NODE, 'rhel9')
    body = node.split('function fail_kernel_bootstrap() {', 1)[1].split('\n}', 1)[0]
    assert 'idea:BootstrapStatus' in body and 'bake-failed' not in body
