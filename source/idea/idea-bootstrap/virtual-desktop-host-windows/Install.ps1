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
    "$FormattedDate $LevelText $Message" | Out-File -FilePath $LogFile -Append
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
      Start-Job -Name DCVWebReq -ScriptBlock { Invoke-WebRequest -uri https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-server-x64-Release.msi -OutFile C:\Windows\Temp\DCVServer.msi }
      Wait-Job -Name DCVWebReq
      $DCVServerInstall = Start-Process "msiexec.exe" -ArgumentList "/I C:\Windows\Temp\DCVServer.msi ADDLOCAL=ALL /quiet /norestart /l*v dcv_install_msi.log " -Wait -PassThru
      if($DCVServerInstall.ExitCode -ne 0){
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
      Start-Job -Name SMWebReq -ScriptBlock { Invoke-WebRequest -uri https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-session-manager-agent-x64-Release.msi -OutFile C:\Windows\Temp\DCVSMAgent.msi }
      Wait-Job -Name SMWebReq
      $SMAgentInstall = Start-Process "msiexec.exe" -ArgumentList "/I C:\Windows\Temp\DCVSMAgent.msi /quiet /norestart " -Wait -PassThru
      if($SMAgentInstall.ExitCode -ne 0){
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
        Invoke-WebRequest $AwsCliInstaller -OutFile "$workdir\AWSCLIV2.msi"
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
    Invoke-WebRequest $AwsCliInstaller -OutFile "$workdir\AWSCLIV2.msi"
    Start-Process msiexec.exe -Wait -ArgumentList "/i $workdir\AWSCLIV2.msi /quiet"
    }
}
function Install-7Zip {
    Write-ToLog -Message "Installing 7-Zip (latest version)" -Level 'Info'

    # Check if already installed
    $7zPath = "${env:ProgramFiles}\7-Zip\7z.exe"
    if (Test-Path $7zPath) {
      Write-ToLog -Message "7-Zip is already installed" -Level 'Info'
      return
    }

    try {
      # Get latest version info
      $url = 'https://sourceforge.net/projects/sevenzip/best_release.json'
      $releaseInfo = ConvertFrom-Json (Invoke-WebRequest $url -UseBasicParsing).Content
      $Version = $releaseInfo.release.filename.Split('/')[2] -replace("[^\d]","")

      # Download and install
      $workdir = 'C:\IDEA\Assets'

      # Create workdir if it doesn't exist
      if (!(Test-Path -Path $workdir -PathType Container)) {
        New-Item -Path $workdir -ItemType Directory -Force | Out-Null
        Write-ToLog -Message "Created directory: $workdir" -Level 'Info'
      }

      $source = "https://www.7-zip.org/a/7z$Version-x64.exe"
      $destination = "$workdir\7z$Version-x64.exe"

      $ProgressPreference = 'SilentlyContinue'
      Invoke-WebRequest $source -OutFile $destination -UseBasicParsing

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
      [switch]$Update
    )
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
