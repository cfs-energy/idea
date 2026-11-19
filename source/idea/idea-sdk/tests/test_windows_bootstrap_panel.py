"""
Windows desktop bootstrap behaviour, run under pwsh with the Windows cmdlets stubbed:

- a repeated run that installs nothing leaves the DCV services running
- a failed authorization, a failed join or a join with no result stops the bootstrap
- an instance that inherited a domain identity from its image is refused
- the package of this instance is the only one used: another instance's package, scripts and
  transcripts are removed first, and a failed download or extraction stops the user data
- the AMD and NVIDIA driver steps fail when the driver did not arrive or did not extract
- only blank instance-store disks are formatted as ephemeral storage

The one text assertion left is the image builder's log check, which runs inside the builder's
top-level script and cannot be loaded on its own.
"""

from pathlib import Path

import pytest

import windows_pwsh
from test_bootstrap_shell_syntax import IDEA_BOOTSTRAP_DIR
from test_image_bake_bootstrap import render
from test_windows_bootstrap_secrets import (
    ACCOUNT_STUBS,
    FLOW_STUBS,
    JOIN_STUBS,
    active_directory,
)

pytestmark = windows_pwsh.needs_pwsh
INSTALL = Path(IDEA_BOOTSTRAP_DIR, 'virtual-desktop-host-windows/Install.ps1')

SERVICE_STUBS = r"""
function Get-Service { [CmdletBinding()] param($Name) if ($env:IDEA_SERVICE) { [pscustomobject]@{ Name = $Name; Status = $env:IDEA_SERVICE } } }
function Stop-Service { [CmdletBinding()] param($Name) Record "STOP $Name" }
function Save-Installer { [CmdletBinding()] param($Uri, $OutFile, $Publisher) Record "DOWNLOAD $OutFile" }
function Start-Process { [CmdletBinding()] param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru) [pscustomobject]@{ ExitCode = 0 } }
function Wait-ForService { param($Name) }
"""


@pytest.mark.parametrize(
    'function, service',
    [
        ('Install-NiceDCV', 'dcvserver'),
        ('Install-NiceSessionManagerAgent', 'DcvSessionManagerAgentService'),
    ],
)
@pytest.mark.parametrize(
    'installed, update, expected',
    [
        ('Running', False, []),
        ('Running', True, ['STOP {service}', 'DOWNLOAD']),
        ('', False, ['DOWNLOAD']),
    ],
)
def test_a_dcv_service_is_stopped_only_when_it_is_replaced(
    tmp_path, function, service, installed, update, expected
):
    code, calls, output = windows_pwsh.run(
        tmp_path,
        INSTALL,
        functions=[function],
        stubs=SERVICE_STUBS,
        action=f'{function}{" -Update" if update else ""} | Out-Null',
        env={'IDEA_SERVICE': installed},
    )
    assert code == 0, output
    assert [c.split(' C:')[0] for c in calls] == [
        e.format(service=service) for e in expected
    ]


JOIN_FUNCTION_STUBS = r"""
function Get-ADAutomationAuthorizationEntry {
  switch ($env:IDEA_ENTRY) {
    'absent' { $null }
    'fail' { @{ status = 'fail'; error_code = 'MARKER_CODE'; message = 'marker refusal' } }
    default { @{ status = 'success'; domain_controller = 'dc.example.invalid'; ou = ''; hostname = $env:COMPUTERNAME; otp = 'marker-join-value' } }
  } }
function Start-Sleep { [CmdletBinding()] param($Seconds) }
function Add-Computer { [CmdletBinding()] param([switch]$Force, $DomainName, $Credential, $Server, [switch]$PassThru, $OUPath, $Options)
  if ($env:IDEA_RESULT -eq 'none') { return }
  [pscustomobject]@{ HasSucceeded = ($env:IDEA_RESULT -ne 'failed') } }
"""


