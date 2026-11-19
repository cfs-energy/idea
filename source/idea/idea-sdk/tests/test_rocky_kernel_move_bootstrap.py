"""
A base image can boot a kernel series newer than any the FSx for Lustre client has a build for:
Rocky 10.3 (6.12.0-264) shipped on 2026-11-13 and FSx has no el/10.3. Every Rocky 10 bake on a
cluster with Lustre then stopped with lustre-module-missing. A bake moves such a host to the
newest kernel of the newest series the client supports, before the client install, and reboots
into it. Rocky's repositories carry only the current minor release's kernels, so the kernel
comes from the Rocky vault of the minor release that series shipped in.

The scripts are the rendered setup stages, run over two boots against a repository, a vault, a
boot loader and a set of kernel modules kept in files.
"""

import subprocess

import pytest

from ideasdk.aws.image_builds import classify_bake_failure
from test_bootstrap_shell_syntax import IDEA_BOOTSTRAP_DIR, job
from test_compute_kernel_hold_bootstrap import (
    BAKE,
    STUBS,
    bake_row_error,
    package_stage,
)
from test_fsx_lustre_client_bootstrap import lustre_config
from test_image_bake_bootstrap import render

COMPUTE_SETUP = 'compute-node-ami-builder/setup.sh.jinja2'
DESKTOP_SETUP = 'dcv-host-ami-builder/setup.sh.jinja2'
NODE = 'compute-node/compute_node.sh.jinja2'

# base_os: (kernel the image boots, what the vendor repository has, what the vault has, the
# kernel the newest FSx client build is for)
HOSTS = {
    'rocky10': (
        '6.12.0-264.16.1.el10_3.x86_64',
        '6.12.0-264.16.1.el10_3.x86_64',
        '6.12.0-211.26.1.el10_2.x86_64 6.12.0-211.74.1.el10_2.x86_64',
        '6.12.0-211.74.1.el10_2.x86_64',
    ),
    # Rocky 9 today boots 741, which FSx supports; the next minor release is the same case
    'rocky9': (
        '5.14.0-800.1.1.el9_10.x86_64',
        '5.14.0-800.1.1.el9_10.x86_64',
        '5.14.0-741.22.1.el9_9.x86_64',
        '5.14.0-741.22.1.el9_9.x86_64',
    ),
}

# the vault is visible once its .repo file exists; a glob installs the newest kernel it matches;
# the client's kmod installs only beside the kernel it was built for, as dnf resolves its symbols
MOVE_STUBS = r"""
eval "$(declare -f dnf | sed '1s/^dnf/repo_dnf/')"
dnf() {
  local REPO_KERNELS="$REPO_KERNELS" arg spec kernel newest args=()
  [[ ! -f "$STATE/idea-rocky-kernel-vault.repo" ]] || REPO_KERNELS="$VAULT_KERNELS $REPO_KERNELS"
  for arg in "$@"; do
    case "$arg" in
      kernel-[0-9]*\*)
        newest=""
        for kernel in $REPO_KERNELS; do [[ "kernel-$kernel" == $arg ]] && newest="$kernel"; done
        [[ -n "$newest" ]] || { echo "No match for argument: $arg"; return 1; }
        args+=("kernel-$newest")
        ;;
      kmod-lustre-client)
        echo "install kmod-lustre-client" >> "$CALLS"
        [[ -f "$STATE/boot/vmlinuz-$CLIENT_KERNEL" ]] || { echo "nothing provides kernel(ib_device_put)"; return 1; }
        echo "$CLIENT_KERNEL" >> "$STATE/lustre"
        args+=("$arg")
        ;;
      *) args+=("$arg") ;;
    esac
  done
  repo_dnf "${args[@]}"
}
modinfo() { grep -qx -- "$2" "$STATE/lustre" 2>/dev/null; }
rpm() {
  case "$*" in
    *lustre-client*) [[ ! -s "$STATE/lustre" ]] || echo lustre-client ;;
    -qa*)
      local kernel
      for kernel in "$STATE"/boot/vmlinuz-*; do echo "kernel-${kernel##*/vmlinuz-}"; done
      ;;
  esac
}
curl() { [[ "$1" != *.repo ]] || printf 'baseurl=%s/$basearch/\n' "${1%/fsx-lustre-client.repo}" > "$3"; }
sudo() { "$@"; }
"""


def setup_stage(template, base_os, lustre=True):
    """the setup stage from its kernel step through the Lustre client install and reboot decision"""
    text = render(
        template,
        base_os,
        lustre=lustre,
        drivers=('fsx_lustre',),
        config=lustre_config(),
        variables={'job': job(), 'job_directory': '/data/job'},
    )
    end = text.index('IDEA_SHARED_MOUNT_DIRS=(')
    start = text.find('# Begin: Kernel Version Management', 0, end)
    if start == -1:
        start = text.index('# Begin: Mount Shared Storage')
    return text, text[start:end]


