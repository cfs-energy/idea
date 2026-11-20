"""
A Windows desktop's bootstrap package is built on the controller and stored where other hosts can
reach it, so it carries no secret: the local passwords are made on the desktop at first boot. The
bootstrap logs and transcripts never hold the directory join password or the key that fetches it,
the transcript resumes on every path, a failed account or join step stops the bootstrap before the
desktop is configured, and the bootstrap folders are for SYSTEM and Administrators.

The behaviour tests run the rendered functions under pwsh with the Windows cmdlets stubbed. Text
assertions remain only where nothing can run off Windows: the package content itself, the random
generator the password comes from, the LSA call, and every join log line in the template.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

import test_bootstrap_shell_syntax as shell
import windows_pwsh
from test_bootstrap_shell_syntax import IDEA_BOOTSTRAP_DIR, PWSH
from test_image_bake_bootstrap import WINDOWS, render

CONFIGURE = 'virtual-desktop-host-windows/Configure.ps1.jinja2'
JOIN = Path(IDEA_BOOTSTRAP_DIR, '_templates/windows/join_activedirectory.jinja2')
INSTALL = Path(IDEA_BOOTSTRAP_DIR, 'virtual-desktop-host-windows/Install.ps1')
needs_pwsh = windows_pwsh.needs_pwsh

# a stubbed account step records what it was given; the password is compared, never recorded
ACCOUNT_STUBS = r"""
function New-LocalPassword { param($UserName) "Marker-Pass-$UserName-1!" }
function Get-LocalUser { [CmdletBinding()] param($Name)
  # the built-in administrator is found by SID; IDEA_ADMIN renames it, IDEA_ADMIN=none removes it
  $admin = if ($env:IDEA_ADMIN) { $env:IDEA_ADMIN } else { 'Administrator' }
  if (-not $Name -and $admin -ne 'none') {
    [pscustomobject]@{ Name = 'Guest'; SID = 'S-1-5-21-1-2-3-501' }
    [pscustomobject]@{ Name = $admin; SID = 'S-1-5-21-1-2-3-500' } } }
function New-LocalUser { [CmdletBinding()] param($Name, $Password, [switch]$PasswordNeverExpires, [switch]$AccountNeverExpires)
  $global:OwnerPassword = [Net.NetworkCredential]::new('', $Password).Password; Record "ACCOUNT new $Name" }
function Set-LocalUser { [CmdletBinding()] param([Parameter(ValueFromPipeline)] $InputObject, $Name, $Password)
  process { $who = if ($Name) { $Name } else { $InputObject.Name }; Record "ACCOUNT set $who" } }
function Add-LocalGroupMember { [CmdletBinding()] param($SID, $Member)
  if ($env:IDEA_MEMBER -eq 'exists') { Write-Error -Message 'already a member' -ErrorId 'MemberExists' }
  else { Record "GROUP add $Member to $SID" } }
function Remove-ItemProperty { [CmdletBinding()] param($Path, $Name) Record "REG remove $Name" }
function New-ItemProperty { [CmdletBinding()] param($Path, $Name, $PropertyType, $Value, [switch]$Force) Record "REG set $Name $PropertyType $Value" }
function Set-AutoLogonPassword { param($Password) Record "LSA store matches-account=$($Password -ceq $global:OwnerPassword)" }
"""

# the steps of Configure-WindowsEC2Instance up to the first one after the account or join step,
# which records NEXT STEP and ends the run with exit code 7
FLOW_STUBS = r"""
function Invoke-RestMethod { [CmdletBinding()] param($Headers, $Method, $Uri)
  if ($Uri -like '*api/token') { 'token' } elseif ($Uri -like '*instance-type') { 'm7i.large' } else { 'i-0marker' } }
