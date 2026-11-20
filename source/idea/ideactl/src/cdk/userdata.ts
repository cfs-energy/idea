/**
 * Builds Linux and Windows user data before CDK wraps it in `Fn::Sub` or
 * `Fn::Base64`. It preserves trailing indentation, empty config here-docs,
 * and a newline after each install command.
 */

export interface BootstrapUserDataParams {
  baseOs: string;
  awsRegion: string;
  bootstrapPackageUri: string;
  installCommands: string[];
  infraConfig?: Record<string, string> | null;
  proxyConfig?: Record<string, string> | null;
  /** Defaults to true. False emits `${VAR}` instead of Fn::Sub-escaped `${!VAR}`. */
  substitutionSupport?: boolean;
}

// The AWS CLI installer comes from an unversioned URL, so no checksum can be pinned. Its
// detached signature is checked against the AWS CLI team key carried here (primary
// fingerprint FB5D B77F D5C1 18B8 0511 ADA8 A631 0ACC 4672 475C, published in the AWS CLI
// user guide; the copy expires 2027-07-01, and a signature by the pinned key still counts
// after that). Written without braces: the CloudFormation variant substitutes ${...}.
const AWS_CLI_SIGNATURE_CHECK = `
AWS_CLI_KEY_FINGERPRINT=FB5DB77FD5C118B80511ADA8A6310ACC4672475C
function verify_aws_cli_signature () {
  local url gnupg status attempt
  case "$(uname -m)" in
    x86_64) url=$AWSCLI_X86_64_URL ;;
    aarch64) url=$AWSCLI_AARCH64_URL ;;
    *) return 1 ;;
  esac
  if ! command -v gpg > /dev/null 2>&1; then
    if [[ $BASE_OS =~ ^ubuntu ]]; then
      retry_bootstrap apt-get -o APT::Update::Error-Mode=any -o DPkg::Lock::Timeout=300 update || return $?
      retry_bootstrap apt-get -o DPkg::Lock::Timeout=300 install -y gpg || return $?
    else
      retry_bootstrap yum install -y gnupg2 || return $?
    fi
  fi
  gnupg=$(mktemp -d) || return $?
  cat > "$gnupg/aws-cli.asc" <<'AWS_CLI_KEY'
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBF2Cr7UBEADJZHcgusOJl7ENSyumXh85z0TRV0xJorM2B/JL0kHOyigQluUG
ZMLhENaG0bYatdrKP+3H91lvK050pXwnO/R7fB/FSTouki4ciIx5OuLlnJZIxSzx
PqGl0mkxImLNbGWoi6Lto0LYxqHN2iQtzlwTVmq9733zd3XfcXrZ3+LblHAgEt5G
TfNxEKJ8soPLyWmwDH6HWCnjZ/aIQRBTIQ05uVeEoYxSh6wOai7ss/KveoSNBbYz
gbdzoqI2Y8cgH2nbfgp3DSasaLZEdCSsIsK1u05CinE7k2qZ7KgKAUIcT/cR/grk
C6VwsnDU0OUCideXcQ8WeHutqvgZH1JgKDbznoIzeQHJD238GEu+eKhRHcz8/jeG
94zkcgJOz3KbZGYMiTh277Fvj9zzvZsbMBCedV1BTg3TqgvdX4bdkhf5cH+7NtWO
lrFj6UwAsGukBTAOxC0l/dnSmZhJ7Z1KmEWilro/gOrjtOxqRQutlIqG22TaqoPG
fYVN+en3Zwbt97kcgZDwqbuykNt64oZWc4XKCa3mprEGC3IbJTBFqglXmZ7l9ywG
EEUJYOlb2XrSuPWml39beWdKM8kzr1OjnlOm6+lpTRCBfo0wa9F8YZRhHPAkwKkX
XDeOGpWRj4ohOx0d2GWkyV5xyN14p2tQOCdOODmz80yUTgRpPVQUtOEhXQARAQAB
tCFBV1MgQ0xJIFRlYW0gPGF3cy1jbGlAYW1hem9uLmNvbT6JAlQEEwEIAD4CGwMF
CwkIBwIGFQoJCAsCBBYCAwECHgECF4AWIQT7Xbd/1cEYuAURraimMQrMRnJHXAUC
akV0ygUJDqP4lQAKCRCmMQrMRnJHXFHjD/9eyZLYcKuQOlLvtqSDtUBiEZf6ZZjM
i3ygYH8rJNtuToUH+HvSpe819urJCquXhDrlK6N+aqW0hCLtNABJG/vsafIgvIYJ
hSGgpgtNnQyMV1jViRWqPjbouw8OkYKBThUfT1i2Y+wn58ifs6ODBCmTexWtXspA
Si+Gt49xDOW0APmbOPnI+a4HJW6tVEo6MWS0WjzpiBayR3d1A4pt4YrPfSdDgpLo
h2SLQqlRqvvVZJaWBjhkErNFpfsBA06sDcPEOb0G8LBUbR4WOcdvhe5LubJbZuxC
AG9kNPCVeQP1ixwjgjXKysaxeQ6rv0VzIQgRp6tLVLWhy6AKDNvLjFSsmXZ1Wl08
Y/RlOHXlzLuQMRE6sR1wOdRxc9TsrNWTGiBK65cvSWOy03JeBkQQ8pesqltiyxI9
U21kkgiXtTSKNGfKK8pO27D81YANhRqPK7iTp6kuFiY2WtOg90KTMNlIT+Ff85Y2
b1rHj6Z0SrCkJujhWk3IBPic/wJgz01LEc/OAdUPlby90RJZcIBhSlWhT7mXnXIO
c0HWlNQrns2s3CTyYwZSiSlYe9ApeLwhjDo8NhbFuCAy61l6O5UsR4AfZxx/rGKv
2wFb1/RN/P4gNe6vmxZAPjR0AQcwD3tc2McimOLr/22kmPz8IH3I0X7WoSFr0Biz
E91G7bb0hOb/cA==
=knv7
-----END PGP PUBLIC KEY BLOCK-----
AWS_CLI_KEY
  # no agent: a minimal gpg (Amazon Linux 2023) has none, and a failed start fails the import
  if ! gpg --batch --quiet --no-autostart --homedir "$gnupg" --import "$gnupg/aws-cli.asc"; then
    echo "The AWS CLI key could not be imported; not installing the AWS CLI" >&2
    rm -rf "$gnupg" awscliv2.zip
    return 1
  fi
  # the installer URL is unversioned: a release published between the two downloads fails
  # once, so the installer and its signature are fetched again before giving up
  for attempt in 1 2; do
    if [[ $attempt -eq 2 ]]; then
      echo "The AWS CLI download did not verify; fetching it and its signature again" >&2
      curl -fSL --retry 3 --connect-timeout 10 --max-time 120 "$url" -o awscliv2.zip || break
    fi
    if ! curl -fSL --retry 3 --connect-timeout 10 --max-time 60 "$url.sig" -o awscliv2.zip.sig; then
      echo "Could not download the AWS CLI signature $url.sig; not installing an unverified AWS CLI" >&2
      rm -rf "$gnupg" awscliv2.zip
      return 1
    fi
    status=$(gpg --batch --no-autostart --homedir "$gnupg" --status-fd 1 --verify awscliv2.zip.sig awscliv2.zip 2> /dev/null)
    if awk -v want="$AWS_CLI_KEY_FINGERPRINT" '$2 == "VALIDSIG" && $NF == want { found = 1 } END { exit !found }' <<< "$status"; then
      rm -rf "$gnupg" awscliv2.zip.sig
      return 0
    fi
  done
  rm -rf "$gnupg" awscliv2.zip.sig awscliv2.zip
  echo "The AWS CLI download is not signed by the AWS CLI key $AWS_CLI_KEY_FINGERPRINT; not installing it" >&2
  return 1
}
`;

