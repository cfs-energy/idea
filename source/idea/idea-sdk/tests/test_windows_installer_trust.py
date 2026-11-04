"""
Windows desktops and image bakes run every downloaded installer as SYSTEM. Each one must be
checked against a pinned publisher (Authenticode) or a pinned SHA-256 before it runs.
"""

import re
from pathlib import Path

import pytest

from test_bootstrap_shell_syntax import IDEA_BOOTSTRAP_DIR
from test_image_bake_bootstrap import WINDOWS, render

INSTALL = Path(IDEA_BOOTSTRAP_DIR) / 'virtual-desktop-host-windows/Install.ps1'
INSTANCE_TYPES = ['m7i.large', 'g5.xlarge', 'g4ad.xlarge']
AMAZON = [
    'CN=Amazon Web Services, Inc.',
    'O=Amazon Web Services, Inc.',
    'L=Seattle',
    'C=US',
]
NVIDIA = ['CN=NVIDIA Corporation', 'O=NVIDIA Corporation', 'L=Santa Clara', 'C=US']


def function_body(script, name):
    start = script.index(f'function {name} {{')
    end = re.compile(r'^\s*function ', re.M).search(script, start + 1)
    return script[start : end.start() if end else len(script)]


def scripts():
    yield 'Install.ps1', INSTALL.read_text()
    for base_os in WINDOWS:
        for instance_type in INSTANCE_TYPES:
            for name in (
                'dcv-host-ami-builder-windows/Setup.ps1.jinja2',
                'virtual-desktop-host-windows/Configure.ps1.jinja2',
            ):
                yield (
                    f'{name} {base_os} {instance_type}',
                    render(name, base_os, instance_type),
                )


def test_trust_check_needs_a_valid_signature_from_the_pinned_publisher():
    body = function_body(INSTALL.read_text(), 'Assert-InstallerTrusted')
    assert 'Get-AuthenticodeSignature -LiteralPath $Path' in body
    assert """if ("$($Signature.Status)" -ne 'Valid')""" in body
    # Valid alone accepts any signer: the signer subject must hold every pinned attribute
    for pin in AMAZON + NVIDIA:
        assert f"'{pin}'" in body
    assert '$Subject -cnotcontains $_' in body
    # OU is not vetted and differs between certificates of the same publisher
    assert 'OU=' not in body
    assert 'Get-FileHash -LiteralPath $Path -Algorithm SHA256' in body
    # the pass path says so in the bootstrap log, so bake and boot logs prove the check ran
    sha_pass = body.index('if ($Actual -eq $Sha256) {')
    log = (
        'Write-ToLog -Message "Verified $(Split-Path -Leaf $Path): SHA-256 matches pin"'
    )
    assert body.index(log, sha_pass) < body.index('return', sha_pass)
    pub_pass = body.index('if ($Missing.Count -eq 0) {')
    log = 'Write-ToLog -Message "Verified $(Split-Path -Leaf $Path): Authenticode publisher $($Publishers[$Publisher][0].Substring(3))"'
    assert body.index(log, pub_pass) < body.index('return', pub_pass)
    # neither pin given is a refusal, not a pass
    assert 'no publisher or SHA-256 is pinned for it' in body
    refusal = body.index(
        'Write-ToLog -Message "Refusing to install ${Path}: $Reason" -Level \'Error\''
    )
    assert body.index('exit 1', refusal) > refusal


def test_downloads_are_checked_before_save_installer_returns():
    body = function_body(INSTALL.read_text(), 'Save-Installer')
    assert body.rstrip().endswith('Assert-InstallerTrusted -Path $OutFile @Pin\n}')


@pytest.mark.parametrize('label,script', list(scripts()))
def test_every_windows_download_is_pinned(label, script):
    # the only direct download is inside Save-Installer, which checks what it saved
    downloads = re.findall(r'Invoke-WebRequest[^\n]*-OutFile[^\n]*', script)
    assert downloads == [
        'Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing -ErrorAction Stop'
    ] or (downloads == [] and 'function Save-Installer' not in script), (
        f'{label}: {downloads}'
    )
    for call in re.findall(r'^\s*Save-Installer -Uri [^\n]*', script, re.M):
        assert re.search(r'-Publisher Amazon$|-Sha256 \$Sha256$', call), (
            f'{label}: {call}'
        )


def test_dcv_installers_must_be_signed_by_amazon():
    install = INSTALL.read_text()
    for msi in (
        'nice-dcv-server-x64-Release.msi -OutFile C:\\Windows\\Temp\\DCVServer.msi',
        'nice-dcv-session-manager-agent-x64-Release.msi -OutFile C:\\Windows\\Temp\\DCVSMAgent.msi',
    ):
        line = next(x for x in install.splitlines() if msi in x)
        assert line.strip().startswith('Save-Installer -Uri https://')
        assert line.rstrip().endswith('-Publisher Amazon')
    assert install.count('-OutFile "$workdir\\AWSCLIV2.msi" -Publisher Amazon') == 2


def test_seven_zip_is_a_release_pinned_by_sha256():
    body = function_body(INSTALL.read_text(), 'Install-7Zip')
    assert 'best_release.json' not in body
    assert re.search(r"\$Sha256 = '[0-9A-F]{64}'", body)
    assert '$source = "https://www.7-zip.org/a/7z$Version-x64.exe"' in body
    assert 'Save-Installer -Uri $source -OutFile $destination -Sha256 $Sha256' in body


@pytest.mark.parametrize('base_os', WINDOWS)
def test_image_bake_checks_ec2launch_and_cloudwatch_agent(base_os):
    setup = render('dcv-host-ami-builder-windows/Setup.ps1.jinja2', base_os)
    assert (
        "Save-Installer -Uri 'https://s3.amazonaws.com/amazon-ec2launch-v2/windows/amd64/latest/AmazonEC2Launch.msi' -OutFile $Installer -Publisher Amazon"
        in setup
    )
    assert (
        'Save-Installer -Uri $CLOUDWATCH_AGENT_DOWNLOAD_LINK -OutFile $LocalPackagePath -Publisher Amazon'
        in setup
    )
    # the bake inlines the same checks as the desktop
    assert 'function Assert-InstallerTrusted' in setup


@pytest.mark.parametrize('base_os', WINDOWS)
@pytest.mark.parametrize(
    'name',
    [
        'dcv-host-ami-builder-windows/Setup.ps1.jinja2',
        'virtual-desktop-host-windows/Configure.ps1.jinja2',
    ],
)
def test_nvidia_driver_is_checked_before_it_is_extracted_or_run(base_os, name):
    script = render(name, base_os, 'g5.xlarge')
    check = script.index(
        'Assert-InstallerTrusted -Path $driverFile.FullName -Publisher NVIDIA'
    )
    assert check > script.index('$driverFile = Get-ChildItem')
    assert check < script.index('7z x $driverFile.FullName')
    assert check < script.index('setup.exe" -ArgumentList "-n", "-s"')
