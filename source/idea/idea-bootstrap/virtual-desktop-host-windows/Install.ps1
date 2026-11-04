#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

function Write-ToLog {
    # LOG: IDEA Bootstrap Log: Get-Content C:\ProgramData\Amazon\EC2-Windows\Launch\Log\UserdataExecutionIDEA.log
    # LOG: Default User Data: Get-Content C:\ProgramData\Amazon\EC2-Windows\Launch\Log\UserdataExecution.log

    Param (
        [ValidateNotNullOrEmpty()]
        [Parameter(Mandatory=$true)]
        [String] $Message,
        [String] $LogFile = ('{0}\ProgramData\Amazon\EC2-Windows\Launch\Log\UserdataExecutionIDEA.log' -f $env:SystemDrive),
        [ValidateSet('Error','Warn','Info')]
        [string] $Level = 'Info'
    )

    if (-not(Test-Path -Path $LogFile)) {
        $null = New-Item -Path $LogFile -ItemType File -Force
    }


    $FormattedDate = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    switch ($Level) {
        'Error' {
            $LevelText = 'ERROR:'
        }
        'Warn' {
            $LevelText = 'WARNING:'
        }
        'Info' {
            $LevelText = 'INFO:'
        }
    }
    # If Level == Error send ses message ?
    # UTF-8: the default file encoding of PowerShell 5.1 is UTF-16, which reached CloudWatch as
    # NUL-separated bytes ("2 0 2 6 - 1 0 ...") in the bootstrap stream
    [System.IO.File]::AppendAllText($LogFile, "$FormattedDate $LevelText $Message`r`n")
}

function Wait-ForService {
    Param (
        [ValidateNotNullOrEmpty()]
        [Parameter(Mandatory=$true)]
        [String] $Name,
        [int] $TimeoutSeconds = 30
    )

    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while (-not(Get-Service $Name -ErrorAction SilentlyContinue)) {
        if ((Get-Date) -gt $Deadline) {
            Write-ToLog -Message "Service $Name was not registered within $TimeoutSeconds seconds" -Level 'Error'
            exit 1
        }
        Start-Sleep -Milliseconds 250
    }
}

function Assert-InstallerTrusted {
    # Every downloaded installer runs as SYSTEM, so it is checked before it runs. A Valid
    # Authenticode status alone accepts any publicly trusted signer, so the signer must also be
    # the pinned publisher: its CN, O, L and C, which the CA vets and which carry over to a renewed
    # certificate. OU is not vetted and differs between a publisher's certificates. An unsigned
    # installer (7-Zip) is pinned by SHA-256 instead.
    Param (
        [Parameter(Mandatory=$true)] [String] $Path,
        [ValidateSet('Amazon', 'NVIDIA')] [String] $Publisher,
        [String] $Sha256
    )
    $Publishers = @{
        'Amazon' = @('CN=Amazon Web Services, Inc.', 'O=Amazon Web Services, Inc.', 'L=Seattle', 'C=US')
        'NVIDIA' = @('CN=NVIDIA Corporation', 'O=NVIDIA Corporation', 'L=Santa Clara', 'C=US')
    }
    if ($Sha256) {
        $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
        if ($Actual -eq $Sha256) {
            Write-ToLog -Message "Verified $(Split-Path -Leaf $Path): SHA-256 matches pin"
            return
        }
        $Reason = "its SHA-256 $Actual does not match the pinned $Sha256"
    } elseif ($Publisher) {
        $Signature = Get-AuthenticodeSignature -LiteralPath $Path
        if ("$($Signature.Status)" -ne 'Valid') {
            $Reason = "its Authenticode signature is $($Signature.Status): $($Signature.StatusMessage)"
        } else {
            $Subject = @($Signature.SignerCertificate.SubjectName.Decode(
                [Security.Cryptography.X509Certificates.X500DistinguishedNameFlags]'UseNewLines, DoNotUseQuotes'
            ) -split "`r?`n" | ForEach-Object { $_.Trim() })
            $Missing = @($Publishers[$Publisher] | Where-Object { $Subject -cnotcontains $_ })
            if ($Missing.Count -eq 0) {
                Write-ToLog -Message "Verified $(Split-Path -Leaf $Path): Authenticode publisher $($Publishers[$Publisher][0].Substring(3))"
                return
            }
            $Reason = "it is signed by '$($Signature.SignerCertificate.Subject)', not the pinned $Publisher publisher ($($Publishers[$Publisher] -join ', '))"
        }
    } else {
        $Reason = 'no publisher or SHA-256 is pinned for it'
    }
    Write-ToLog -Message "Refusing to install ${Path}: $Reason" -Level 'Error'
    exit 1
}

