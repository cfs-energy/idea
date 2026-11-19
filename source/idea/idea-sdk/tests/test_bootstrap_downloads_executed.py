"""
Run each download-and-run or download-and-build step of the rendered bootstrap in a sandbox,
with the network tools stubbed: the file is verified before it runs, it runs under a name the
real tool accepts, and a failed check or a failed run stops the step with its stated reason.
"""

import hashlib

import pytest

from test_bootstrap_secrets_and_downloads import COMMON, LOG_STUBS, bash
from test_bootstrap_shell_syntax import BASH, Config
from test_image_bake_bootstrap import render

pytestmark = pytest.mark.skipif(BASH is None, reason='bash not installed')

FAILURE = """
fail_bootstrap() { echo "FAILED $1: $2"; exit 1; }
"""
RUSTUP_DIGEST = 'b1c8cb14e30889e7e2999e6b69e62d9e9b9ffc0f4df8180564d306d9680e6c55'


def function_body(text, name):
    start = text.index(f'function {name} () {{')
    return text[start : text.index('\n}\n', start) + 3]


def fake_installer(record, exit_code=0):
    """A stand-in installer that records the name it ran under and its arguments."""
    return f'#!/bin/bash\necho "$(basename "$0") $*" >> {record}\nexit {exit_code}\n'


def efs_config(mount_type='efs'):
    """The mock cluster's EFS shares, mounted with the helper (efs) or as nfs4."""
    config = Config()
    for share in ('apps', 'data'):
        config.put(
            f'shared-storage.{share}.mount_options',
            'efs _netdev,noresvport,tls,iam 0 0'
            if mount_type == 'efs'
            else 'nfs4 nfsvers=4.1,hard 0 0',
        )
    return config


def rustup_run(tmp_path, installer, served=None, required=True):
    text = render(
        '_templates/linux/efs_mount_helper.jinja2',
        'rhel9',
        config=efs_config('efs' if required else 'nfs4'),
    )
    flag = 'EFS_UTILS_REQUIRED=' + ('yes' if required else 'no') + '\n'
    assert flag in text
    body = (
        flag
        + function_body(text, 'efs_utils_unavailable')
        + function_body(text, 'install_rust_toolchain')
    )
    served = installer if served is None else served
    # the bundle pins the real installer's digest; the sandbox pins the stand-in's instead
    body = body.replace(RUSTUP_DIGEST, hashlib.sha256(installer.encode()).hexdigest())
    payload = tmp_path / 'payload'
    payload.write_text(served)
    stubs = f"""
uname() {{ echo x86_64; }}
curl() {{ local out; while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out=$2; shift; done; command cp {payload} "$out"; }}
"""
    return bash(
        f'source {COMMON}\n'
        + LOG_STUBS
        + FAILURE
        + stubs
        + body
        + 'install_rust_toolchain && echo TOOLCHAIN\n'
    )


def test_rustup_init_runs_under_its_installer_name_after_its_digest(tmp_path):
    record = tmp_path / 'ran'
    result = rustup_run(tmp_path, fake_installer(record))
    assert result.returncode == 0, result.stdout + result.stderr
    assert record.read_text() == (
        'rustup-init -y --profile minimal --no-modify-path --default-toolchain 1.100.0\n'
    )


def test_rustup_init_that_fails_its_digest_never_runs(tmp_path):
    record = tmp_path / 'ran'
    result = rustup_run(
        tmp_path, fake_installer(record), served=fake_installer(record) + '# changed\n'
    )
    assert result.returncode == 1
    assert (
        'FAILED download-unverified: rustup-init failed its checksum' in result.stdout
    )
    assert not record.exists()


def test_rustup_init_that_exits_non_zero_fails_the_build_step(tmp_path):
    record = tmp_path / 'ran'
    result = rustup_run(tmp_path, fake_installer(record, exit_code=3))
    assert result.returncode == 1 and 'TOOLCHAIN' not in result.stdout
    assert 'FAILED efs-utils-build-failed: rustup-init exited 3' in result.stdout


def test_rustup_failure_only_warns_where_no_share_mounts_with_the_helper(tmp_path):
    record = tmp_path / 'ran'
    result = rustup_run(
        tmp_path,
        fake_installer(record),
        served=fake_installer(record) + '# changed\n',
        required=False,
    )
    assert 'FAILED' not in result.stdout and 'TOOLCHAIN' not in result.stdout
    assert 'continuing without the EFS mount helper' in result.stdout
    assert not record.exists()


