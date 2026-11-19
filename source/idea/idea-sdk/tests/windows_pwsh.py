"""
Runs functions from a rendered Windows bootstrap script under pwsh with stubbed cmdlets.

Only the named functions and top-level assignments are loaded (from the parsed script), so the
script's own top-level steps never run. Stubs append lines to a calls file through `Record`;
the run returns pwsh's exit code and those lines.
"""

import os
import subprocess
from pathlib import Path

import pytest

from test_bootstrap_shell_syntax import PWSH

# In CI (or with IDEA_REQUIRE_PWSH=1) a missing pwsh fails these tests; elsewhere they skip with
# a reason the -ra summary prints, so a run without them is never silent.
REQUIRED = os.environ.get('CI') == 'true' or os.environ.get('IDEA_REQUIRE_PWSH') == '1'
MISSING = 'pwsh not installed: the Windows bootstrap behaviour tests did NOT run'
needs_pwsh = pytest.mark.skipif(PWSH is None and not REQUIRED, reason=MISSING)


def require():
    if PWSH is None:
        pytest.fail(
            f'{MISSING} (required here: CI or IDEA_REQUIRE_PWSH=1)', pytrace=False
        )


# records every transcript change and every bootstrap log line
COMMON_STUBS = r"""
function Start-Transcript { param($Path, [switch]$Append, [switch]$NoClobber, [switch]$IncludeInvocationHeader) Record 'TRANSCRIPT start' }
function Stop-Transcript { Record 'TRANSCRIPT stop' }
function Write-ToLog { param($Message, $Level = 'Info') Record "LOG ${Level}: $Message" }
"""


def run(tmp_path, script, functions=(), variables=(), stubs='', action='', env=None):
    require()
    calls = tmp_path / 'calls.txt'
    calls.write_text('', encoding='utf-8')
    names = ', '.join(f"'{n}'" for n in functions)
    assigned = ', '.join(f"'{n}'" for n in variables)
    probe = tmp_path / 'probe.ps1'
    # Windows paths (C:\...) resolve under tmp_path/C: for cmdlets (a C: drive) and for .NET
    # calls (a relative folder named C: in the working directory) alike
    drive = tmp_path / 'cdrive'
    drive.mkdir(exist_ok=True)
    if not (tmp_path / 'C:').exists():
        (tmp_path / 'C:').symlink_to(drive, target_is_directory=True)
    probe.write_text(
        f"New-PSDrive -Name C -PSProvider FileSystem -Root '{drive}' -Scope Global | Out-Null\n"
        f"$global:IdeaCalls = '{calls}'\n"
        'function Record { param($Line) Add-Content -LiteralPath $global:IdeaCalls -Value $Line -Encoding utf8 }\n'
        + COMMON_STUBS
        + stubs
        + '\n$errors = $null\n'
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$errors)\n"
        'if ($errors) { throw "the script does not parse: $($errors[0].Message)" }\n'
        f'foreach ($name in @({names})) {{\n'
        '  $f = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)\n'
        '  if (-not $f) { throw "no function $name" }\n'
        '  . ([scriptblock]::Create($f.Extent.Text))\n'
        '}\n'
        f'foreach ($name in @({assigned})) {{\n'
        '  $a = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq "`$$name" }, $true)\n'
        '  if (-not $a) { throw "no assignment to $name" }\n'
        '  . ([scriptblock]::Create($a.Extent.Text))\n'
        '}\n' + action + '\n',
        encoding='utf-8',
    )
    result = subprocess.run(
        [PWSH, '-NoProfile', '-NonInteractive', '-File', str(probe)],
        capture_output=True,
        text=True,
        encoding='utf-8',
        cwd=tmp_path,
        env={**os.environ, **(env or {})},
    )
    lines = calls.read_text(encoding='utf-8').splitlines()
    return result.returncode, lines, result.stdout + result.stderr


def configure(tmp_path, base_os='windows2022'):
    """the rendered Configure.ps1 of a desktop package"""
    import test_bootstrap_shell_syntax as shell

    _, component, variables = shell.packages(base_os)[0]
    scripts = shell.render(tmp_path, base_os, 'm7i.large', component, variables)
    return Path(next(f for f in scripts if f.endswith('Configure.ps1')))