/**
 * The shell body shared by both Linux variants, written in the substitution form.
 * The two forms differ only in `${!` versus `${`.
 */
const LINUX_BODY = `

timestamp=$(date +%s)
mkdir -p /root/bootstrap/logs
if [[ -f /root/bootstrap/logs/userdata.log ]]; then
  mv /root/bootstrap/logs/userdata.log /root/bootstrap/logs/userdata.log.\${!timestamp}
fi
exec > /root/bootstrap/logs/userdata.log 2>&1

export PATH="\${!PATH}:/usr/local/bin"

function retry_bootstrap () {
  local delay status output
  output=$(mktemp) || return $?
  for delay in 15 30 60 0; do
    if "$@" > "$output" 2>&1; then status=0; else status=$?; fi
    if [[ $status -eq 0 || $delay -eq 0 ]]; then
      cat "$output"
      rm -f "$output"
      return "$status"
    fi
    # Recovered errors must not fail the later in-bake log scan.
    sed 's/^/[retried] /' "$output"
    echo "Retrying $* in \${!delay}s (exit $status)"
    sleep "$delay"
  done
}

function fail_userdata () {
  echo "userdata failed: $1; see /root/bootstrap/logs/userdata.log" >&2
  # A failed CLI install may leave no way to publish the failure tag.
  if command -v aws >/dev/null 2>&1; then
    local token instance_id status
    token=$(curl -fsS --connect-timeout 2 --max-time 5 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
    instance_id=$(curl -fsS --connect-timeout 2 --max-time 5 -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id)
    status="$1-failed"
    [[ "$BOOTSTRAP_FAILURE_TAG" != idea:AmiBuilderStatus ]] || status="failed:userdata-$1"
    [[ -z "$instance_id" ]] || aws --region "$AWS_REGION" --cli-connect-timeout 5 --cli-read-timeout 10 ec2 create-tags --resources "$instance_id" --tags "Key=$BOOTSTRAP_FAILURE_TAG,Value=$status" || true
  fi
  exit 1
}

${AWS_CLI_SIGNATURE_CHECK}
function install_aws_cli () {
  # an image baked from this bootstrap already has the CLI; reinstalling it cost every boot
  if [[ -x /usr/local/aws-cli/v2/current/bin/aws && -x /bin/aws ]]; then
    return 0
  fi
  cd /root/bootstrap || return $?
  local machine=$(uname -m)
  if [[ \${!machine} == "x86_64" ]]; then
    curl -fSL --retry 3 --connect-timeout 10 --max-time 120 \${!AWSCLI_X86_64_URL} -o "awscliv2.zip" || return $?
    elif [[ \${!machine} == "aarch64" ]]; then
      curl -fSL --retry 3 --connect-timeout 10 --max-time 120 \${!AWSCLI_AARCH64_URL} -o "awscliv2.zip" || return $?
  else
    echo "Unsupported AWS CLI architecture: $machine" >&2
    return 1
  fi
  verify_aws_cli_signature || return $?
  # the image's own CLI goes only once a verified one is ready to replace it: until then it
  # can still publish the failure tag
  if [[ "\${!BASE_OS}" == "amazonlinux2023" ]]; then
    retry_bootstrap yum remove -y awscli || return $?
  fi
  if ! command -v unzip > /dev/null 2>&1; then
    if [[ $BASE_OS =~ ^ubuntu ]]; then
      retry_bootstrap apt-get -o APT::Update::Error-Mode=any -o DPkg::Lock::Timeout=300 update || return $?
      retry_bootstrap apt-get -o DPkg::Lock::Timeout=300 install -y unzip || return $?
    else
      retry_bootstrap yum install -y unzip || return $?
    fi
  fi
  unzip -q awscliv2.zip || return $?
  ./aws/install --bin-dir /bin --update || return $?
  aws --version || return $?
  rm -rf aws awscliv2.zip
}

echo "#!/bin/bash
set -e
PACKAGE_DOWNLOAD_URI=\\\${!1}
PACKAGE_ARCHIVE=\\$(basename \\\${!PACKAGE_DOWNLOAD_URI})
PACKAGE_NAME=\\\${!PACKAGE_ARCHIVE%.tar.gz*}
INSTANCE_REGION=\\$(TOKEN=\\$(curl --silent -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 900') && curl --silent -H \\"X-aws-ec2-metadata-token: \\\${!TOKEN}\\" 'http://169.254.169.254/latest/meta-data/placement/region')
if [[ \\\${!PACKAGE_DOWNLOAD_URI} == s3://* ]]; then
  AWS=\\$(command -v aws)
  S3_BUCKET=\\$(echo \\\${!PACKAGE_DOWNLOAD_URI} | cut -f3 -d/)
  if [[ \\\${!INSTANCE_REGION} =~ ^us-gov-[a-z]+-[0-9]+$ ]]; then
    S3_BUCKET_REGION=\\$(curl -s --connect-timeout 5 --max-time 10 --head https://\\\${!S3_BUCKET}.s3.us-gov-west-1.amazonaws.com | grep bucket-region | awk '{print \\$2}' | tr -d '\\r\\n')
    [[ -n \\"\\\${!S3_BUCKET_REGION}\\" ]] || S3_BUCKET_REGION=\\"\\\${!INSTANCE_REGION}\\"
    \\$AWS --region \\\${!S3_BUCKET_REGION} s3 cp \\\${!PACKAGE_DOWNLOAD_URI} /root/bootstrap/
  else
    #S3_BUCKET_REGION=\\$(curl -s --head https://\\\${!S3_BUCKET}.s3.us-east-1.amazonaws.com | grep bucket-region | awk '{print \\$2}' | tr -d '\\r\\n')
    \\$AWS --region \\\${!INSTANCE_REGION} s3 cp \\\${!PACKAGE_DOWNLOAD_URI} /root/bootstrap/
  fi
else
  cp \\\${!PACKAGE_DOWNLOAD_URI} /root/bootstrap/
fi
PACKAGE_DIR=/root/bootstrap/\\\${!PACKAGE_NAME}
if [[ -d \\\${!PACKAGE_DIR} ]]; then
  rm -rf \\\${!PACKAGE_DIR}
fi
mkdir -p \\\${!PACKAGE_DIR}
tar -xvf /root/bootstrap/\\\${!PACKAGE_ARCHIVE} -C \\\${!PACKAGE_DIR}
rm -f /root/bootstrap/latest
ln -sf \\\${!PACKAGE_DIR} /root/bootstrap/latest
" > /root/bootstrap/download_bootstrap.sh

chmod +x /root/bootstrap/download_bootstrap.sh
        `;

