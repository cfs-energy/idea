"""
A host renders its bootstrap from the cluster's stored settings, which an upgrade keeps when an
operator wrote them and which differ between clusters. Each checked download must pass for every
value a release has shipped, and refuse any other value with a message naming the setting that
supplies its digest or commit.
"""

import hashlib
import shutil

import pytest

from test_bootstrap_secrets_and_downloads import COMMON, LOG_STUBS, bash
from test_bootstrap_shell_syntax import BASH, Config
from test_image_bake_bootstrap import render

pytestmark = pytest.mark.skipif(BASH is None, reason='bash not installed')

RELEASES = (
    'https://github.com/prometheus/{0}/releases/download/v{1}/{0}-{1}.linux-{2}.tar.gz'
)
PROMETHEUS = 'global-settings.package_config.prometheus.installer.linux'
NODE_EXPORTER = (
    'global-settings.package_config.prometheus.exporters.node_exporter.linux'
)
FAILURE = """
fail_bootstrap() { echo "FAILED $1: $2"; exit 1; }
"""


def install_function(template, start, end, config, variables=None):
    text = render(
        f'_templates/linux/{template}', 'rhel9', config=config, variables=variables
    )
    return text[text.index(start) : text.index(end)]


def release_archive(tmp_path, binary, top='custom-9.9.9.linux-amd64'):
    """A real archive laid out like a release: one top directory holding the binary."""
    import tarfile

    source = tmp_path / 'src' / top
    source.mkdir(parents=True, exist_ok=True)
    (source / binary).write_text('#!/bin/sh\n')
    archive = tmp_path / f'{top}.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(source, arcname=top)
    return archive


def run_download(tmp_path, body, call, payload, machine='x86_64'):
    """Runs <call> from <body> with a download that yields the file <payload>."""
    work = tmp_path / 'work'
    work.mkdir(exist_ok=True)
    installed = tmp_path / 'installed'
    installed.mkdir(exist_ok=True)
    stubs = f"""
uname() {{ echo {machine}; }}
wget() {{ command cp {payload} "${{1##*/}}"; }}
pushd() {{ builtin cd {work}; }}
"""
    body = body.replace('/usr/local/bin/', f'{installed}/').replace(
        'mkdir -p ${PROMETHEUS_DIR}', ':'
    )
    result = bash(f'source {COMMON}\n' + LOG_STUBS + FAILURE + stubs + body + call)
    result.installed = sorted(p.name for p in installed.iterdir())
    return result


@pytest.mark.parametrize(
    'prefix,start,end,call,name,version',
    [
        (
            PROMETHEUS,
            'PROMETHEUS_AMD64_URL=',
            'function setup_prometheus_service',
            'install_prometheus\n',
            'prometheus',
            '2.53.4',
        ),
        (
            PROMETHEUS,
            'PROMETHEUS_AMD64_URL=',
            'function setup_prometheus_service',
            'install_prometheus\n',
            'prometheus',
            '2.37.0',
        ),
        (
            NODE_EXPORTER,
            'PROMETHEUS_NODE_EXPORTER_AMD64_URL=',
            'function setup_prometheus_node_exporter_service',
            'install_prometheus_node_exporter\n',
            'node_exporter',
            '1.9.1',
        ),
        (
            NODE_EXPORTER,
            'PROMETHEUS_NODE_EXPORTER_AMD64_URL=',
            'function setup_prometheus_node_exporter_service',
            'install_prometheus_node_exporter\n',
            'node_exporter',
            '1.3.1',
        ),
    ],
)
def test_metrics_archives_from_an_older_stored_release_are_pinned(
    tmp_path, prefix, start, end, call, name, version
):
    config = Config()
    for arch, label in (('x86_64', 'amd64'), ('aarch64', 'arm64')):
        config.put(f'{prefix}.{arch}', RELEASES.format(name, version, label))
    body = install_function(
        'prometheus.jinja2'
        if name == 'prometheus'
        else 'prometheus_node_exporter.jinja2',
        start,
        end,
        config,
        {'prometheus_exporters': ['node_exporter'], 'additional_scrape_configs': None},
    )
    archive = f'{name}-{version}.linux-amd64.tar.gz'
    assert f'    {archive}) echo -n ' in body
    # the carried digest is the release's, so a different archive under that name is refused
    payload = tmp_path / 'not-the-release'
    payload.write_bytes(b'not the release archive')
    result = run_download(tmp_path, body, call, payload)
    assert result.returncode == 1
    assert f'FAILED download-unverified: {archive} failed its checksum' in result.stdout
    assert result.installed == []