def boot(tmp_path, stage, running, host, vault=None):
    """one pass of the stage on `host` running `running`; the state carries over to the next"""
    image, repo, vault_kernels, client = host
    state = tmp_path / 'state'
    if not state.exists():
        (state / 'boot').mkdir(parents=True)
        (state / 'boot' / f'vmlinuz-{image}').write_text('')
        (state / 'boot' / f'initramfs-{image}.img').write_text('x')
        (state / 'default').write_text(f'{state}/boot/vmlinuz-{image}\n')
    (state / 'reboot_required.txt').write_text('no')
    for path in (
        '/var/lib/idea/',
        '/root/bootstrap/',
        '/etc/yum.repos.d/',
        '/etc/modprobe.d/',
    ):
        stage = stage.replace(path, f'{state}/')
    stage = stage.replace('/boot/', f'{state}/boot/').replace(
        'find /boot ', f'find {state}/boot '
    )
    (state / 'image_checks.sh').write_text(
        f'echo "image_checks $(head -1 {state}/bake-failed)" >> "$CALLS"\nexit 1\n'
    )
    calls = tmp_path / 'calls'
    calls.write_text('')
    script = tmp_path / 'run.sh'
    script.write_text(
        f'source "{IDEA_BOOTSTRAP_DIR}/common/bootstrap_common.sh"\n'
        f'BOOTSTRAP_DIR="{state}"\nSCRIPT_DIR="{state}"\n'
        + STUBS
        + MOVE_STUBS
        + stage
        + '\necho fell-through >> "$CALLS"\n'
    )
    result = subprocess.run(
        ['bash', str(script)],
        env={
            'PATH': '/usr/bin:/bin',
            'CALLS': str(calls),
            'STATE': str(state),
            'RUNNING_KERNEL': running,
            'REPO_KERNELS': repo,
            'VAULT_KERNELS': vault_kernels if vault is None else vault,
            'CLIENT_KERNEL': client,
            'LUSTRE_KERNELS': '',
            'NO_VERSIONLOCK': '',
            'KERNEL_BOOT_DIR': str(state / 'boot'),
            'DRACUT_CONF_DIR': str(state / 'dracut.conf.d'),
        },
        capture_output=True,
        text=True,
    )
    vault_repo = state / 'idea-rocky-kernel-vault.repo'
    failed = state / 'bake-failed'
    return {
        'code': result.returncode,
        'out': result.stdout + result.stderr,
        'calls': calls.read_text().splitlines(),
        'default': (state / 'default').read_text().strip().rsplit('vmlinuz-', 1)[1],
        'vault': vault_repo.read_text() if vault_repo.exists() else '',
        'failed': failed.read_text().strip() if failed.exists() else '',
        'reboot': (state / 'reboot_required.txt').read_text(),
        'lock': (state / 'lock').read_text().strip()
        if (state / 'lock').exists()
        else '',
    }


@pytest.mark.parametrize('template', [COMPUTE_SETUP, DESKTOP_SETUP])
@pytest.mark.parametrize('base_os', ['rocky10', 'rocky9'])
def test_an_image_ahead_of_the_lustre_client_moves_to_a_supported_kernel_first(
    tmp_path, template, base_os
):
    image, _, _, supported = HOSTS[base_os]
    _, stage = setup_stage(template, base_os)

    first = boot(tmp_path, stage, image, HOSTS[base_os])
    assert first['code'] == 0, first['out']
    # the supported kernel from the vault of its minor release, made the default, then a reboot,
    # and no client install on the kernel that has none
    minor = '10.2' if base_os == 'rocky10' else '9.9'
    assert (
        f'baseurl=https://dl.rockylinux.org/vault/rocky/{minor}/BaseOS/$basearch/os/'
        in first['vault']
    )
    assert f'/vault/rocky/{minor}/AppStream/' in first['vault']
    assert 'includepkgs=kernel*' in first['vault'] and 'gpgcheck=1' in first['vault']
    # an unreachable vault never fails an unrelated package operation on the host
    assert first['vault'].count('skip_if_unavailable=1') == 2
    assert f'install kernel {supported}' in first['calls']
    assert first['default'] == supported
    assert first['calls'][-1] == f'reboot into {supported}'
    assert 'install kmod-lustre-client' not in first['calls']
    assert first['failed'] == ''

    # after the reboot: the running kernel is the supported one, and the client goes on it
    second = boot(tmp_path, stage, supported, HOSTS[base_os])
    assert second['code'] == 0, second['out']
    assert second['calls'] == ['install kmod-lustre-client', f'reboot into {supported}']
    assert second['failed'] == ''

    # the stage's own reboot for the client: nothing left to do
    third = boot(tmp_path, stage, supported, HOSTS[base_os])
    assert third['code'] == 0, third['out']
    assert third['calls'] == ['fell-through'] and third['failed'] == ''
    if template != COMPUTE_SETUP:
        return

    # the package stage holds the kernel the client is on, and its reboot keeps it
    _, packages = package_stage(BAKE, base_os)
    held = boot(tmp_path, packages, supported, HOSTS[base_os])
    assert held['code'] == 0, held['out']
    assert held['lock'] == supported and held['default'] == supported
    assert held['calls'][-1] == f'reboot into {supported}' and held['failed'] == ''