@pytest.mark.parametrize(
    'entry, result, error',
    [
        (
            'absent',
            '',
            'no authorization for the domain join arrived before the deadline',
        ),
        (
            'fail',
            '',
            'authorization for the domain join failed: (MARKER_CODE) marker refusal',
        ),
        ('success', 'none', 'the domain join did not succeed after 1 attempts'),
        ('success', 'failed', 'the domain join did not succeed after 1 attempts'),
        ('success', 'joined', None),
    ],
)
def test_only_a_successful_join_counts(tmp_path, monkeypatch, entry, result, error):
    active_directory(monkeypatch)
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=['Start-ADAutomationWaitForAuthorizationAndJoin'],
        variables=['AD_DOMAIN_NAME', 'AD_JOIN_MAX_SLEEP', 'AD_JOIN_RETRY_COUNT'],
        stubs=JOIN_FUNCTION_STUBS,
        # no retry wait: the deadline is now
        action="$AD_JOIN_RETRY_COUNT = '0'\n"
        'try { Start-ADAutomationWaitForAuthorizationAndJoin; Record "RETURNED joined=$global:IdeaDomainJoined" }'
        ' catch { Record "THROWN $($_.Exception.Message) joined=$global:IdeaDomainJoined" }',
        env={'IDEA_ENTRY': entry, 'IDEA_RESULT': result, 'COMPUTERNAME': 'IDEA-MARKER'},
    )
    assert code == 0, output
    if error:
        assert calls[-1] == f'THROWN {error} joined=False'
    else:
        assert calls[-1] == 'RETURNED joined=True'
    assert 'marker-join-value' not in '\n'.join(calls) + output


@pytest.mark.parametrize('name, stack', [('IDEA-OLD', True), ('IDEA-NEW', False)])
def test_an_already_joined_desktop_skips_the_join_as_before(
    tmp_path, monkeypatch, name, stack
):
    # "Create Stack from Session": the image boots joined under the source desktop's name. As in
    # 26.11.1 the join and the auto-logon writes are skipped and the desktop goes on to ready;
    # the shared computer account is reported.
    active_directory(monkeypatch)
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=['Configure-WindowsEC2Instance', 'Join-IdeaDirectory'],
        stubs=ACCOUNT_STUBS
        + FLOW_STUBS
        + JOIN_STUBS
        + 'function Get-WmiObject { [CmdletBinding()] param($Class) [pscustomobject]@{ PartOfDomain = $true } }\n'
        + 'function Get-IdeaHostname { "IDEA-NEW" }\n',
        action='Configure-WindowsEC2Instance',
        env={'COMPUTERNAME': name},
    )
    assert code == 7, output
    assert calls[-1] == 'NEXT STEP'
    assert not any(c.startswith(('JOIN', 'REG set', 'RENAME')) for c in calls)
    warning = [
        c for c in calls if c.startswith('LOG Warn: This instance is already joined')
    ]
    if stack:
        assert warning == [
            'LOG Warn: This instance is already joined to the domain as IDEA-OLD, an identity it '
            "took from its image (this desktop's own name would be IDEA-NEW); it shares that "
            'computer account with the source desktop. The join is skipped.'
        ]
    else:
        assert warning == []
    # an image from an earlier local-account desktop is cleaned on a directory cluster too
    assert 'REG remove DefaultPassword' in calls
    assert 'REMOVE C:\\LOCAL_ADMINS_PASSWORD.txt' in calls


AUTHORIZATION_STUBS = r"""
function Get-Random { [CmdletBinding()] param($Minimum, $Maximum) 3 }
function Start-Sleep { [CmdletBinding()] param($Seconds) }
function Send-SQSMessage { [CmdletBinding()] param($QueueUrl, $MessageBody, $MessageGroupId, $MessageDeduplicationId, $Region)
  $global:Sends++
  if ($global:Sends -le [int]$env:IDEA_SEND_FAILURES) { throw 'marker send failure' }
  Record "SENT $global:Sends"; 'sent' }
function Start-ADAutomationWaitForAuthorizationAndJoin { Record 'JOIN join'; $global:IdeaDomainJoined = $true }
"""


@pytest.mark.parametrize('failures, joined', [('1', True), ('7', False)])
def test_a_failed_authorization_send_is_retried(
    tmp_path, monkeypatch, failures, joined
):
    active_directory(monkeypatch)
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=[
            'Join-IdeaDirectory',
            'Start-ADAutomationAuthorization',
            'Send-ADAutomationSQSMessage',
        ],
        stubs=AUTHORIZATION_STUBS,
        action="Join-IdeaDirectory -Hostname 'IDEA-MARKER' -TranscriptPath 'transcript.log'; Record 'AFTER'",
        env={'IDEA_SEND_FAILURES': failures},
    )
    warnings = [
        c
        for c in calls
        if c
        == 'LOG Warn: [Join AD] sending the authorization request failed: marker send failure'
    ]
    assert len(warnings) == min(int(failures), 7)
    if joined:
        assert code == 0, output
        assert (
            calls[-3:] == ['JOIN join', 'TRANSCRIPT start', 'AFTER']
            and 'SENT 2' in calls
        )
    else:
        # every send failed: the join still waits for the authorization deadline (stubbed here)
        assert 'SENT' not in ' '.join(calls)


