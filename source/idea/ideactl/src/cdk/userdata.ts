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

function install_aws_cli () {
  # an image baked from this bootstrap already has the CLI; reinstalling it cost every boot
  if [[ -x /usr/local/aws-cli/v2/current/bin/aws && -x /bin/aws ]]; then
    return 0
  fi
  if [[ "\${!BASE_OS}" == "amazonlinux2023" ]]; then
    retry_bootstrap yum remove -y awscli || return $?
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
    S3_BUCKET_REGION=\\$(curl -s --head https://\\\${!S3_BUCKET}.s3.us-gov-west-1.amazonaws.com | grep bucket-region | awk '{print \\$2}' | tr -d '\\r\\n')
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

const windowsUserData = (uri: string): string => `
<powershell>
 $BootstrapDir = "C:\\Users\\Administrator\\IDEA\\bootstrap"
 function Download-Idea-Package {
     Param(
     [ValidateNotNullOrEmpty()]
     [Parameter(Mandatory=$true)]
     [String] $PackageDownloadURI
     )
     if (!(Test-Path "$BootstrapDir")) {
         New-Item -itemType Directory -Path "$BootstrapDir"
     }
     cd "$BootstrapDir"
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
 }
 Download-Idea-Package ${uri}
`;

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
      windowsUserData(bootstrapPackageUri) +
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
    `bash /root/bootstrap/download_bootstrap.sh "${bootstrapPackageUri}" || fail_userdata bootstrap-download\n` +
    `\n` +
    `cd /root/bootstrap/latest || fail_userdata bootstrap-download\n`;

  return userdata + installCommands.map((c) => `${c}\n`).join('');
}