@pytest.mark.parametrize(
    'stored,resolves',
    [
        ('4c11ca4a13992802efb450a15be9e82ea501c965', True),
        ('4C11CA4A13992802EFB450A15BE9E82EA501C965', True),
        ('4c11ca4', True),
        ('v23.06.06', True),
        ('master', True),
        ('no-such-ref', False),
    ],
)
def test_openpbs_resolves_the_stored_reference_to_a_commit(tmp_path, stored, resolves):
    commit = '4c11ca4a13992802efb450a15be9e82ea501c965'
    config = Config()
    config.put('global-settings.package_config.openpbs.commit', stored)
    text = render('_templates/linux/openpbs.jinja2', 'amazonlinux2023', config=config)
    start = text.index('function install_openpbs_git() {')
    body = text[start : text.index('\n}\n', start) + 3].replace(
        '/root/bootstrap', str(tmp_path)
    )
    record = tmp_path / 'checked-out'
    # a repository that knows the full hash, its abbreviation, a tag and a branch
    stubs = f"""
check_openpbs_git_installed() {{ return 1; }}
install_openpbs_dependencies() {{ :; }}
git() {{
  case "$1" in
    clone) command mkdir -p openpbs ;;
    rev-parse)
      if [[ "$2" == HEAD ]]; then cat {record}; return; fi
      case "$4" in
        {commit}^{{commit}}|4c11ca4^{{commit}}|v23.06.06^{{commit}}|master^{{commit}}) echo {commit} ;;
        *) return 1 ;;
      esac ;;
    checkout) echo "$3" > {record} ;;
  esac
}}
compile_and_install_openpbs() {{ echo "COMPILED $(cat {record})"; }}
pushd() {{ builtin cd "$1"; }}
popd() {{ :; }}
"""
    result = bash(LOG_STUBS + FAILURE + stubs + body + 'install_openpbs_git\n')
    if resolves:
        assert f'COMPILED {commit}' in result.stdout, result.stdout + result.stderr
    else:
        assert 'COMPILED' not in result.stdout
        assert (
            'FAILED download-unverified: OpenPBS checkout of no-such-ref (commit not found) failed'
            in result.stdout
        )


def test_cjson_builds_only_the_pinned_commit(tmp_path):
    body = function_body(
        render('_templates/linux/cjson_devel.jinja2', 'rhel9'), 'install_cjson_devel'
    )
    for head, built in (
        ('c859b25da02955fef659d658b8f324b5cde87be3', True),
        ('0' * 40, False),
    ):
        stubs = f"""
ldconfig() {{ return 1; }}
git() {{ case "$1" in clone) command mkdir -p cJSON ;; rev-parse) echo {head} ;; esac; }}
cmake3() {{ echo BUILT; }}
make() {{ :; }}
ln() {{ :; }}
"""
        result = bash(
            LOG_STUBS + FAILURE + stubs + body + 'install_cjson_devel\n', cwd=tmp_path
        )
        assert ('BUILT' in result.stdout) == built, result.stdout + result.stderr
        if not built:
            assert (
                'FAILED download-unverified: cJSON checkout of pinned commit'
                in result.stdout
            )
        (tmp_path / 'cJSON').exists() and __import__('shutil').rmtree(
            tmp_path / 'cJSON'
        )


def amd_install(tmp_path, signatures_ok):
    text = render(
        '_templates/linux/gpu_drivers.jinja2', 'rocky9', instance_type='g4ad.xlarge'
    )
    body = function_body(text, 'amd_rpms_signed')
    start = text.index('function install_amd_gpu_drivers() {')
    body += text[start : text.index('\n}\n', start) + 3]
    body = body.replace('/root/bootstrap', str(tmp_path)).replace(
        '/bin/amdgpu-install', 'amdgpu_install'
    )
    record = tmp_path / 'installed'
    repo = 'repo.radeon.com/amdgpu/6.4.1/el/9.4/main/x86_64'
    stubs = f"""
awk() {{ echo 9.4; }}
systemctl() {{ return 1; }}
wget() {{ command mkdir -p {repo}; local pattern=$5; touch "{repo}/${{pattern//\\*/6.4.1}}"; }}
rpmkeys() {{
  [[ "$3" == --import ]] && return 0
  if [[ {str(signatures_ok).lower()} == true ]]; then echo "$4: digests signatures OK"; else echo "$4: digests SIGNATURES NOT OK"; fi
}}
package_transaction() {{ echo "$*" >> {record}; }}
amdgpu_install() {{ echo "amdgpu-install $*" >> {record}; }}
set_reboot_required() {{ :; }}
"""
    return bash(
        LOG_STUBS + FAILURE + stubs + body + 'install_amd_gpu_drivers\n', cwd=tmp_path
    ), record