DDB_STUBS = r"""
function ConvertTo-DDBItem { [CmdletBinding()] param([Parameter(ValueFromPipeline)] $InputObject) process { 'key' } }
function Get-DDBItem { [CmdletBinding()] param($TableName, $Key, $Select)
  $global:Reads++
  if ($global:Reads -eq 1) { throw 'marker read failure' }
  [pscustomobject]@{ IsItemSet = $true; Item = @{ status = @{ S = 'fail' }; error_code = @{ S = 'MARKER' }; message = @{ S = 'refused' } } } }
"""


def test_a_failed_authorization_read_is_polled_again(tmp_path, monkeypatch):
    active_directory(monkeypatch)
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=['Get-ADAutomationAuthorizationEntry'],
        stubs=DDB_STUBS,
        action='$first = Get-ADAutomationAuthorizationEntry; $second = Get-ADAutomationAuthorizationEntry\n'
        'Record "FIRST $($null -eq $first) SECOND $($second.status)"',
    )
    assert code == 0, output
    assert calls == [
        'LOG Warn: [Join AD] reading the authorization entry failed: marker read failure',
        'FIRST True SECOND fail',
    ]


def test_the_ready_event_waits_for_the_domain_join():
    # text: the check runs after the DCV, GPU and storage steps, which need a Windows host
    configure = render(
        'virtual-desktop-host-windows/Configure.ps1.jinja2', 'windows2022'
    )
    assert 'not joined to the domain; the desktop is not configured' not in configure
    template = Path(
        IDEA_BOOTSTRAP_DIR, 'virtual-desktop-host-windows/Configure.ps1.jinja2'
    ).read_text()
    check = template.index('if (-not ($ServerInAD -or $global:IdeaDomainJoined))')
    assert (
        check
        < template.index('Write-ToLog -Message "IDEA_BOOTSTRAP_COMPLETE')
        < template.index(
            'Send-SQSMessage -QueueUrl $VdcRequestQueueURL -MessageBody $Message'
        )
    )
    assert template.index(
        'Send-SQSMessage -QueueUrl $VdcRequestQueueURL -MessageBody $Message'
    ) < (template.index('[IO.File]::WriteAllText($ConfiguredMarker'))