// The same text as the Windows branch of idea-sdk bootstrap_userdata_builder.py, byte for byte
// (bootstrap-userdata-parity.test.ts compares both with the fixture rendered by the SDK builder): a
// failed download or extraction logs, tags the instance with the failure status Linux uses and
// stops before the install commands, and a package left by the desktop an image was taken from is
// cleared first so its scripts never run here.
const windowsUserData = (uri: string, region: string, installCommands: string[]): string => {
  const [tagKey, tagValue] = installCommands.some((command) => command.includes('-ami-builder-windows'))
    ? ['idea:AmiBuilderStatus', 'failed:userdata-bootstrap-download']
    : ['idea:BootstrapStatus', 'bootstrap-download-failed'];
  return `
<powershell>
 $BootstrapDir = "C:\\Users\\Administrator\\IDEA\\bootstrap"
 function Write-IdeaUserdataLog {
     # the bootstrap log Install.ps1 writes later, in the same line format, so it is shipped too
     Param([String] $Message, [String] $Level)
     Write-Output "$Level $Message"
     $Log = '{0}\\ProgramData\\Amazon\\EC2-Windows\\Launch\\Log' -f $env:SystemDrive
     # the line is on the console above; a log file that cannot be written never stops the bootstrap
     try {
         New-Item -ItemType Directory -Path $Log -Force -ErrorAction Stop | Out-Null
         Add-Content -Path "$Log\\UserdataExecutionIDEA.log" -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') \${Level}: $Message" -ErrorAction Stop
     } catch { }
 }
 function Remove-InheritedPath {
     # Remove-Item fails while another process holds a file open (the CloudWatch agent tails
     # log\\*): retried as the image builder's cleanup does
     Param([String] $Path)
     foreach ($Attempt in 1..5) {
         if (-not (Test-Path -LiteralPath $Path)) { return $true }
         try { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop } catch { Start-Sleep -Seconds 3 }
     }
     -not (Test-Path -LiteralPath $Path)
 }
 function Protect-InheritedLog {
     # a log another instance left that cannot be removed now is limited to SYSTEM and
     # Administrators and reported; it never stops the bootstrap
     Param([String] $Path)
     $Result = 'access is limited to SYSTEM and Administrators'
     try {
         $Acl = New-Object System.Security.AccessControl.FileSecurity
         $Acl.SetAccessRuleProtection($true, $false)
         foreach ($Sid in 'S-1-5-18', 'S-1-5-32-544') {
             $Acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule((New-Object System.Security.Principal.SecurityIdentifier $Sid), 'FullControl', 'Allow')))
         }
         Set-Acl -LiteralPath $Path -AclObject $Acl -ErrorAction Stop
     } catch { $Result = "its access could not be limited: $($_.Exception.Message)" }
     Write-IdeaUserdataLog "could not remove $Path, left by another instance; $Result" 'WARNING'
 }
 function Clear-ForeignBootstrap {
     # An image taken from another desktop carries that desktop's package, extracted scripts,
     # transcripts and bootstrap log, which can hold its settings and, from earlier releases, its
     # local passwords. The folder belongs to the instance named in instance-id; for any other
     # instance all of it is removed before this instance's package is downloaded and before the
     # owner's account is created.
     $InstanceId = Get-EC2InstanceMetadata -Category InstanceId
     if (-not $InstanceId) { throw 'the instance id is not available from instance metadata' }
     $Owner = "$BootstrapDir\\instance-id"
     $Owned = "$(if (Test-Path -Path $Owner) { Get-Content -Path $Owner -Raw })".Trim()
     if ($Owned -eq $InstanceId) { return }
     # Logs first: the IDEA bootstrap log UserdataExecutionIDEA.log (before anything of this
     # instance is written to it), then every file under log\\ (ConfigureEVDI.log.<time> and
     # InstallEVDI.log.<time>, the transcripts this and earlier releases write). A log that
     # cannot be removed is protected, never fatal.
     $Logs = @('{0}\\ProgramData\\Amazon\\EC2-Windows\\Launch\\Log\\UserdataExecutionIDEA.log' -f $env:SystemDrive)
     $Logs += @(Get-ChildItem -Path "$BootstrapDir\\log" -File -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
     foreach ($Log in $Logs) {
         try { if (-not (Remove-InheritedPath $Log)) { Protect-InheritedLog $Log } }
         catch { Write-Output "WARNING could not handle $Log, left by another instance: $($_.Exception.Message)" }
     }
     foreach ($Item in @(Get-ChildItem -Path $BootstrapDir -Force)) {
         # the log folder may still hold a protected log; an old package would be used, so it must go
         if ($Item.Name -eq 'log') { try { Remove-InheritedPath $Item.FullName | Out-Null } catch { }; continue }
         if (-not (Remove-InheritedPath $Item.FullName)) { throw "could not remove $($Item.FullName), left by another instance" }
     }
     Set-Content -Path $Owner -Value $InstanceId
 }
 function Download-Idea-Package {
     Param(
     [ValidateNotNullOrEmpty()]
     [Parameter(Mandatory=$true)]
     [String] $PackageDownloadURI
     )
     $ErrorActionPreference = 'Stop'
     if (!(Test-Path "$BootstrapDir")) {
         New-Item -itemType Directory -Path "$BootstrapDir"
     }
     cd "$BootstrapDir"
     Clear-ForeignBootstrap
     Write-Output $PackageDownloadURI
     $PackageArchive=Split-Path $PackageDownloadURI -Leaf
     $PackageName = [System.IO.Path]::GetFileNameWithoutExtension($PackageDownloadURI)
     if ($PackageDownloadURI -like "s3\`://*") {
        $urlParts = $PackageDownloadURI -Split "/", 4
        $bucketName = $urlParts[2]
        $key = $urlParts[3]
        Copy-S3Object -BucketName $bucketName -Key $key -LocalFile "$BootstrapDir\\$PackageArchive" -Force
     } else {
        Copy-Item -Path $PackageDownloadURI -Destination "$BootstrapDir\\$PackageArchive"
     }
     Tar -xf "$BootstrapDir\\$PackageArchive"
     if ($LASTEXITCODE -ne 0) { throw "extracting $PackageArchive failed with exit code $LASTEXITCODE" }
 }
 # the install commands below run only from a package this instance downloaded and extracted
 try {
     Download-Idea-Package ${uri}
 } catch {
     Write-IdeaUserdataLog "IDEA bootstrap package download failed: $($_.Exception.Message)" 'ERROR'
     try {
         New-EC2Tag -Resource (Get-EC2InstanceMetadata -Category InstanceId) -Tag @{Key='${tagKey}'; Value='${tagValue}'} -Region '${region}'
     } catch { }
     exit 1
 }
`;
};

