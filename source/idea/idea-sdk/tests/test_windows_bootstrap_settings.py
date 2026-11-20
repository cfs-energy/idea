"""
Settings rendered into a Windows desktop's bootstrap are data: each lands in PowerShell as a
single-quoted literal, and shared storage reaches the logon batch file as double-quoted arguments.
A share whose drive or path holds a character the batch file cannot carry is left out and logged.
"""

import json
import subprocess
from pathlib import Path

import pytest

import test_bootstrap_shell_syntax as shell
from test_bootstrap_shell_syntax import PWSH

import windows_pwsh  # noqa: E402

pytestmark = windows_pwsh.needs_pwsh

# harmless markers: a space, an ASCII and a typographic single quote, and an ampersand
SHARE = "team share 'a' & b" + chr(0x2019) + 's'  # typographic quote
SERVER = "fs one & 'two'.example.invalid"
PREFIX = "IDEA 'x' & "
SHORT_NAME = "EX 'a' & b"
DOMAIN = "corp 'a' & b.example.invalid"


def ontap(drive, share, server=SERVER):
    return {
        'provider': 'fsx_netapp_ontap',
        'mount_drive': drive,
        'fsx_netapp_ontap': {
            'svm': {'smb_dns': server},
            'volume': {'cifs_share_name': share},
        },
    }


def configured(monkeypatch, refused=None, shares=None):
    if shares is None:
        shares = {
            'marker_ontap': ontap('P', SHARE),
            'marker_fsxw': {
                'provider': 'fsx_windows_file_server',
                'mount_drive': 'Q',
                'fsx_windows_file_server': {'dns': refused or SERVER},
            },
        }

    class Settings(shell.Config):
        def __init__(self):
            super().__init__()
            self.put('directoryservice.provider', 'aws_managed_activedirectory')
            self.put('directoryservice.name', DOMAIN)
            self.put('directoryservice.ad_short_name', SHORT_NAME)
            self.put('directoryservice.ad_automation.hostname_prefix', PREFIX)
            self.put(
                'directoryservice.ad_automation.sqs_queue_url',
                'https://sqs.example.invalid/ad',
            )
            for name, storage in shares.items():
                self.put(f'shared-storage.{name}', storage)

    monkeypatch.setattr(shell, 'Config', Settings)


def configure_script(tmp_path):
    _, component, variables = shell.packages('windows2022')[0]
    scripts = shell.render(tmp_path, 'windows2022', 'm7i.large', component, variables)
    return Path(next(f for f in scripts if f.endswith('Configure.ps1')))