USERDATA_STUBS = r"""
function Get-EC2InstanceMetadata { [CmdletBinding()] param($Category) 'i-0new' }
function Copy-S3Object { [CmdletBinding()] param($BucketName, $Key, $LocalFile, [switch]$Force)
  if ($env:IDEA_COPY -eq 'fail') { throw 'marker download failure' }
  Set-Content -LiteralPath $LocalFile -Value 'archive'; Record "COPY $Key" }
function Tar { Record "TAR $args"; $global:LASTEXITCODE = [int]$env:IDEA_TAR }
function Start-Sleep { [CmdletBinding()] param($Seconds) }
function New-EC2Tag { [CmdletBinding()] param($Resource, $Tag, $Region) Record "TAG $($Tag.Key)=$($Tag.Value)" }
# a file another process holds open cannot be deleted: IDEA_HELD names the folder that holds one
function Remove-Item { [CmdletBinding()] param($LiteralPath, $Path, [switch]$Recurse, [switch]$Force)
  # as on Windows, a folder that holds the open file cannot be removed either
  $holds = $LiteralPath -and (Test-Path -LiteralPath $LiteralPath -PathType Container) -and @(Get-ChildItem -LiteralPath $LiteralPath -Recurse -Force -Filter $env:IDEA_HELD).Count
  if ($env:IDEA_HELD -and ($LiteralPath -like "*$env:IDEA_HELD" -or $holds)) { throw 'The process cannot access the file because it is being used by another process.' }
  if ($LiteralPath) { Microsoft.PowerShell.Management\Remove-Item -LiteralPath $LiteralPath -Recurse:$Recurse -Force:$Force }
  else { Microsoft.PowerShell.Management\Remove-Item -Path $Path -Recurse:$Recurse -Force:$Force } }
# IDEA_NO_WRITE makes every write to the bootstrap log fail
function Add-Content { [CmdletBinding()] param($Path, $LiteralPath, $Value, $Encoding)
  if ($env:IDEA_NO_WRITE -and "$Path" -like '*UserdataExecutionIDEA.log') { throw 'The process cannot access the file because it is being used by another process.' }
  if ($LiteralPath) { Microsoft.PowerShell.Management\Add-Content -LiteralPath $LiteralPath -Value $Value -Encoding utf8 }
  else { Microsoft.PowerShell.Management\Add-Content -Path $Path -Value $Value } }
# file ACLs do not exist off Windows: the object Protect-InheritedLog builds is recorded instead
function New-Object { [CmdletBinding()] param([string]$TypeName, [object[]]$ArgumentList)
  switch ($TypeName) {
    'System.Security.AccessControl.FileSecurity' {
      $acl = [pscustomobject]@{ Protected = $null; Preserve = $null; Rules = [System.Collections.ArrayList]::new() }
      $acl | Add-Member ScriptMethod SetAccessRuleProtection { param($protect, $preserve) $this.Protected = $protect; $this.Preserve = $preserve }
      $acl | Add-Member ScriptMethod AddAccessRule { param($rule) [void]$this.Rules.Add($rule) }
      $acl }
    'System.Security.Principal.SecurityIdentifier' { "$($ArgumentList[0])" }
    'System.Security.AccessControl.FileSystemAccessRule' { "$($ArgumentList[0]):$($ArgumentList[1]):$($ArgumentList[2])" }
    default { throw "unexpected type $TypeName" } } }
function Set-Acl { [CmdletBinding()] param($LiteralPath, $AclObject)
  Record "ACL $(Split-Path -Leaf $LiteralPath) protected=$($AclObject.Protected) inherited=$($AclObject.Preserve) $($AclObject.Rules -join ' ')" }
"""
# windows_pwsh maps C:\ to tmp_path/cdrive
BOOTSTRAP = Path('cdrive/Users/Administrator/IDEA/bootstrap')


def userdata(tmp_path, env, seed=None):
    from ideasdk.bootstrap.bootstrap_userdata_builder import BootstrapUserDataBuilder

    text = BootstrapUserDataBuilder(
        base_os='windows2022',
        aws_region='us-east-1',
        bootstrap_package_uri='s3://bucket-marker/idea/dcv-host-new.tar.gz',
        install_commands=["Record 'INSTALL'"],
    ).build()
    script = tmp_path / 'userdata.ps1'
    script.write_text(
        text.replace('<powershell>', '').replace('</powershell>', ''), encoding='utf-8'
    )
    root = tmp_path / BOOTSTRAP
    if seed:
        for name, content in seed.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(content, encoding='utf-8')
    code, calls, output = windows_pwsh.run(
        tmp_path,
        script,
        stubs=USERDATA_STUBS,
        action=f"& '{script}'; exit $LASTEXITCODE",
        env={'SystemDrive': 'C:', **env},
    )
    left = (
        sorted(str(p.relative_to(root)) for p in root.rglob('*'))
        if root.exists()
        else []
    )
    return code, calls, output, left


FOREIGN = {
    'instance-id': 'i-0old\n',
    'dcv-host-old.tar.gz': 'old archive',
    'virtual-desktop-host-windows/Configure.ps1': 'old script',
    'log/ConfigureEVDI.log.1': 'old transcript',
}


@pytest.mark.parametrize('failure', ['copy', 'tar'])
def test_a_failed_download_or_extraction_stops_the_user_data(tmp_path, failure):
    code, calls, output, left = userdata(
        tmp_path,
        {
            'IDEA_COPY': 'fail' if failure == 'copy' else '',
            'IDEA_TAR': '2' if failure == 'tar' else '0',
        },
        seed=FOREIGN,
    )
    assert code == 1, output
    assert 'INSTALL' not in calls
    expected = (
        'marker download failure' if failure == 'copy' else 'failed with exit code 2'
    )
    assert expected in output
    # the earlier instance's package and scripts are gone even though this download failed
    assert 'virtual-desktop-host-windows/Configure.ps1' not in left
    assert 'dcv-host-old.tar.gz' not in left


