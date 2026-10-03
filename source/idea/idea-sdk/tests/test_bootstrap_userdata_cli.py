"""A baked image keeps the AWS CLI the userdata installed; boots do not reinstall it."""

import subprocess

import pytest

from ideasdk.bootstrap import BootstrapUserDataBuilder


@pytest.mark.parametrize('substitution', [True, False])
@pytest.mark.parametrize('baked', [True, False])
def test_install_aws_cli_returns_early_on_a_baked_image(tmp_path, substitution, baked):
    userdata = BootstrapUserDataBuilder(
        base_os='amazonlinux2023',
        aws_region='us-east-1',
        bootstrap_package_uri='s3://bucket/key.tar.gz',
        install_commands=['true'],
        substitution_support=substitution,
    ).build()
    start = userdata.index('function install_aws_cli () {')
    body = userdata[start : userdata.index('\n}\n', start) + 3]
    root = tmp_path / 'root'
    if baked:
        for path in ('usr/local/aws-cli/v2/current/bin/aws', 'bin/aws'):
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text('')
            (root / path).chmod(0o755)
    body = body.replace('/usr/local/aws-cli', f'{root}/usr/local/aws-cli')
    body = body.replace(' -x /bin/aws', f' -x {root}/bin/aws')
    # any reinstall step fails the probe
    script = 'yum() { exit 7; }; curl() { exit 7; }; cd() { exit 7; }\n'
    script += body + 'install_aws_cli\n'
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert (result.returncode == 0) == baked, result.stderr