function Test-Path { [CmdletBinding()] param($Path) $false }
function Get-IdeaHostname { $env:COMPUTERNAME }
function Start-Sleep { [CmdletBinding()] param($Seconds) }
function Remove-Item { [CmdletBinding()] param($Path, [switch]$Force) Record "REMOVE $Path" }
function Install-OwnerPasswordScript { Record 'OWNER script installed' }
function Get-WmiObject { [CmdletBinding()] param($Class) [pscustomobject]@{ PartOfDomain = $false } }
function Set-ItemProperty { [CmdletBinding()] param($Path, $Name, $Value, [switch]$Force) Record 'NEXT STEP'; exit 7 }
"""


def body(script, name):
    start = script.index(f'function {name} {{')
    end = re.compile(r'^function ', re.M).search(script, start + 1)
    return script[start : end.start() if end else len(script)]


def active_directory(monkeypatch):
    class ActiveDirectory(shell.Config):
        def __init__(self):
            super().__init__()
            self.put('directoryservice.provider', 'aws_managed_activedirectory')
            for key, value in {
                'ad_short_name': 'EXAMPLE',
                'name': 'example.invalid',
                'ad_automation.sqs_queue_url': 'https://sqs.example.invalid/ad',
            }.items():
                self.put(f'directoryservice.{key}', value)

    monkeypatch.setattr(shell, 'Config', ActiveDirectory)


# text: the package is the artifact other hosts can read, so its content is what is checked
@pytest.mark.parametrize('base_os', WINDOWS)
def test_the_local_desktop_package_carries_no_password(base_os):
    # a password chosen at render time made every render differ; the package now renders the same
    first, second = render(CONFIGURE, base_os), render(CONFIGURE, base_os)
    assert first == second
    assert '-Name "DefaultPassword" -PropertyType' not in first
    assert 'LOCAL_ADMINS_PASSWORD.txt" -Value' not in first
    assert 'Set-IdeaLocalAccounts -LocalUser $LocalUser' in first


@needs_pwsh
@pytest.mark.parametrize(
    'env, group, admin, done',
    [
        (
            {},
            [
                'GROUP add owner to S-1-5-32-544',
                'LOG Info: Added owner to the Administrators group',
            ],
            ['ACCOUNT set Administrator'],
            'and Administrator',
        ),
        # a renamed or localized built-in administrator, an owner already in the group, and an
        # image with no RID 500 account are all ordinary images, not failures
        (
            {'IDEA_ADMIN': 'Administrateur'},
            [
                'GROUP add owner to S-1-5-32-544',
                'LOG Info: Added owner to the Administrators group',
            ],
            ['ACCOUNT set Administrateur'],
            'and Administrateur',
        ),
        (
            {'IDEA_MEMBER': 'exists'},
            [],
            ['ACCOUNT set Administrator'],
            'and Administrator',
        ),
        (
            {'IDEA_ADMIN': 'none'},
            [
                'GROUP add owner to S-1-5-32-544',
                'LOG Info: Added owner to the Administrators group',
            ],
            [
                'LOG Warn: This image has no built-in administrator account (RID 500); no administrator password was changed'
            ],
            '',
        ),
    ],
)
def test_local_accounts_are_set_with_the_transcript_off(
    tmp_path, env, group, admin, done
):
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=['Set-IdeaLocalAccounts'],
        stubs=ACCOUNT_STUBS,
        action="Set-IdeaLocalAccounts -LocalUser 'owner' -TranscriptPath 'transcript.log'\nRecord 'AFTER'",
        env=env,
    )
    assert code == 0, output
    assert calls == [
        'TRANSCRIPT stop',
        'ACCOUNT new owner',
        *group,
        'REG remove DefaultPassword',
        'LSA store matches-account=True',
        'REG set AutoAdminLogon DWord 1',
        'REG set DefaultUserName String owner',
        *admin,
        'TRANSCRIPT start',
        f'LOG Info: Set first-boot passwords for owner (auto-logon){" " + done if done else ""}',
        'AFTER',
    ]
    assert 'Marker-Pass' not in '\n'.join(calls) + output


@needs_pwsh
@pytest.mark.parametrize(
    'failing, stub',
    [
        (
            'the account',
            "function New-LocalUser { [CmdletBinding()] param($Name, $Password, [switch]$PasswordNeverExpires, [switch]$AccountNeverExpires) throw 'marker account failure' }",
        ),
        (
            'the logon secret',
            "function Set-AutoLogonPassword { param($Password) throw 'marker account failure' }",
        ),
        (
            'the Administrator',
            "function Set-LocalUser { [CmdletBinding()] param([Parameter(ValueFromPipeline)] $InputObject, $Name, $Password) process { Write-Error 'marker account failure' } }",
        ),
    ],
)
def test_a_failed_account_step_is_logged_and_stops_before_the_desktop_is_configured(
    tmp_path, failing, stub
):
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=['Configure-WindowsEC2Instance', 'Set-IdeaLocalAccounts'],
        stubs=ACCOUNT_STUBS + FLOW_STUBS + stub,
        action='Configure-WindowsEC2Instance',
    )
    assert code == 1, f'{failing}: {output}'
    error = [c for c in calls if c.startswith('LOG Error: ')]
    assert error == [
        'LOG Error: Local account setup for sample-user stopped with an error, '
        'the desktop is not configured: marker account failure'
    ]
    # the transcript is back on before the error is written, and nothing after the step runs
    stop = calls.index('TRANSCRIPT stop')
    assert stop < calls.index('TRANSCRIPT start', stop) < calls.index(error[0])
    assert 'NEXT STEP' not in calls and 'OWNER script installed' not in calls
    assert not any(p.name.endswith('configured-i-0marker') for p in tmp_path.rglob('*'))
    assert 'Marker-Pass' not in '\n'.join(calls) + output
    if failing == 'the logon secret':
        assert not any(c.startswith('REG set AutoAdminLogon') for c in calls)


@needs_pwsh
def test_the_local_flow_continues_after_the_account_step(tmp_path):
    # positive control for the test above: the same run without a failure reaches the next step
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=['Configure-WindowsEC2Instance', 'Set-IdeaLocalAccounts'],
        stubs=ACCOUNT_STUBS + FLOW_STUBS,
        action='Configure-WindowsEC2Instance',
    )
    assert code == 7, output
    assert calls[0] == 'TRANSCRIPT start'
    assert calls[-2:] == ['OWNER script installed', 'NEXT STEP']
    # the values earlier releases left are removed before the account step
    removed = calls.index('REMOVE C:\\LOCAL_ADMINS_PASSWORD.txt')
    assert (
        calls.index('REG remove DefaultPassword')
        < removed
        < calls.index('TRANSCRIPT stop')
    )
    assert [c for c in calls if c.startswith('TRANSCRIPT')] == [
        'TRANSCRIPT start',
        'TRANSCRIPT stop',
        'TRANSCRIPT start',
    ]


JOIN_STUBS = r"""
function Get-IdeaHostname { 'IDEA-MARKER' }
function Start-ADAutomationAuthorization { param($Hostname) Record "JOIN authorize $Hostname" }
function Start-ADAutomationWaitForAuthorizationAndJoin { Record 'JOIN join'; $global:IdeaDomainJoined = $true }
function Set-ItemProperty { [CmdletBinding()] param($Path, $Name, $Value, [switch]$Force) Record 'NEXT STEP'; exit 7 }
"""


@needs_pwsh
@pytest.mark.parametrize('fails', [False, True])
def test_the_directory_join_runs_with_the_transcript_off(tmp_path, monkeypatch, fails):
    active_directory(monkeypatch)
    failure = (
        "function Start-ADAutomationWaitForAuthorizationAndJoin { throw 'marker join failure' }"
        if fails
        else ''
    )
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=['Configure-WindowsEC2Instance', 'Join-IdeaDirectory'],
        stubs=ACCOUNT_STUBS + FLOW_STUBS + JOIN_STUBS + failure,
        action='Configure-WindowsEC2Instance',
    )
    transcript = [c for c in calls if c.startswith('TRANSCRIPT')]
    assert transcript == ['TRANSCRIPT start', 'TRANSCRIPT stop', 'TRANSCRIPT start'], (
        output
    )
    stop, resume = (
        calls.index('TRANSCRIPT stop'),
        len(calls) - calls[::-1].index('TRANSCRIPT start') - 1,
    )
    authorize = next(i for i, c in enumerate(calls) if c.startswith('JOIN authorize'))
    assert stop < authorize < resume
    if fails:
        assert code == 1, output
        assert calls[-1] == (
            'LOG Error: [Join AD] the directory join stopped with an error, '
            'the desktop is not configured: marker join failure'
        )
        assert 'NEXT STEP' not in calls
    else:
        assert code == 7, output
        assert calls.index('JOIN join') < resume
        assert calls[-3:] == [
            'REG set AutoAdminLogon DWord 1',
            'REG set DefaultUserName String EXAMPLE\\sample-user',
            'NEXT STEP',
        ]


@needs_pwsh
def test_a_configured_instance_ends_the_transcript_and_exits(tmp_path):
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=['Configure-WindowsEC2Instance'],
        stubs=FLOW_STUBS
        + 'function Test-Path { [CmdletBinding()] param($Path) $Path -like "*configured-i-0marker" }',
        action='Configure-WindowsEC2Instance',
    )
    assert code == 0, output
    assert calls == [
        'TRANSCRIPT start',
        'LOG Info: Instance i-0marker is already configured; skipping the repeated user data run',
        'TRANSCRIPT stop',
    ]


@needs_pwsh
def test_a_rename_ends_the_transcript_before_the_restart(tmp_path):
    code, calls, output = windows_pwsh.run(
        tmp_path,
        windows_pwsh.configure(tmp_path),
        functions=['Configure-WindowsEC2Instance'],
        stubs=FLOW_STUBS
        + 'function Get-IdeaHostname { "IDEA-MARKER" }\n'
        + 'function Rename-Computer { [CmdletBinding()] param($NewName, [switch]$Force) Record "RENAME $NewName" }\n'
        + 'function Restart-Computer { [CmdletBinding()] param([switch]$Force) Record "RESTART" }\n',
        action='Configure-WindowsEC2Instance',
    )
    assert code == 0, output
    assert calls[0] == 'TRANSCRIPT start' and 'RENAME IDEA-MARKER' in calls
    assert calls[-2:] == ['TRANSCRIPT stop', 'RESTART']


# text: the source of the randomness and the LSA call cannot be observed off Windows
def test_the_password_comes_from_the_os_random_generator_and_meets_the_policy():
    script = render(CONFIGURE, 'windows2022')
    make = body(script, 'New-LocalPassword')
    assert '[System.Security.Cryptography.RandomNumberGenerator]::Create()' in make
    assert 'Get-Random' not in make
    store = body(script, 'Set-AutoLogonPassword')
    assert 'LsaStorePrivateData' in store
    assert "[IdeaLsaSecret]::Store('DefaultPassword', $Password)" in store


@needs_pwsh
def test_generated_passwords_differ_and_meet_the_policy(tmp_path):
    script = tmp_path / 'make.ps1'
    script.write_text(
        body(render(CONFIGURE, 'windows2022'), 'New-LocalPassword')
        + '\n1..40 | ForEach-Object { New-LocalPassword -UserName "abc" }\n'
    )
    windows_pwsh.require()
    out = subprocess.run(
        [PWSH, '-NoProfile', '-File', str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert len(out) == 40 and len(set(out)) == 40
    for password in out:
        assert len(password) == 24
        assert re.search('[A-Z]', password) and re.search('[a-z]', password)
        assert re.search('[0-9]', password) and re.search('[^A-Za-z0-9]', password)
        assert 'abc' not in password.lower()


# text: every log line in the template, including branches the executed tests do not reach
def test_join_logging_never_holds_the_join_password_or_its_key():
    for line in JOIN.read_text().splitlines():
        if 'Write-ToLog' in line:
            assert 'otp' not in line.lower(), line
            assert '$AD_AUTHORIZATION_NONCE' not in line, line
            assert '$response' not in line, line


OWNER_STUBS = r"""
function New-Object { [CmdletBinding()] param($TypeName, $ArgumentList, $ComObject)
  $shortcut = [pscustomobject]@{ TargetPath = $null; Arguments = $null }
  $shortcut | Add-Member ScriptMethod Save { Record "SHORTCUT $($this.TargetPath) $($this.Arguments)" }
  $shell = [pscustomobject]@{ Shortcut = $shortcut }
  $shell | Add-Member ScriptMethod CreateShortcut { param($Path) Record "SHORTCUT at $Path"; $this.Shortcut }
  $shell }