def test_another_instances_bootstrap_folder_is_emptied_first(tmp_path):
    code, calls, output, left = userdata(tmp_path, {'IDEA_TAR': '0'}, seed=FOREIGN)
    assert code == 0, output
    assert len(calls) == 3 and calls[0] == 'COPY idea/dcv-host-new.tar.gz', calls
    assert calls[1].startswith('TAR -xf ') and calls[1].endswith('dcv-host-new.tar.gz')
    assert calls[2] == 'INSTALL'
    assert left == ['dcv-host-new.tar.gz', 'instance-id']
    assert (tmp_path / BOOTSTRAP / 'instance-id').read_text().strip() == 'i-0new'


def test_this_instances_own_folder_is_kept(tmp_path):
    seed = {'instance-id': 'i-0new\n', 'log/ConfigureEVDI.log.1': 'this instance'}
    code, calls, output, left = userdata(tmp_path, {'IDEA_TAR': '0'}, seed=seed)
    assert code == 0, output
    assert 'log/ConfigureEVDI.log.1' in left and calls[-1] == 'INSTALL'


GPU_STUBS = r"""
function Get-ComputerInfo { [pscustomobject]@{ OsName = 'Microsoft Windows Server 2022 Datacenter' } }
function Get-PnpDevice { [CmdletBinding()] param($Class) if ($global:AmdActive) { [pscustomobject]@{ FriendlyName = 'AMD Radeon Pro V520' } } }
function Get-WmiObject { [CmdletBinding()] param($Class) }
function Invoke-WebRequest { [CmdletBinding()] param($Method, $Uri, [switch]$UseBasicParsing) [pscustomobject]@{ Headers = @{ 'x-amz-bucket-region' = 'us-east-1' } } }
function Expand-Archive { [CmdletBinding()] param($Path, $DestinationPath, [switch]$Force) Record "EXPAND $(Split-Path -Leaf $Path)" }
function pnputil { Record 'PNPUTIL'; $global:AmdActive = $env:IDEA_DEVICE -eq '1'; $global:LASTEXITCODE = [int]$env:IDEA_PNPUTIL }
function Assert-InstallerTrusted { param($Path, $Publisher) Record "TRUST $(Split-Path -Leaf $Path)" }
function reg { }
function Set-ExecutionPolicy { [CmdletBinding()] param($ExecutionPolicy, $Scope) }
function 7z { Record '7Z'; if ($env:IDEA_7Z -eq '0') { Set-Content 'C:\IDEA\Assets\nvidia_grid_driver\setup.exe' 'new' }; $global:LASTEXITCODE = [int]$env:IDEA_7Z }
function Start-Process { [CmdletBinding()] param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru) Record 'SETUP'; [pscustomobject]@{ ExitCode = 0 } }
function aws {
  $count = [int]$env:IDEA_ARCHIVES
  $folder = if ($args -contains 'C:\IDEA\Assets\nvidia_grid_driver') { 'C:\IDEA\Assets\nvidia_grid_driver' } else { 'C:\IDEA\Assets' }
  New-Item -ItemType Directory -Path $folder -Force | Out-Null
  for ($i = 1; $i -le $count; $i++) {
    $name = if ($folder -like '*nvidia*') { "580_grid_win_server2022_$i.exe" } else { "AMD_GPU_WINDOWS_2K22_$i.zip" }
    Set-Content "$folder\$name" 'driver'
    (Get-Item "$folder\$name").LastWriteTime = (Get-Date).AddMinutes($i)
  }
  Record "AWS $count" }
"""


def gpu(tmp_path, function, template, env, seed=()):
    configure = tmp_path / 'gpu.ps1'
    configure.write_text(
        'function Write-ToLog { }\n' + render(template, 'windows2022'), encoding='utf-8'
    )
    for name in seed:
        name = name.replace('C:/', 'cdrive/', 1)
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text('stale', encoding='utf-8')
    return windows_pwsh.run(
        tmp_path,
        configure,
        functions=[function],
        stubs=GPU_STUBS,
        action=function,
        env=env,
    )