def test_amd_packages_install_only_after_their_signatures_verify(tmp_path):
    result, record = amd_install(tmp_path, signatures_ok=True)
    installed = record.read_text()
    assert (
        'localinstall -y repo.radeon.com/amdgpu/6.4.1/el/9.4/main/x86_64/amdgpu-install-6.4.1.noarch.rpm'
        in installed
    ), result.stdout
    assert 'amdgpu-install --usecase=workstation' in installed


def test_amd_packages_with_a_bad_signature_install_nothing(tmp_path):
    result, record = amd_install(tmp_path, signatures_ok=False)
    assert result.returncode == 1
    assert (
        'FAILED download-unverified: amdgpu-install RPM is not signed by the AMD ROCm key'
        in result.stdout
    )
    assert not record.exists() or 'localinstall' not in record.read_text()


@pytest.mark.parametrize('name', ['fsx-rpm-public-key', 'fsx-rpm-public-key-2'])
def test_fsx_rpm_key_import_uses_the_carried_key(tmp_path, name):
    text = render('_templates/linux/fsx_lustre_client.jinja2', 'rhel9')
    body = function_body(text, 'fsx_lustre_key') + function_body(
        text, 'import_fsx_rpm_key'
    )
    record = tmp_path / 'imported'
    stubs = f'rpm() {{ command cp "$2" {record}; }}\n'
    assert bash(stubs + body + f'import_fsx_rpm_key {name}\n').returncode == 0
    carried = 'FSX-LUSTRE-RPM-KEY' + ('-2' if name.endswith('-2') else '')
    path = f'{COMMON.rsplit("/common/", 1)[0]}/_templates/linux/{carried}'
    assert record.read_text().strip() == open(path).read().strip()
    # an unknown key name imports nothing
    record.unlink()
    assert bash(stubs + body + 'import_fsx_rpm_key fsx-other-key\n').returncode != 0
    assert not record.exists()


def fsx_scratch_run(tmp_path, responses):
    """Runs the new-FSx scratch step with aws answering from <responses>, one per call."""
    from test_bootstrap_shell_syntax import JOB_PARAMS, job

    text = render(
        'compute-node/_templates/scratch_storage.jinja2',
        'rhel9',
        variables={'job': job(**JOB_PARAMS['new fsx'])},
    )
    body = function_body(text, 'setup_scratch_storage_fsx_for_lustre')
    scratch = tmp_path / 'fsx'
    body = body.replace("FSX_MOUNTPOINT='/fsx'", f"FSX_MOUNTPOINT='{scratch}'")
    answers = tmp_path / 'answers'
    answers.write_text(''.join(f'{r}\n' for r in responses))
    stubs = f"""
aws() {{
  local answer; answer=$(head -n 1 {answers}); sed -i '1d' {answers}
  [[ "$answer" == - ]] || echo "$answer"
}}
sleep() {{ :; }}
add_fsx_lustre_to_fstab() {{ echo "FSTAB $1 $2" | tee {tmp_path}/fstab; }}
awk() {{ echo "$1@tcp:/fsx"; }}
"""
    return bash(
        f'BOOTSTRAP_DIR={tmp_path}\n'
        + LOG_STUBS
        + FAILURE
        + stubs
        + body
        + 'setup_scratch_storage_fsx_for_lustre\n'
    )


def test_new_fsx_is_found_and_mounted_when_its_tag_appears_late(tmp_path):
    # tags not visible on the first lookup, then CREATING, then AVAILABLE
    arn = 'arn:aws:fsx:us-east-1:123456789012:file-system/fs-0123'
    result = fsx_scratch_run(tmp_path, ['-', arn, 'CREATING', 'AVAILABLE'])
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'FSTAB fs-0123.fsx.' in result.stdout


def test_new_fsx_that_never_becomes_available_stops_the_node(tmp_path):
    arn = 'arn:aws:fsx:us-east-1:123456789012:file-system/fs-0123'
    result = fsx_scratch_run(tmp_path, [arn] + ['CREATING'] * 10)
    assert result.returncode == 1
    assert 'FAILED scratch-unavailable:' in result.stdout and 'fs-0123' in result.stdout
    assert not (tmp_path / 'fstab').exists()