@pytest.mark.parametrize(
    'prefix,template,start,end,call',
    [
        (
            PROMETHEUS,
            'prometheus.jinja2',
            'PROMETHEUS_AMD64_URL=',
            'function setup_prometheus_service',
            'install_prometheus\n',
        ),
        (
            NODE_EXPORTER,
            'prometheus_node_exporter.jinja2',
            'PROMETHEUS_NODE_EXPORTER_AMD64_URL=',
            'function setup_prometheus_node_exporter_service',
            'install_prometheus_node_exporter\n',
        ),
    ],
)
def test_metrics_archive_from_a_mirror_needs_its_digest_in_settings(
    tmp_path, prefix, template, start, end, call
):
    binary = 'prometheus' if template == 'prometheus.jinja2' else 'node_exporter'
    payload = release_archive(tmp_path, binary)
    variables = {
        'prometheus_exporters': ['node_exporter'],
        'additional_scrape_configs': None,
    }
    config = Config()
    config.put(
        f'{prefix}.x86_64',
        'https://mirror.example.invalid/metrics/custom-9.9.9.linux-amd64.tar.gz',
    )
    config.put(
        f'{prefix}.aarch64',
        'https://mirror.example.invalid/metrics/custom-9.9.9.linux-arm64.tar.gz',
    )
    result = run_download(
        tmp_path,
        install_function(template, start, end, config, variables),
        call,
        payload,
    )
    assert result.returncode == 1
    assert f'has no pinned SHA-256: set {prefix}.x86_64_sha256' in result.stdout
    assert result.installed == []
    config.put(
        f'{prefix}.x86_64_sha256', hashlib.sha256(payload.read_bytes()).hexdigest()
    )
    result = run_download(
        tmp_path,
        install_function(template, start, end, config, variables),
        call,
        payload,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.installed == [binary]
    # the arm64 host takes its own URL and its own digest setting
    result = run_download(
        tmp_path,
        install_function(template, start, end, config, variables),
        call,
        payload,
        machine='aarch64',
    )
    assert result.returncode == 1
    assert f'set {prefix}.aarch64_sha256' in result.stdout


def efs_clone(tmp_path, config, head):
    from test_bootstrap_downloads_executed import function_body

    for share in ('apps', 'data'):
        # a share that mounts with the helper, so a failed clone stops the host
        config.put(f'shared-storage.{share}.mount_options', 'efs _netdev,tls 0 0')
    text = render('_templates/linux/efs_mount_helper.jinja2', 'rhel9', config=config)
    body = (
        'EFS_UTILS_REQUIRED=yes\n'
        + function_body(text, 'efs_utils_unavailable')
        + function_body(text, 'clone_efs_utils')
    )
    stubs = f"""
git() {{
  case "$1" in
    clone) echo "clone $*" ;;
    -C) echo {head} ;;
  esac
}}
rm() {{ :; }}
timeout() {{ shift; "$@"; }}
"""
    return bash(
        LOG_STUBS + FAILURE + stubs + body + 'clone_efs_utils && echo CLONED\n',
        cwd=tmp_path,
    )


def test_efs_utils_version_without_a_pinned_commit_names_the_commit_setting(tmp_path):
    config = Config()
    config.put('global-settings.package_config.efs_utils.version', 'v3.2.0')
    result = efs_clone(tmp_path, config, '1111111111111111111111111111111111111111')
    assert result.returncode == 1
    assert 'set global-settings.package_config.efs_utils.commit' in result.stdout
    config.put(
        'global-settings.package_config.efs_utils.commit',
        '1111111111111111111111111111111111111111',
    )
    result = efs_clone(tmp_path, config, '1111111111111111111111111111111111111111')
    assert result.returncode == 0 and 'CLONED' in result.stdout
    # the commit setting is a pin, not a hint: a clone at another commit is refused
    result = efs_clone(tmp_path, config, '2222222222222222222222222222222222222222')
    assert result.returncode == 1
    assert (
        'is not the pinned commit 1111111111111111111111111111111111111111'
        in result.stdout
    )


def test_openpbs_builds_the_commit_stored_on_this_cluster(tmp_path):
    stored = '4c11ca4a13992802efb450a15be9e82ea501c965'
    config = Config()
    config.put('global-settings.package_config.openpbs.commit', stored)
    text = render('_templates/linux/openpbs.jinja2', 'amazonlinux2023', config=config)
    start = text.index('function install_openpbs_git() {')
    body = text[start : text.index('\n}\n', start) + 3].replace(
        '/root/bootstrap', str(tmp_path)
    )
    stubs = f"""
check_openpbs_git_installed() {{ return 1; }}
install_openpbs_dependencies() {{ :; }}
git() {{
  case "$1" in
    clone) command mkdir -p openpbs ;;
    checkout) [[ "$3" == {stored} ]] ;;
    rev-parse) echo {stored} ;;
  esac
}}
compile_and_install_openpbs() {{ echo COMPILED; }}
pushd() {{ builtin cd "$1"; }}
popd() {{ :; }}
"""
    result = bash(LOG_STUBS + FAILURE + stubs + body + 'install_openpbs_git\n')
    assert 'COMPILED' in result.stdout, result.stdout + result.stderr
    assert 'FAILED' not in result.stdout


PATTERN = 'https://s3.%region%.amazonaws.com/amazoncloudwatch-agent-%region%/%os%/%architecture%/latest/amazon-cloudwatch-agent.%ext%'


def cloudwatch_install(
    tmp_path,
    config,
    region,
    served,
    bad_signatures=None,
    base_os='rhel9',
    machine='x86_64',
):
    from ideasdk.context import BootstrapContext
    from jinja2 import Environment, FileSystemLoader
    from test_bootstrap_shell_syntax import IDEA_BOOTSTRAP_DIR

    config.put('cluster.aws.region', region)
    context = BootstrapContext(
        config=config,
        module_name='scheduler',
        module_id='scheduler',
        module_set='default',
        base_os=base_os,
        instance_type='m7i.large',
    )
    text = (
        Environment(loader=FileSystemLoader(IDEA_BOOTSTRAP_DIR))
        .get_template('_templates/linux/cloudwatch_agent.jinja2')
        .render(context=context, additional_log_files=None)
    )
    end = '\n  popd || exit\nfi\n'
    body = text[: text.index(end) + len(end)]
    body = body.replace('/root/bootstrap', str(tmp_path))
    calls = tmp_path / 'fetched'
    if bad_signatures is not None:
        (tmp_path / 'bad-signatures').write_text(str(bad_signatures))
    stubs = f"""
uname() {{ echo {machine}; }}
curl() {{
  local url="" out=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o) out=$2; shift ;;
      --retry|--connect-timeout|--max-time) shift ;;
      -*) ;;
      *) url=$1 ;;
    esac
    shift
  done
  echo "$url" >> {calls}
  [[ " {served} " == *" $url "* ]] || return 22
  printf 'package:%s' "${{url%.sig}}" > "$out"
}}
verify_detached_signature() {{
  echo "VERIFY $3"
  [[ -f {tmp_path}/bad-signatures ]] || return 0
  local left=$(cat {tmp_path}/bad-signatures)
  echo $((left - 1)) > {tmp_path}/bad-signatures
  [[ $left -le 0 ]]
}}
rpm() {{ echo INSTALLED; }}
dpkg() {{ echo INSTALLED; }}
"""
    return bash(f'source {COMMON}\n' + LOG_STUBS + FAILURE + stubs + body), calls


@pytest.mark.parametrize('region', ['us-east-1', 'us-gov-west-1'])
def test_cloudwatch_signature_url_follows_the_stored_pattern_in_every_region(
    tmp_path, region
):
    # stored on both reference clusters: download_link null, the regional pattern
    config = Config()
    config.put(
        'global-settings.package_config.amazon_cloudwatch_agent.download_link', None
    )
    config.put(
        'global-settings.package_config.amazon_cloudwatch_agent.download_link_pattern',
        PATTERN,
    )
    package = f'https://s3.{region}.amazonaws.com/amazoncloudwatch-agent-{region}/redhat/amd64/latest/amazon-cloudwatch-agent.rpm'
    result, calls = cloudwatch_install(
        tmp_path, config, region, f'{package} {package}.sig'
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().split() == [package, f'{package}.sig']
    assert (
        'VERIFY ./amazon-cloudwatch-agent.rpm.sig' in result.stdout
        and 'INSTALLED' in result.stdout
    )
    # where the signature is not served, nothing is installed and the message names the settings
    (tmp_path / 'fetched').unlink()
    result, _ = cloudwatch_install(tmp_path, config, region, package)
    assert result.returncode == 1 and 'INSTALLED' not in result.stdout
    assert f'no signature at {package}.sig' in result.stdout
    assert 'amazon_cloudwatch_agent.sha256' in result.stdout


def test_cloudwatch_explicit_download_link_is_signature_checked_or_digest_pinned(
    tmp_path,
):
    link = 'https://mirror.example.invalid/cw/1.300/amazon-cloudwatch-agent.rpm'
    config = Config()
    config.put(
        'global-settings.package_config.amazon_cloudwatch_agent.download_link', link
    )
    result, calls = cloudwatch_install(
        tmp_path, config, 'us-east-1', f'{link} {link}.sig'
    )
    assert result.returncode == 0 and calls.read_text().split() == [link, f'{link}.sig']
    config.put(
        'global-settings.package_config.amazon_cloudwatch_agent.sha256',
        hashlib.sha256(f'package:{link}'.encode()).hexdigest(),
    )
    (tmp_path / 'fetched').unlink()
    result, calls = cloudwatch_install(tmp_path, config, 'us-east-1', link)
    assert result.returncode == 0 and 'INSTALLED' in result.stdout, result.stdout
    assert calls.read_text().split() == [link]
    config.put(
        'global-settings.package_config.amazon_cloudwatch_agent.sha256', '0' * 64
    )
    result, _ = cloudwatch_install(tmp_path, config, 'us-east-1', link)
    assert result.returncode == 1 and 'INSTALLED' not in result.stdout
    assert (
        'matches no digest in global-settings.package_config.amazon_cloudwatch_agent.sha256'
        in result.stdout
    )


def test_cloudwatch_digests_cover_each_package_the_pattern_resolves(tmp_path):
    # a versioned mirror pattern: an x86_64 RPM and an arm64 DEB, one digest for each
    pattern = 'https://mirror.example.invalid/cw/1.300/%os%/%architecture%/amazon-cloudwatch-agent.%ext%'
    rpm = 'https://mirror.example.invalid/cw/1.300/redhat/amd64/amazon-cloudwatch-agent.rpm'
    deb = 'https://mirror.example.invalid/cw/1.300/ubuntu/arm64/amazon-cloudwatch-agent.deb'
    config = Config()
    config.put(
        'global-settings.package_config.amazon_cloudwatch_agent.download_link_pattern',
        pattern,
    )
    config.put(
        'global-settings.package_config.amazon_cloudwatch_agent.sha256',
        ', '.join(
            hashlib.sha256(f'package:{u}'.encode()).hexdigest() for u in (rpm, deb)
        ),
    )
    for base_os, machine, url in (
        ('rhel9', 'x86_64', rpm),
        ('ubuntu2404', 'aarch64', deb),
    ):
        result, calls = cloudwatch_install(
            tmp_path, config, 'us-east-1', url, base_os=base_os, machine=machine
        )
        assert result.returncode == 0 and 'INSTALLED' in result.stdout, (
            result.stdout + result.stderr
        )
        assert calls.read_text().split() == [url]
        calls.unlink()


def test_bundle_constant_downloads_do_not_read_cluster_settings():
    # rustup-init, cJSON, the AMD ROCm RPMs, the FSx repository keys and the AWS CLI come from
    # constants in this bundle; no stored setting changes what they fetch or how it is checked.
    texts = {
        name: open(f'{COMMON.rsplit("/common/", 1)[0]}/_templates/linux/{name}').read()
        for name in ('cjson_devel.jinja2', 'fsx_lustre_client.jinja2')
    }
    assert 'context.config' not in texts['cjson_devel.jinja2']
    assert 'fsx-lustre-client-repo-public-keys' not in texts['fsx_lustre_client.jinja2']
    efs = open(
        f'{COMMON.rsplit("/common/", 1)[0]}/_templates/linux/efs_mount_helper.jinja2'
    ).read()
    rustup = efs[
        efs.index('function install_rust_toolchain') : efs.index(
            'function clone_efs_utils'
        )
    ]
    assert 'context.config' not in rustup
    gpu = open(
        f'{COMMON.rsplit("/common/", 1)[0]}/_templates/linux/gpu_drivers.jinja2'
    ).read()
    amd = gpu[
        gpu.index('function amd_rpms_signed') : gpu.index(
            'function install_amd_gpu_drivers'
        )
    ]
    assert 'context.config' not in amd
    assert 'AMD_RADEON_REPO_BASE_URL="https://repo.radeon.com/amdgpu"' in gpu
    assert shutil.which('bash')


@pytest.mark.parametrize('bad,installed', [(1, True), (2, False)])
def test_cloudwatch_refetches_the_pair_once_when_the_signature_does_not_match(
    tmp_path, bad, installed
):
    # a release published between the package and its signature mismatches once
    config = Config()
    config.put(
        'global-settings.package_config.amazon_cloudwatch_agent.download_link_pattern',
        PATTERN,
    )
    package = 'https://s3.us-east-1.amazonaws.com/amazoncloudwatch-agent-us-east-1/redhat/amd64/latest/amazon-cloudwatch-agent.rpm'
    result, calls = cloudwatch_install(
        tmp_path, config, 'us-east-1', f'{package} {package}.sig', bad_signatures=bad
    )
    assert calls.read_text().split() == [package, f'{package}.sig'] * 2
    assert ('INSTALLED' in result.stdout) == installed, result.stdout
    if not installed:
        assert result.returncode == 1
        assert 'failed its signature check' in result.stdout


# Each release's own sha256sums.txt lines (fetched from the release pages when the pins were
# added); the digests the templates carry must be exactly these.
RELEASE_SHA256SUMS = """
ca70f5a261fd545da0b9893c42a73547a94ebd5111ef2b6b9f8742c5dbb73522  prometheus-2.37.0.linux-amd64.tar.gz
024cc048131098d408c8c3588237c42de4a13d9030b352f0a0d7f4a734fda102  prometheus-2.37.0.linux-arm64.tar.gz
b8b497c4610d1b93208252b60c8f20f6b2e78596ae8df43397a2e805aa53d475  prometheus-2.53.4.linux-amd64.tar.gz
ec7236ecea7154d0bfe142921708b1ae7b5e921e100e0ee85ab92b7c444357e0  prometheus-2.53.4.linux-arm64.tar.gz
456eb31530484d181e860e329fa0c6628e79eb388fac670cdeb4fa5d57c74ed9  prometheus-2.53.5.linux-amd64.tar.gz
c47274a855b6df116f5f8b6e8583a8f4f482e22e9cff9faf2518f444639a28e0  prometheus-2.53.5.linux-arm64.tar.gz
68f3802c2dd3980667e4ba65ea2e1fb03f4a4ba026cca375f15a0390ff850949  node_exporter-1.3.1.linux-amd64.tar.gz
f19f35175f87d41545fa7d4657e834e3a37c1fe69f3bf56bc031a256117764e7  node_exporter-1.3.1.linux-arm64.tar.gz
becb950ee80daa8ae7331d77966d94a611af79ad0d3307380907e0ec08f5b4e8  node_exporter-1.9.1.linux-amd64.tar.gz
848f139986f63232ced83babe3cad1679efdbb26c694737edc1f4fbd27b96203  node_exporter-1.9.1.linux-arm64.tar.gz
b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63  node_exporter-1.12.1.linux-amd64.tar.gz
ad35b605f9954b9f1ffddf5ba054bdc5a98d790b9eae5291e1eeb83f1ecbd0e7  node_exporter-1.12.1.linux-arm64.tar.gz
"""


@pytest.mark.parametrize(
    'template,function',
    [
        ('prometheus.jinja2', 'prometheus_sha256'),
        ('prometheus_node_exporter.jinja2', 'node_exporter_sha256'),
    ],
)
@pytest.mark.parametrize('arch', ['amd64', 'arm64'])
def test_carried_release_digests_equal_the_published_sums(template, function, arch):
    from test_bootstrap_downloads_executed import function_body

    variables = {
        'prometheus_exporters': ['node_exporter'],
        'additional_scrape_configs': None,
    }
    config = Config()
    for prefix in (PROMETHEUS, NODE_EXPORTER):
        for key in ('x86_64', 'aarch64'):
            config.put(f'{prefix}.{key}', 'https://example.invalid/archive.tar.gz')
    text = render(
        f'_templates/linux/{template}', 'rhel9', config=config, variables=variables
    )
    body = function_body(text, function)
    name = 'prometheus' if function == 'prometheus_sha256' else 'node_exporter'
    expected = [
        line.split()
        for line in RELEASE_SHA256SUMS.strip().splitlines()
        if line.endswith(f'linux-{arch}.tar.gz')
        and line.split()[1].startswith(name + '-')
    ]
    assert len(expected) == 3
    for digest, archive in expected:
        assert bash(body + f'{function} {archive}').stdout == digest