@pytest.mark.parametrize(
    'archives, pnputil, device, expected',
    [
        (
            '0',
            '0',
            '0',
            [
                'LOG Warn: No AMD driver archive matching AMD_GPU_WINDOWS_2K22 was copied to C:\\IDEA\\Assets; AMD drivers are not installed'
            ],
        ),
        (
            '2',
            '0',
            '1',
            [
                'LOG Warn: Found 2 AMD driver archives matching AMD_GPU_WINDOWS_2K22; using the newest, AMD_GPU_WINDOWS_2K22_2.zip',
                'LOG Info: Extracting AMD drivers',
                'EXPAND AMD_GPU_WINDOWS_2K22_2.zip',
                'PNPUTIL',
                'LOG Info: AMD Grid driver installed (pnputil exit code 0), reboot will be done at the end of the bootstrap sequence',
            ],
        ),
        (
            '1',
            '5',
            '0',
            [
                'LOG Info: Extracting AMD drivers',
                'EXPAND AMD_GPU_WINDOWS_2K22_1.zip',
                'PNPUTIL',
                'LOG Warn: pnputil exited 5 and no AMD display device is active yet; if the display is still the basic adapter after the restart, the AMD driver did not install',
            ],
        ),
        (
            '1',
            '3010',
            '1',
            [
                'LOG Info: Extracting AMD drivers',
                'EXPAND AMD_GPU_WINDOWS_2K22_1.zip',
                'PNPUTIL',
                'LOG Info: AMD Grid driver installed (pnputil exit code 3010), reboot will be done at the end of the bootstrap sequence',
            ],
        ),
    ],
)
def test_the_amd_driver_step_reports_what_it_installed(
    tmp_path, archives, pnputil, device, expected
):
    # warnings, not stops: the bucket contents and pnputil's codes for this package are unconfirmed
    result, calls, output = gpu(
        tmp_path,
        'Install-AMDGpuDrivers',
        '_templates/windows/gpu/amd_drivers.jinja2',
        {'IDEA_ARCHIVES': archives, 'IDEA_PNPUTIL': pnputil, 'IDEA_DEVICE': device},
    )
    assert result == 0, output
    assert calls[-len(expected) :] == expected


@pytest.mark.parametrize('extract, setup', [('2', False), ('0', True)])
def test_the_nvidia_installer_runs_only_from_this_extraction(tmp_path, extract, setup):
    result, calls, output = gpu(
        tmp_path,
        'Install-NvidiaGpuDrivers',
        '_templates/windows/gpu/nvidia_drivers.jinja2',
        {'IDEA_ARCHIVES': '1', 'IDEA_7Z': extract},
        # an earlier copy and extraction left these behind
        seed=[
            'C:/IDEA/Assets/nvidia_grid_driver/setup.exe',
            'C:/IDEA/Assets/nvidia_grid_driver/old_grid_server2022_0.exe',
        ],
    )
    assert ('SETUP' in calls) == setup, output
    assert result == (0 if setup else 1), output
    trusted = [c for c in calls if c.startswith('TRUST ')]
    assert trusted == ['TRUST 580_grid_win_server2022_1.exe']


STORAGE_STUBS = r"""
function Get-PhysicalDisk {
  @([pscustomobject]@{ DeviceId = '0'; FriendlyName = 'NVMe Amazon Elastic B'; OperationalStatus = 'OK'; Size = 100GB },
    [pscustomobject]@{ DeviceId = '1'; FriendlyName = 'NVMe Amazon Elastic B'; OperationalStatus = 'OK'; Size = 200GB },
    [pscustomobject]@{ DeviceId = '2'; FriendlyName = 'NVMe Amazon EC2 NVMe'; OperationalStatus = 'OK'; Size = 300GB }) }
function Get-Disk { [CmdletBinding()] param($Number) [pscustomobject]@{ Number = $Number; PartitionStyle = $(if ("$Number" -eq '0') { 'GPT' } else { 'RAW' }) } }
function Get-Partition { [CmdletBinding()] param($DriveLetter) [pscustomobject]@{ DiskNumber = 0 } }
function Initialize-Disk { [CmdletBinding(SupportsShouldProcess)] param($Number, $PartitionStyle) Record "INITIALIZE $Number" }
function New-Partition { [CmdletBinding()] param($DiskNumber, [switch]$UseMaximumSize, [switch]$AssignDriveLetter) [pscustomobject]@{ DriveLetter = 'Z' } }
function Format-Volume { [CmdletBinding(SupportsShouldProcess)] param($DriveLetter, $FileSystem, $NewFileSystemLabel, [switch]$Force) Record "FORMAT $DriveLetter" }
function Get-Volume { [CmdletBinding()] param($DriveLetter) [pscustomobject]@{ Size = 300GB } }
"""