function Read-Host { param($Prompt, [switch]$AsSecureString)
  $global:Reads++; $value = if ($global:Reads -eq 2 -and $env:IDEA_SECOND) { $env:IDEA_SECOND } else { 'Marker-Owner-1!' }
  ConvertTo-SecureString $value -AsPlainText -Force }
function Set-LocalUser { [CmdletBinding()] param($Name, $Password) Record "ACCOUNT set $Name typed=$([Net.NetworkCredential]::new('', $Password).Password -ceq 'Marker-Owner-1!')" }
function Get-ItemProperty { [CmdletBinding()] param($Path, $Name) [pscustomobject]@{ DefaultUserName = $env:IDEA_AUTOLOGON } }
function Set-AutoLogonPassword { param($Password) Record "LSA store typed=$($Password -ceq 'Marker-Owner-1!')" }
function Write-Host { param($Object) Record "SAY $Object" }
"""


@needs_pwsh
@pytest.mark.parametrize(
    'case, env, expected',
    [
        (
            'the auto-logon owner',
            {'IDEA_AUTOLOGON': 'owner'},
            ['ACCOUNT set owner typed=True', 'LSA store typed=True'],
        ),
        (
            'another administrator',
            {'IDEA_AUTOLOGON': 'someone-else'},
            ['ACCOUNT set owner typed=True'],
        ),
        (
            'a mistyped second entry',
            {'IDEA_AUTOLOGON': 'owner', 'IDEA_SECOND': 'Other-1!'},
            [],
        ),
    ],
)
def test_the_owner_sets_their_own_password_from_the_session(
    tmp_path, case, env, expected
):
    configure = windows_pwsh.configure(tmp_path)
    code, calls, output = windows_pwsh.run(
        tmp_path,
        configure,
        functions=['Install-OwnerPasswordScript', 'Set-AutoLogonPassword'],
        stubs=OWNER_STUBS.replace(
            'function Set-AutoLogonPassword', 'function Unused-Stub'
        ),
        action='Install-OwnerPasswordScript',
    )
    assert code == 0, output
    written = next(tmp_path.glob('*Set-MyWindowsPassword.ps1'))
    assert calls[-1].startswith(
        'SHORTCUT powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell.exe -Verb RunAs'
    )
    assert calls[-1].endswith(
        '-File C:\\IDEA\\LocalScripts\\Set-MyWindowsPassword.ps1\'"'
    )
    content = written.read_text(encoding='utf-8')
    # the script carries the same LSA function the bootstrap uses, then the owner's steps
    assert content.startswith('function Set-AutoLogonPassword {')
    assert "[IdeaLsaSecret]::Store('DefaultPassword', $Password)" in content
    steps = tmp_path / 'steps.ps1'
    steps.write_text(
        content[content.index("$ErrorActionPreference = 'Stop'") :], encoding='utf-8'
    )
    code, calls, output = windows_pwsh.run(
        tmp_path,
        steps,
        stubs=OWNER_STUBS,
        action=f"& '{steps}'; exit $LASTEXITCODE",
        env={'USERNAME': 'owner', **env},
    )
    assert [c for c in calls if not c.startswith('SAY ')] == expected, (
        f'{case}: {output}'
    )
    assert code == (1 if not expected else 0), output
    assert 'Marker-Owner' not in '\n'.join(calls)


PROTECT_STUBS = r"""
function New-Item { [CmdletBinding()] param($Path, $ItemType, [switch]$Force) }
function New-Object { [CmdletBinding()] param([string]$TypeName, [object[]]$ArgumentList)
  switch ($TypeName) {
    'System.Security.AccessControl.DirectorySecurity' {
      $acl = [pscustomobject]@{ Protected = $null; PreserveInherited = $null; Rules = [System.Collections.ArrayList]::new() }
      $acl | Add-Member ScriptMethod SetAccessRuleProtection { param($protect, $preserve) $this.Protected = $protect; $this.PreserveInherited = $preserve }
      $acl | Add-Member ScriptMethod AddAccessRule { param($rule) [void]$this.Rules.Add($rule) }
      $acl }
    'System.Security.Principal.SecurityIdentifier' { "$($ArgumentList[0])" }
    'System.Security.AccessControl.FileSystemAccessRule' {
      [pscustomobject]@{ Sid = $ArgumentList[0]; Rights = $ArgumentList[1]; Inheritance = $ArgumentList[2]; Propagation = $ArgumentList[3]; Type = $ArgumentList[4] } }
    default { throw "unexpected type $TypeName" }
  } }