def pwsh(tmp_path, script):
    windows_pwsh.require()
    path = tmp_path / 'probe.ps1'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run(
        [PWSH, '-NoProfile', '-NonInteractive', '-File', str(path)],
        capture_output=True,
        text=True,
        encoding='utf-8',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def string_assignments(tmp_path, configure):
    # the parser's view of each setting: a constant string, never an expandable one
    out = pwsh(
        tmp_path,
        '$errors = $null\n'
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{configure}', [ref]$null, [ref]$errors)\n"
        'if ($errors) { $errors | ForEach-Object { $_.Message }; exit 1 }\n'
        '$found = @{}\n'
        '$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true) |\n'
        '  ForEach-Object {\n'
        '    $value = $_.Right.Expression\n'
        '    if ($value -is [System.Management.Automation.Language.StringConstantExpressionAst]) {\n'
        '      $found[$_.Left.Extent.Text] = $value.Value\n'
        '    }\n'
        '  }\n'
        '[Console]::OutputEncoding = [Text.Encoding]::UTF8\n'
        '$found | ConvertTo-Json -Compress\n',
    )
    return json.loads(out)


MOUNT_STUBS = r"""
function New-Item { [CmdletBinding()] param($Path, $ItemType, $Value, [switch]$Force)
  foreach ($line in $Value -split "`n") { Record "BATCH $line" } }
function New-ScheduledTaskAction { [CmdletBinding()] param($Execute) 'action' }
function New-ScheduledTaskTrigger { [CmdletBinding()] param([switch]$AtLogOn, $User) 'trigger' }
function Register-ScheduledTask { [CmdletBinding()] param($Action, $Trigger, $TaskName, $Description, $User, [switch]$Force)
  Record "TASK register $TaskName user=$User force=$Force" }
function Unregister-ScheduledTask { [CmdletBinding(SupportsShouldProcess)] param($TaskName) Record "TASK unregister $TaskName" }
"""


def mount_run(tmp_path, configure):
    """runs the rendered Mount-SharedStorage; returns the final batch lines and the other calls"""
    import windows_pwsh

    code, calls, output = windows_pwsh.run(
        tmp_path,
        configure,
        functions=['Mount-SharedStorage'],
        stubs=MOUNT_STUBS,
        action='Mount-SharedStorage -DomainUserName sample-user',
    )
    assert code == 0, output
    batch = [c[6:] for c in calls if c.startswith('BATCH ')]
    return batch, [c for c in calls if not c.startswith('BATCH ')]


def test_settings_reach_powershell_as_text(tmp_path, monkeypatch):
    configured(monkeypatch)
    found = string_assignments(tmp_path, configure_script(tmp_path))
    assert found['$HOSTNAME_PREFIX'] == PREFIX
    assert found['$AD_DOMAIN_NAME'] == DOMAIN
    assert found['$AD_SHORT_NAME'] == SHORT_NAME


def test_shared_storage_reaches_the_batch_file_intact(tmp_path, monkeypatch):
    configured(monkeypatch)
    lines, calls = mount_run(tmp_path, configure_script(tmp_path))
    assert lines == [
        f'if not exist "P:" (net use "P:" "\\\\{SERVER}\\{SHARE}" /persistent:yes)',
        f'if not exist "Q:" (net use "Q:" "\\\\{SERVER}\\share" /persistent:yes)',
    ]
    assert calls == ['TASK register MountSharedStorage user=sample-user force=True']


def test_each_kind_of_share_reaches_the_logon_batch_file(tmp_path, monkeypatch):
    # a plain share, one with a space, the documented per-user home share (stored with doubled
    # backslashes, as some clusters have it) and a share with any other percent sign
    configured(
        monkeypatch,
        shares={
            'apps': ontap('A', 'Apps', 'SVM.EXAMPLE'),
            'team': ontap('T', 'team share', 'SVM.EXAMPLE'),
            'home': ontap('H', 'Users$\\\\home\\\\%UserName%', 'SVM.EXAMPLE'),
            'other': ontap('O', 'home\\%OTHER%', 'SVM.EXAMPLE'),
        },
    )
    lines, calls = mount_run(tmp_path, configure_script(tmp_path))
    assert lines == [
        'if not exist "A:" (net use "A:" "\\\\SVM.EXAMPLE\\Apps" /persistent:yes)',
        'if not exist "T:" (net use "T:" "\\\\SVM.EXAMPLE\\team share" /persistent:yes)',
        'if not exist "H:" (net use "H:" "\\\\SVM.EXAMPLE\\Users$\\home\\%UserName%" /persistent:yes)',
    ]
    assert calls == [
        'LOG Error: Shared storage other is not mounted: its drive or path has a character the '
        'logon batch file cannot carry (double quote, a percent sign other than %UserName%, or a '
        'control character)',
        'TASK register MountSharedStorage user=sample-user force=True',
    ]


@pytest.mark.parametrize(
    'path',
    [
        'fs"marker.example.invalid',
        'fs%marker.example.invalid',
        'fs\nmarker.example.invalid',
    ],
)
def test_a_share_the_batch_file_cannot_carry_is_left_out_and_logged(
    tmp_path, monkeypatch, path
):
    configured(monkeypatch, refused=path)
    lines, calls = mount_run(tmp_path, configure_script(tmp_path))
    assert lines == [
        f'if not exist "P:" (net use "P:" "\\\\{SERVER}\\{SHARE}" /persistent:yes)'
    ]
    assert calls[0].startswith('LOG Error: Shared storage marker_fsxw is not mounted')


def test_with_nothing_to_map_an_inherited_mount_task_is_removed(tmp_path, monkeypatch):
    configured(monkeypatch, shares={'other': ontap('O', '%OTHER%', 'SVM.EXAMPLE')})
    lines, calls = mount_run(tmp_path, configure_script(tmp_path))
    assert lines == []
    assert calls[-1] == 'TASK unregister MountSharedStorage'
    assert not any(c.startswith('TASK register') for c in calls)


# the domain controller and OU come from the authorization entry cluster manager writes
CONTROLLER = "dc one & 'two'.example.invalid"
UNIT = "OU=desk 'a' & b,DC=example,DC=invalid"
JOIN_STUBS = r"""
function Get-ADAutomationAuthorizationEntry {
  @{ status = 'success'; domain_controller = $env:IDEA_DC; ou = $env:IDEA_OU; hostname = 'IDEA-MARKER'; otp = 'marker-join-value' } }
function Rename-Computer { [CmdletBinding()] param($NewName, [switch]$Force) Record "RENAME $NewName" }
function Add-Computer { [CmdletBinding()] param([switch]$Force, $DomainName, $Credential, $Server, [switch]$PassThru, $OUPath, $Options)
  $bound = @{} + $PSBoundParameters
  $bound.Remove('Credential') | Out-Null
  $bound.CredentialMatches = [Net.NetworkCredential]::new('', $Credential.Password).Password -ceq 'marker-join-value'
  $bound.Force = [bool]$Force; $bound.PassThru = [bool]$PassThru; $bound.Verbose = [bool]$bound.Verbose
  Record ('JOIN ' + ($bound | ConvertTo-Json -Compress))
  [pscustomobject]@{ HasSucceeded = $true } }
"""


def test_the_join_passes_the_settings_to_add_computer_intact(tmp_path, monkeypatch):
    import windows_pwsh

    configured(monkeypatch)
    code, calls, output = windows_pwsh.run(
        tmp_path,
        configure_script(tmp_path),
        functions=['Start-ADAutomationWaitForAuthorizationAndJoin'],
        variables=['AD_DOMAIN_NAME', 'AD_JOIN_MAX_SLEEP', 'AD_JOIN_RETRY_COUNT'],
        stubs=JOIN_STUBS,
        action='Start-ADAutomationWaitForAuthorizationAndJoin',
        env={'IDEA_DC': CONTROLLER, 'IDEA_OU': UNIT, 'COMPUTERNAME': 'EC2AMAZ-MARKER'},
    )
    assert code == 0, output
    joins = [json.loads(c[5:]) for c in calls if c.startswith('JOIN ')]
    assert len(joins) == 1, calls
    assert joins[0] == {
        'Force': True,
        'DomainName': DOMAIN,
        'Server': CONTROLLER,
        'PassThru': True,
        'OUPath': UNIT,
        'Options': 'UnsecuredJoin, PasswordPass, JoinWithNewName',
        'CredentialMatches': True,
        'ErrorVariable': '+domainJoinError',
        'Verbose': True,
    }
    # the rename sets the pending name the join then uses (JoinWithNewName)
    assert calls.index('RENAME IDEA-MARKER') < next(
        i for i, c in enumerate(calls) if c.startswith('JOIN ')
    )
    assert 'marker-join-value' not in '\n'.join(calls) + output
