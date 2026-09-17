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

function install_aws_cli () {
  if [[ "\${!BASE_OS}" == "amazonlinux2023" ]]; then
    yum remove -y awscli
  fi
  cd /root/bootstrap
  local machine=$(uname -m)
  if [[ \${!machine} == "x86_64" ]]; then
    curl -s \${!AWSCLI_X86_64_URL} -o "awscliv2.zip"
    elif [[ \${!machine} == "aarch64" ]]; then
      curl -s \${!AWSCLI_AARCH64_URL} -o "awscliv2.zip"
  fi
  which unzip > /dev/null 2>&1
  if [[ "$?" != "0" ]]; then
    if [[ $BASE_OS =~ ^ubuntu ]]; then
      apt install -y unzip
    else
      yum install -y unzip
    fi
  fi
  unzip -q awscliv2.zip
  ./aws/install --bin-dir /bin --update
  rm -rf aws awscliv2.zip
}

echo "#!/bin/bash
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
rm /root/bootstrap/latest
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
    `\ninstall_aws_cli\n` +
    `bash /root/bootstrap/download_bootstrap.sh "${bootstrapPackageUri}"\n` +
    `\n` +
    `cd /root/bootstrap/latest\n`;

  return userdata + installCommands.map((c) => `${c}\n`).join('');
}