def test_only_a_blank_instance_store_disk_is_formatted(tmp_path):
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=['Configure-InstanceStorage'],
        stubs=STORAGE_STUBS,
        action='Configure-InstanceStorage',
    )
    assert code == 0, output
    assert [c for c in calls if not c.startswith('LOG')] == ['INITIALIZE 2', 'FORMAT Z']


def test_the_builder_log_check_reads_only_this_bakes_lines():
    # text: the check is part of the builder's top-level script
    setup = render('dcv-host-ami-builder-windows/Setup.ps1.jinja2', 'windows2022')
    start = setup.index(
        '$LogStart = if (Test-Path $LogFile) { @(Get-Content -LiteralPath $LogFile).Count } else { 0 }'
    )
    assert start < setup.index(
        'Install-WindowsEC2Instance -Update -ModuleVersion $Release'
    )
    assert setup.count('Where-Object { $_.LineNumber -gt $LogStart }') == 2


def builder_script(tmp_path):
    script = tmp_path / 'Setup.ps1'
    script.write_text(
        render('dcv-host-ami-builder-windows/Setup.ps1.jinja2', 'windows2022'),
        encoding='utf-8',
    )
    return script


@pytest.mark.parametrize('owners', [['alice-marker', 'bob-marker'], []])
def test_a_bake_refuses_a_source_that_holds_desktop_user_data(tmp_path, owners):
    for owner in owners:
        (tmp_path / 'cdrive' / 'session-storage' / owner).mkdir(parents=True)
    code, calls, output = windows_pwsh.run(
        tmp_path,
        builder_script(tmp_path),
        functions=['Assert-CleanImageSource'],
        action='try { Assert-CleanImageSource; Record "ACCEPTED" } catch { Record "REFUSED $($_.Exception.Message)" }',
    )
    assert code == 0, output
    if owners:
        assert calls == [
            'REFUSED The source image holds desktop user data in C:\\session-storage '
            '(alice-marker, bob-marker). Build from an AMI that was never used as a desktop, or '
            'on the source instance delete C:\\session-storage\\<owner> and C:\\Users\\<owner> '
            'for each listed owner and create a new source AMI.'
        ]
    else:
        assert calls == ['ACCEPTED']


def test_the_source_check_runs_before_anything_is_installed():
    # text: the order is in the builder's top-level script
    setup = render('dcv-host-ami-builder-windows/Setup.ps1.jinja2', 'windows2022')
    body = setup[setup.index('\ntry {\n') :]
    assert body.index('Assert-CleanImageSource') < body.index(
        'Install-WindowsEC2Instance -Update'
    )


FINALIZE_STUBS = r"""
function Invoke-RestMethod { [CmdletBinding()] param($Method, $Uri, $Headers) if ($Uri -like '*token') { 'token' } else { 'i-0builder' } }
function New-EC2Tag { [CmdletBinding()] param($Resource, $Tag, $Region) Record "TAG $($Tag.Value)" }
"""


@pytest.mark.parametrize('saved', [False, True])
def test_finalize_ends_the_wait_when_the_saved_checks_cannot_be_read(tmp_path, saved):
    script = builder_script(tmp_path)
    state = tmp_path / 'cdrive' / 'ProgramData' / 'IDEA'
    state.mkdir(parents=True)
    if saved:
        release = next(
            line.split("'")[1]
            for line in script.read_text().splitlines()
            if line.startswith('$Release = ')
        )
        (state / 'image-checks.json').write_text(
            '{"release": "%s", "checks": [{"name": "bootstrap", "ok": true}]}' % release
        )
        (state / 'baked-release').write_text(release)
    code, calls, output = windows_pwsh.run(
        tmp_path,
        script,
        stubs=FINALIZE_STUBS,
        action=f'try {{ & \'{script}\' -Finalize }} catch {{ Record "THROWN $($_.Exception.Message)" }}',
    )
    if saved:
        # positive control: readable checks go on to generalize, with no finalize failure tag
        assert 'TAG failed:finalize' not in calls, output
    else:
        assert calls[0] == 'TAG failed:finalize', output
        assert calls[-1].startswith(
            'THROWN Refusing to generalize: the saved image checks cannot be read'
        )


LOG = Path('cdrive/ProgramData/Amazon/EC2-Windows/Launch/Log/UserdataExecutionIDEA.log')