function Set-Acl { [CmdletBinding()] param($LiteralPath, $AclObject)
  Record ("ACL " + (@{ path = $LiteralPath; protected = $AclObject.Protected; preserve = $AclObject.PreserveInherited; rules = @($AclObject.Rules) } | ConvertTo-Json -Compress -Depth 4)) }
"""


@needs_pwsh
def test_bootstrap_folders_grant_only_system_and_administrators(tmp_path):
    code, calls, output = windows_pwsh.run(
        tmp_path,
        INSTALL,
        functions=['Protect-IdeaDirectory', 'Protect-IdeaBootstrapDirectories'],
        stubs=PROTECT_STUBS,
        action='Protect-IdeaBootstrapDirectories',
        env={'SystemDrive': 'C:'},
    )
    assert code == 0, output
    acls = {a['path']: a for a in (json.loads(c[4:]) for c in calls)}
    full = [('S-1-5-18', 'FullControl'), ('S-1-5-32-544', 'FullControl')]
    expected = {
        'C:\\ProgramData\\Amazon\\EC2-Windows\\Launch\\Log': full,
        'C:\\Users\\Administrator\\IDEA\\bootstrap': full,
        'C:\\IDEA': full + [('S-1-5-32-545', 'ReadAndExecute')],
    }
    assert set(acls) == set(expected)
    for path, grants in expected.items():
        acl = acls[path]
        # inheritance from the parent is cut and nothing inherited is kept
        assert acl['protected'] is True and acl['preserve'] is False, path
        assert [(r['Sid'], r['Rights']) for r in acl['rules']] == grants, path
        for rule in acl['rules']:
            assert rule['Inheritance'] == 'ContainerInherit, ObjectInherit'
            assert (rule['Propagation'], rule['Type']) == ('None', 'Allow')


@needs_pwsh
def test_bootstrap_folders_are_protected_before_the_first_log_line(tmp_path):
    code, calls, output = windows_pwsh.run(
        tmp_path,
        INSTALL,
        functions=['Install-WindowsEC2Instance'],
        stubs='function Protect-IdeaBootstrapDirectories { Record "PROTECT" }\n'
        'function Test-Path { [CmdletBinding()] param($Path) $false }\n'
        'function Write-ToLog { param($Message, $Level) Record "LOG $Message"; exit 3 }\n'
        'function Start-Transcript { Record "TRANSCRIPT start"; exit 3 }\n',
        action="Install-WindowsEC2Instance -ModuleVersion 'marker-release'",
    )
    assert code == 3, output
    assert calls == ['PROTECT', 'LOG Installing Windows EC2 Instance']


def test_the_folder_the_log_writes_to_is_one_that_is_protected():
    # text: $LogFile is a script-level path, and the protected list must name its folder
    install = INSTALL.read_text()
    log_dir = re.search(
        r"\$LogFile = \('(\{0\}\\[^']+)\\UserdataExecutionIDEA\.log'", install
    ).group(1)
    folders = body(install, 'Protect-IdeaBootstrapDirectories')
    assert f"('{log_dir}' -f" in folders
    configure = render(CONFIGURE, 'windows2022')
    assert '\\Users\\Administrator\\IDEA\\bootstrap\\log\\' in configure