export function buildBootstrapUserData(params: BootstrapUserDataParams): string {
  const {
    baseOs,
    awsRegion,
    bootstrapPackageUri,
    installCommands,
    infraConfig,
    proxyConfig,
    substitutionSupport = true,
  } = params;

  if (baseOs.toLowerCase().includes('windows')) {
    if (infraConfig && Object.keys(infraConfig).length > 0) {
      throw new Error('infra config is not supported for windows');
    }
    return (
      windowsUserData(bootstrapPackageUri, awsRegion, installCommands) +
      installCommands.map((c) => `${c}\n`).join('') +
      '</powershell>'
    );
  }

  const infra = Object.entries(infraConfig ?? {})
    .map(([k, v]) => `${k}=${v}\n`)
    .join('');
  const proxy = Object.entries(proxyConfig ?? {})
    .map(([k, v]) => `export ${k}=${v}\n`)
    .join('');

  let userdata =
    `#!/bin/bash\n` +
    `\n` +
    `set -x\n` +
    `mkdir -p /root/bootstrap\n` +
    `AWS_REGION="${awsRegion}"\n` +
    `BASE_OS="${baseOs}"\n` +
    `BOOTSTRAP_FAILURE_TAG="${installCommands.some((cmd) => cmd.includes('-ami-builder/setup.sh')) ? 'idea:AmiBuilderStatus' : 'idea:BootstrapStatus'}"\n` +
    `DEFAULT_AWS_REGION="${awsRegion}"\n` +
    `AWSCLI_X86_64_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"\n` +
    `AWSCLI_AARCH64_URL="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"\n`;

  // The substitution variant writes infra.cfg, including when it is empty.
  if (substitutionSupport) {
    userdata += `\necho "\n${infra}\n" > /root/bootstrap/infra.cfg\n        `;
  }

  userdata += `\necho "${proxy}\n" > /root/bootstrap/proxy.cfg\nsource /root/bootstrap/proxy.cfg\n        `;

  userdata += substitutionSupport ? LINUX_BODY : LINUX_BODY.replaceAll('${!', '${');

  userdata +=
    `\ninstall_aws_cli || fail_userdata aws-cli\n` +
    // single-quoted data, as the SDK builder writes it
    `bash /root/bootstrap/download_bootstrap.sh '${bootstrapPackageUri.replaceAll("'", "'\\''")}' || fail_userdata bootstrap-download\n` +
    `\n` +
    `cd /root/bootstrap/latest || fail_userdata bootstrap-download\n`;

  return userdata + installCommands.map((c) => `${c}\n`).join('');
}