function Save-Installer {
    # A background-job download whose job process dies reports no error and leaves no file, so
    # msiexec then failed with 1619 (package could not be opened). Download in-process, check
    # the file, and name the failure. The download is then checked against its pinned publisher
    # or SHA-256 before anything runs it.
    Param (
        [Parameter(Mandatory=$true)] [String] $Uri,
        [Parameter(Mandatory=$true)] [String] $OutFile,
        [ValidateSet('Amazon', 'NVIDIA')] [String] $Publisher,
        [String] $Sha256
    )
    $ProgressPreference = 'SilentlyContinue'
    $Saved = $false
    foreach ($Attempt in 1..3) {
        try {
            Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing -ErrorAction Stop
            if ((Test-Path $OutFile) -and (Get-Item $OutFile).Length -gt 0) { $Saved = $true; break }
            $Reason = 'the download wrote no file'
        } catch { $Reason = $_.Exception.Message }
        Write-ToLog -Message "Download of $Uri attempt $Attempt failed: $Reason" -Level 'Warn'
        Start-Sleep -Seconds 3
    }
    if (-not $Saved) {
        Write-ToLog -Message "Could not download $Uri to ${OutFile}: $Reason" -Level 'Error'
        exit 1
    }
    $Pin = @{}
    if ($Publisher) { $Pin.Publisher = $Publisher }
    if ($Sha256) { $Pin.Sha256 = $Sha256 }
    Assert-InstallerTrusted -Path $OutFile @Pin
}

  function Install-NiceDCV {
    Param(
      [string]$OSVersion,
      [string]$InstanceType,
      [switch]$Update
    )

    $DCVInstalled = $false
    $DCVServiceStatus = Get-Service dcvserver -ErrorAction SilentlyContinue -Verbose

    if($DCVServiceStatus.Status){
      if($DCVServiceStatus.Status -eq 'Running'){
        Stop-Service dcvserver
      }
      $DCVInstalled = $true
    }

    if(!$DCVInstalled -or $Update){
      # Information on NICE Virtual Display Driver: https://docs.aws.amazon.com/dcv/latest/adminguide/setting-up-installing-winprereq.html#setting-up-installing-general
      # Every supported base_os (2019, 2022, 2025) ships the Indirect Display Driver with
      # DCV 2023.1+, so the Virtual Display Driver is never installed: it conflicts with GPU drivers.
      Save-Installer -Uri https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-server-x64-Release.msi -OutFile C:\Windows\Temp\DCVServer.msi -Publisher Amazon
      $DCVServerInstall = Start-Process "msiexec.exe" -ArgumentList "/I C:\Windows\Temp\DCVServer.msi ADDLOCAL=ALL /quiet /norestart /l*v dcv_install_msi.log " -Wait -PassThru
      if($DCVServerInstall.ExitCode -notin @(0, 3010)){
        Write-ToLog -Message "DCV Server install failed with exit code $($DCVServerInstall.ExitCode), see dcv_install_msi.log" -Level 'Error'
        exit 1
      }
      Wait-ForService -Name dcvserver
    }

    Get-Service dcvserver -ErrorAction SilentlyContinue
  }

  function Install-NiceSessionManagerAgent {
    Param(
      [switch]$Update
    )

    $DCVSMInstalled = $false
    $DCVSMServiceStatus = Get-Service DcvSessionManagerAgentService -ErrorAction SilentlyContinue -Verbose

    if($DCVSMServiceStatus.Status){
      if($DCVSMServiceStatus.Status -eq 'Running'){
        Stop-Service DcvSessionManagerAgentService
      }
      $DCVSMInstalled = $true
    }

    if(!$DCVSMInstalled -or $Update){
      # Standard distribution link for NICE DCV Session Manager Agent
      Save-Installer -Uri https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-session-manager-agent-x64-Release.msi -OutFile C:\Windows\Temp\DCVSMAgent.msi -Publisher Amazon
      $SMAgentInstall = Start-Process "msiexec.exe" -ArgumentList "/I C:\Windows\Temp\DCVSMAgent.msi /quiet /norestart " -Wait -PassThru
      if($SMAgentInstall.ExitCode -notin @(0, 3010)){
        Write-ToLog -Message "DCV Session Manager Agent install failed with exit code $($SMAgentInstall.ExitCode)" -Level 'Error'
        exit 1
      }
      Wait-ForService -Name DcvSessionManagerAgentService
    }

    Get-Service DcvSessionManagerAgentService -ErrorAction SilentlyContinue
  }

  function Install-AwsCliV2 {
    Write-ToLog -Message "Installing AWS CLI v2 (if needed)" -Level 'Info'
    $AwsCliInstaller = "https://awscli.amazonaws.com/AWSCLIV2.msi"
    try {
        $awsVersion = aws --version 2>&1
        if ($awsVersion -match "^aws-cli\/\d+\.\d+\.\d+") {
          Write-ToLog -Message "AWS CLI is installed. Version: $awsVersion" -Level 'Info'
        }
        else {
        Write-ToLog -Message "AWS not found, installing it ..." -Level 'Info'
        $ProgressPreference = 'SilentlyContinue'
        # Create directory if it doesn't exist
        $workdir = 'C:\IDEA\Assets'
        if (!(Test-Path -Path $workdir -PathType Container)) {
          New-Item -Path $workdir -ItemType Directory -Force | Out-Null
          Write-ToLog -Message "Created directory: $workdir" -Level 'Info'
        }
        Save-Installer -Uri $AwsCliInstaller -OutFile "$workdir\AWSCLIV2.msi" -Publisher Amazon
        Start-Process msiexec.exe -Wait -ArgumentList "/i $workdir\AWSCLIV2.msi /quiet"
        }
    } catch {
    Write-ToLog -Message "AWS not found, installing it ..." -Level 'Info'
    $ProgressPreference = 'SilentlyContinue'
    # Create directory if it doesn't exist
    $workdir = 'C:\IDEA\Assets'
    if (!(Test-Path -Path $workdir -PathType Container)) {
      New-Item -Path $workdir -ItemType Directory -Force | Out-Null
      Write-ToLog -Message "Created directory: $workdir" -Level 'Info'
    }
    Save-Installer -Uri $AwsCliInstaller -OutFile "$workdir\AWSCLIV2.msi" -Publisher Amazon
    Start-Process msiexec.exe -Wait -ArgumentList "/i $workdir\AWSCLIV2.msi /quiet"
    }
}
function Install-7Zip {
    # 7-Zip installers are not Authenticode-signed, so a pinned release is checked by SHA-256.
    # To move to a new release, change both values together; the GitHub release (ip7z/7zip) lists
    # each asset's SHA-256.
    $Release = '26.05'
    $Version = $Release.Replace('.', '')
    $Sha256 = 'FD99E86F57D8F429EF5ED045BF98315AF962E2309B0F1BA87A147A44C71647D0'
    Write-ToLog -Message "Installing 7-Zip $Release" -Level 'Info'

    # Check if already installed
    $7zPath = "${env:ProgramFiles}\7-Zip\7z.exe"
    if (Test-Path $7zPath) {
      Write-ToLog -Message "7-Zip is already installed" -Level 'Info'
      return
    }

    try {
      # Download and install
      $workdir = 'C:\IDEA\Assets'

      # Create workdir if it doesn't exist
      if (!(Test-Path -Path $workdir -PathType Container)) {
        New-Item -Path $workdir -ItemType Directory -Force | Out-Null
        Write-ToLog -Message "Created directory: $workdir" -Level 'Info'
      }

      $source = "https://www.7-zip.org/a/7z$Version-x64.exe"
      $destination = "$workdir\7z$Version-x64.exe"
      Save-Installer -Uri $source -OutFile $destination -Sha256 $Sha256

      # Install silently
      Start-Process -FilePath $destination -ArgumentList '/S' -Wait -PassThru | Out-Null

      Write-ToLog -Message "7-Zip installed successfully" -Level 'Info'
    }
    catch {
      Write-ToLog -Message "Error installing 7-Zip: $_" -Level 'Error'
    }
  }