INHERITED_LOGS = {
    'log/ConfigureEVDI.log.20251101T0900000000Z': 'previous release transcript',
    'log/InstallEVDI.log.20251101T0850000000Z': 'previous release transcript',
}


def seed_bootstrap_log(tmp_path, text):
    (tmp_path / LOG).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / LOG).write_text(text, encoding='utf-8')


def test_inherited_logs_and_transcripts_are_removed(tmp_path):
    seed_bootstrap_log(tmp_path, 'previous instance log line\n')
    code, calls, output, left = userdata(
        tmp_path, {'IDEA_TAR': '0'}, seed=dict(FOREIGN, **INHERITED_LOGS)
    )
    assert code == 0, output
    assert calls[-1] == 'INSTALL' and not any(c.startswith('ACL ') for c in calls)
    assert left == ['dcv-host-new.tar.gz', 'instance-id']
    # the IDEA bootstrap log of the earlier instance is gone, and nothing was logged about it
    assert not (tmp_path / LOG).exists()


def test_an_inherited_log_held_open_is_limited_to_administrators_and_reported(tmp_path):
    seed_bootstrap_log(tmp_path, 'previous instance log line\n')
    held = 'ConfigureEVDI.log.20251101T0900000000Z'
    code, calls, output, left = userdata(
        tmp_path,
        {'IDEA_TAR': '0', 'IDEA_HELD': held},
        seed=dict(FOREIGN, **INHERITED_LOGS),
    )
    assert code == 0, output
    assert calls[-1] == 'INSTALL'
    assert [c for c in calls if c.startswith('ACL ')] == [
        f'ACL {held} protected=True inherited=False S-1-5-18:FullControl:Allow S-1-5-32-544:FullControl:Allow'
    ]
    assert left == ['dcv-host-new.tar.gz', 'instance-id', 'log', f'log/{held}']
    log = (tmp_path / LOG).read_text()
    assert 'previous instance log line' not in log
    assert (
        'WARNING: could not remove ' in log
        and f'{held}, left by another instance; access is limited to SYSTEM and Administrators'
        in log
    )


def test_an_instances_own_logs_are_left_alone(tmp_path):
    seed_bootstrap_log(tmp_path, 'this instance log line\n')
    seed = dict(INHERITED_LOGS, **{'instance-id': 'i-0new\n'})
    code, calls, output, left = userdata(tmp_path, {'IDEA_TAR': '0'}, seed=seed)
    assert code == 0, output
    assert not any(c.startswith('ACL ') for c in calls)
    assert all(f in left for f in INHERITED_LOGS)
    assert (tmp_path / LOG).read_text() == 'this instance log line\n'


def test_an_old_package_that_cannot_be_removed_stops_with_a_status(tmp_path):
    code, calls, output, left = userdata(
        tmp_path,
        {'IDEA_TAR': '0', 'IDEA_HELD': 'virtual-desktop-host-windows'},
        seed=FOREIGN,
    )
    assert code == 1, output
    assert 'INSTALL' not in calls
    assert calls[-1] == 'TAG idea:BootstrapStatus=bootstrap-download-failed'
    assert (
        'ERROR: IDEA bootstrap package download failed: could not remove'
        in (tmp_path / LOG).read_text()
    )


def test_an_empty_instance_id_file_is_treated_as_another_instances(tmp_path):
    seed = dict(FOREIGN, **{'instance-id': ''})
    code, calls, output, left = userdata(tmp_path, {'IDEA_TAR': '0'}, seed=seed)
    assert code == 0, output
    assert left == ['dcv-host-new.tar.gz', 'instance-id']


def test_a_bootstrap_log_that_can_be_neither_removed_nor_written_never_stops_the_bootstrap(
    tmp_path,
):
    # the inherited IDEA log is held open and every write to it fails: the warning about it
    # cannot be logged either, and the bootstrap still goes on to the download and install
    seed_bootstrap_log(tmp_path, 'previous instance log line\n')
    code, calls, output, left = userdata(
        tmp_path,
        {
            'IDEA_TAR': '0',
            'IDEA_HELD': 'UserdataExecutionIDEA.log',
            'IDEA_NO_WRITE': '1',
        },
        seed=dict(FOREIGN, **INHERITED_LOGS),
    )
    assert code == 0, output
    assert calls[-1] == 'INSTALL'
    assert 'WARNING could not remove' in output
    assert left == ['dcv-host-new.tar.gz', 'instance-id']