def test_a_host_that_did_not_boot_the_supported_kernel_stops_before_the_client(
    tmp_path,
):
    image, _, _, _ = HOSTS['rocky10']
    _, stage = setup_stage(COMPUTE_SETUP, 'rocky10')
    assert boot(tmp_path, stage, image, HOSTS['rocky10'])['code'] == 0
    # the boot loader ignored the default: the same kernel again
    again = boot(tmp_path, stage, image, HOSTS['rocky10'])
    assert again['code'] == 1
    assert 'install kmod-lustre-client' not in again['calls']
    assert again['failed'].startswith('kernel-boot-mismatch: ')


def test_a_supported_kernel_is_left_as_it_is(tmp_path):
    # Rocky 9.9 boots 741.22.1 and FSx has el/9.9: no vault and no kernel change
    running = '5.14.0-741.22.1.el9_9.x86_64'
    _, stage = setup_stage(COMPUTE_SETUP, 'rocky9')
    host = boot(tmp_path, stage, running, (running, running, '', running))
    assert host['code'] == 0, host['out']
    assert host['vault'] == ''
    # the client, then the stage's own reboot for it: no kernel installed, no kernel reboot
    assert host['calls'] == ['install kmod-lustre-client', f'reboot into {running}']


def test_no_supported_kernel_to_move_to_stops_with_the_lustre_reason(tmp_path):
    # also what an unreachable vault looks like: skip_if_unavailable leaves it with no kernels
    image, _, _, _ = HOSTS['rocky10']
    _, stage = setup_stage(COMPUTE_SETUP, 'rocky10')
    host = boot(tmp_path, stage, image, HOSTS['rocky10'], vault='')
    assert host['code'] == 1
    assert not [c for c in host['calls'] if c.startswith('reboot')]
    assert 'install kmod-lustre-client' not in host['calls']
    failed = host['failed']
    assert failed.startswith(
        f'lustre-module-missing: Kernel {image} is newer than any FSx'
    )
    assert 'from the Rocky 10.2 vault' in failed and 'To fix:' in failed
    assert classify_bake_failure(bake_row_error(failed)) == 'lustre_kernel'
    # the row shows the last 400 characters of the check, with its prefix
    assert len(f'a command failed: {failed}') <= 400


@pytest.mark.parametrize('base_os', ['rocky9', 'rocky10'])
def test_the_client_failure_says_what_to_do(tmp_path, base_os):
    # no move rendered (no fsx storage provider): the client step meets the newer kernel itself
    image, _, _, _ = HOSTS[base_os]
    text = render(
        'compute-node-ami-builder/compute_node_ami_builder.sh.jinja2',
        base_os,
        lustre=False,
        drivers=('fsx_lustre',),
    )
    client = text[
        text.index('# Begin: FSx Lustre Client') : text.index(
            '# End: FSx Lustre Client'
        )
    ]
    host = boot(tmp_path, client, image, HOSTS[base_os])
    assert host['code'] == 1
    failed = host['failed']
    assert f'No FSx for Lustre client build for kernel {image}' in failed
    assert 'Use a base image whose kernel FSx for Lustre has a client for' in failed
    assert failed.endswith('(reason code: lustre-module-missing).')
    assert len(f'a command failed: {failed}') <= 400


def test_the_move_is_only_where_the_host_has_not_installed_the_client_yet():
    for template in (COMPUTE_SETUP, DESKTOP_SETUP):
        text, _ = setup_stage(template, 'rocky10')
        assert text.index('FSX_NEWEST_SERIES="6.12.0-211"') < text.index(
            '# Begin: FSx Lustre Client'
        )
        assert 'KERNEL_SERIES_MOVE:-' in text
    # RHEL keeps every kernel in its repositories: the move, no vault
    rhel, _ = setup_stage(COMPUTE_SETUP, 'rhel10')
    assert 'FSX_NEWEST_SERIES' in rhel and 'idea-rocky-kernel-vault' not in rhel
    # a cluster without Lustre, and an unbaked compute node, are as they were
    text, _ = setup_stage(DESKTOP_SETUP, 'rocky10', lustre=False)
    assert 'FSX_NEWEST_SERIES' not in text and 'idea-rocky-kernel-vault' not in text
    node = render(
        NODE,
        'rocky10',
        drivers=('fsx_lustre',),
        variables={'job': job(), 'job_directory': '/data/job'},
    )
    assert 'FSX_NEWEST_SERIES' not in node