function Install-WindowsEC2Instance {
    Param(

      [switch]$ConfigureForEVDI,
      [switch]$Update,
      [string]$ModuleVersion = (& "$PSScriptRoot\ModuleVersion.ps1")
    )
    # Configuration always runs; only a matching release may skip installation.
    $BakedRelease = 'C:\ProgramData\IDEA\baked-release'
    if (-not $Update -and $ModuleVersion -and (Test-Path $BakedRelease) -and
        (Get-Content $BakedRelease -Raw).Trim() -eq $ModuleVersion) {
        if ($ConfigureForEVDI) {
            Import-Module .\Configure.ps1
            Configure-WindowsEC2Instance
        }
        return
    }
    Write-ToLog -Message "Installing Windows EC2 Instance" -Level 'Info'
    $timestamp = Get-Date -Format 'yyyyMMddTHHmmssffffZ'
    $InstallEVDI = "$env:SystemDrive\Users\Administrator\IDEA\bootstrap\log\InstallEVDI.log.$timestamp"

    Start-Transcript -Path $InstallEVDI -NoClobber -IncludeInvocationHeader

    [string]$IMDS_Token = Invoke-RestMethod -Headers @{"X-aws-ec2-metadata-token-ttl-seconds" = "600"} -Method PUT -Uri http://169.254.169.254/latest/api/token -Verbose
    $OSVersion = ((Get-ItemProperty -Path "Microsoft.PowerShell.Core\Registry::\HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion" -Name ProductName -Verbose).ProductName) -replace  "[^0-9]" , ''
    $InstanceType = Invoke-RestMethod -Headers @{'X-aws-ec2-metadata-token' = $IMDS_Token} -Method GET -Uri http://169.254.169.254/latest/meta-data/instance-type -Verbose
    Write-ToLog -Message "OS Version: $OSVersion" -Level 'Info'
    Write-ToLog -Message "Instance Type: $InstanceType" -Level 'Info'
    Write-ToLog -Message "Installing NICE DCV" -Level 'Info'
    Install-NiceDCV -OSVersion $OSVersion -InstanceType $InstanceType -Update:$Update
    Write-ToLog -Message "Installing NICE Session Manager Agent" -Level 'Info'
    Install-NiceSessionManagerAgent -Update:$Update
    Write-ToLog -Message "Installing AWS CLI v2" -Level 'Info'
    Install-AwsCliV2
    Write-ToLog -Message "Adding AWS CLI v2 to PATH" -Level 'Info'
    $env:Path += ";C:\Program Files\Amazon\AWSCLIV2\"
    Write-ToLog -Message "Installing 7-Zip" -Level 'Info'
    Install-7Zip
    Write-ToLog -Message "Adding 7-Zip to PATH" -Level 'Info'
    $env:Path += ";C:\Program Files\7-Zip\"
    Stop-Transcript

    if($ConfigureForEVDI){
      Import-Module .\Configure.ps1
      Write-ToLog -Message "Configuring Windows EC2 Instance" -Level 'Info'
      Configure-WindowsEC2Instance
    }
  }
