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

from ideadatamodel import exceptions
from ideasdk.utils import Utils

import os
from typing import List, Dict, Optional


class BootstrapUserDataBuilder:
    """
    generic user data script builder for all ec2 instances launched by IDEA bootstrap framework
    todo: refactor implementation to use jinja2 based templates via SDK resources
    """

    def __init__(
        self,
        base_os: str,
        aws_region: str,
        bootstrap_package_uri: str,
        install_commands: List[str],
        infra_config: Optional[Dict] = None,
        proxy_config: Optional[Dict] = None,
        substitution_support: bool = True,
    ):
        self.base_os = base_os
        self.aws_region = aws_region
        self.bootstrap_package_uri = bootstrap_package_uri
        self.install_commands = install_commands
        self.infra_config = infra_config
        self.proxy_config = proxy_config
        self.substitution_support = substitution_support

    def build(self):
        if 'windows' in self.base_os.lower():
            if Utils.is_not_empty(self.infra_config):
                raise exceptions.general_exception(
                    'infra config is not supported for windows'
                )
            return self._build_windows_userdata()

        if self.substitution_support:
            return self._build_linux_userdata_substitution()
        return self._build_linux_userdata_non_substitution()

    def _build_windows_userdata(self) -> str:
        userdata = f"""
<powershell>
 $BootstrapDir = "C:\\Users\\Administrator\\IDEA\\bootstrap"
 function Download-Idea-Package {{
     Param(
     [ValidateNotNullOrEmpty()]
     [Parameter(Mandatory=$true)]
     [String] $PackageDownloadURI
     )
     if (!(Test-Path "$BootstrapDir")) {{
         New-Item -itemType Directory -Path "$BootstrapDir"
     }}
     cd "$BootstrapDir"
     Write-Output $PackageDownloadURI
     $PackageArchive=Split-Path $PackageDownloadURI -Leaf
     $PackageName = [System.IO.Path]::GetFileNameWithoutExtension($PackageDownloadURI)
     if ($PackageDownloadURI -like "s3`://*") {{
        $urlParts = $PackageDownloadURI -Split "/", 4
        $bucketName = $urlParts[2]
        $key = $urlParts[3]
        Copy-S3Object -BucketName $bucketName -Key $key -LocalFile "$BootstrapDir\\$PackageArchive" -Force
     }} else {{
        Copy-Item -Path $PackageDownloadURI -Destination "$BootstrapDir\\$PackageArchive"
     }}
     Tar -xf "$BootstrapDir\\$PackageArchive"
 }}
 Download-Idea-Package {self.bootstrap_package_uri}
"""
        for install_command in self.install_commands:
            userdata += f'{install_command}{os.linesep}'
        userdata += '</powershell>'
        return userdata

    def _build_linux_userdata_substitution(self) -> str:
        userdata = f"""#!/bin/bash

set -x
mkdir -p /root/bootstrap
AWS_REGION="{self.aws_region}"
BASE_OS="{self.base_os}"
BOOTSTRAP_FAILURE_TAG="{'idea:AmiBuilderStatus' if any('-ami-builder/setup.sh' in cmd for cmd in self.install_commands) else 'idea:BootstrapStatus'}"
DEFAULT_AWS_REGION="{self.aws_region}"
AWSCLI_X86_64_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
AWSCLI_AARCH64_URL="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"
"""

        infra_config_properties = ''
        if self.infra_config is not None:
            for key, value in self.infra_config.items():
                infra_config_properties += f'{key}={value}{os.linesep}'

        userdata += f"""
echo "
{infra_config_properties}
" > /root/bootstrap/infra.cfg
        """

        proxy_config_entries = ''
        if self.proxy_config is not None:
            for key, value in self.proxy_config.items():
                proxy_config_entries += f'export {key}={value}{os.linesep}'

        userdata += f'''
echo "{proxy_config_entries}
" > /root/bootstrap/proxy.cfg
source /root/bootstrap/proxy.cfg
        '''

        userdata += """

timestamp=$(date +%s)
mkdir -p /root/bootstrap/logs
if [[ -f /root/bootstrap/logs/userdata.log ]]; then
  mv /root/bootstrap/logs/userdata.log /root/bootstrap/logs/userdata.log.${!timestamp}
fi
exec > /root/bootstrap/logs/userdata.log 2>&1

export PATH="${!PATH}:/usr/local/bin"

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
    echo "Retrying $* in ${!delay}s (exit $status)"
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
  if [[ "${!BASE_OS}" == "amazonlinux2023" ]]; then
    retry_bootstrap yum remove -y awscli || return $?
  fi
  cd /root/bootstrap || return $?
  local machine=$(uname -m)
  if [[ ${!machine} == "x86_64" ]]; then
    curl -fSL --retry 3 --connect-timeout 10 --max-time 120 ${!AWSCLI_X86_64_URL} -o "awscliv2.zip" || return $?
    elif [[ ${!machine} == "aarch64" ]]; then
      curl -fSL --retry 3 --connect-timeout 10 --max-time 120 ${!AWSCLI_AARCH64_URL} -o "awscliv2.zip" || return $?
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
PACKAGE_DOWNLOAD_URI=\\${!1}
PACKAGE_ARCHIVE=\\$(basename \\${!PACKAGE_DOWNLOAD_URI})
PACKAGE_NAME=\\${!PACKAGE_ARCHIVE%.tar.gz*}
INSTANCE_REGION=\\$(TOKEN=\\$(curl --silent -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 900') && curl --silent -H \\"X-aws-ec2-metadata-token: \\${!TOKEN}\\" 'http://169.254.169.254/latest/meta-data/placement/region')
if [[ \\${!PACKAGE_DOWNLOAD_URI} == s3://* ]]; then
  AWS=\\$(command -v aws)
  S3_BUCKET=\\$(echo \\${!PACKAGE_DOWNLOAD_URI} | cut -f3 -d/)
  if [[ \\${!INSTANCE_REGION} =~ ^us-gov-[a-z]+-[0-9]+$ ]]; then
    S3_BUCKET_REGION=\\$(curl -s --head https://\\${!S3_BUCKET}.s3.us-gov-west-1.amazonaws.com | grep bucket-region | awk '{print \\$2}' | tr -d '\\r\\n')
    \\$AWS --region \\${!S3_BUCKET_REGION} s3 cp \\${!PACKAGE_DOWNLOAD_URI} /root/bootstrap/
  else
    #S3_BUCKET_REGION=\\$(curl -s --head https://\\${!S3_BUCKET}.s3.us-east-1.amazonaws.com | grep bucket-region | awk '{print \\$2}' | tr -d '\\r\\n')
    \\$AWS --region \\${!INSTANCE_REGION} s3 cp \\${!PACKAGE_DOWNLOAD_URI} /root/bootstrap/
  fi
else
  cp \\${!PACKAGE_DOWNLOAD_URI} /root/bootstrap/
fi
PACKAGE_DIR=/root/bootstrap/\\${!PACKAGE_NAME}
if [[ -d \\${!PACKAGE_DIR} ]]; then
  rm -rf \\${!PACKAGE_DIR}
fi
mkdir -p \\${!PACKAGE_DIR}
tar -xvf /root/bootstrap/\\${!PACKAGE_ARCHIVE} -C \\${!PACKAGE_DIR}
rm -f /root/bootstrap/latest
ln -sf \\${!PACKAGE_DIR} /root/bootstrap/latest
" > /root/bootstrap/download_bootstrap.sh

chmod +x /root/bootstrap/download_bootstrap.sh
        """

        userdata += f'''
install_aws_cli || fail_userdata aws-cli
bash /root/bootstrap/download_bootstrap.sh "{self.bootstrap_package_uri}" || fail_userdata bootstrap-download

cd /root/bootstrap/latest || fail_userdata bootstrap-download
'''
        for install_command in self.install_commands:
            userdata += f'{install_command}{os.linesep}'
        return userdata

    def _build_linux_userdata_non_substitution(self) -> str:
        userdata = f"""#!/bin/bash

set -x
mkdir -p /root/bootstrap
AWS_REGION="{self.aws_region}"
BASE_OS="{self.base_os}"
BOOTSTRAP_FAILURE_TAG="{'idea:AmiBuilderStatus' if any('-ami-builder/setup.sh' in cmd for cmd in self.install_commands) else 'idea:BootstrapStatus'}"
DEFAULT_AWS_REGION="{self.aws_region}"
AWSCLI_X86_64_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
AWSCLI_AARCH64_URL="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"
"""

        proxy_config_entries = ''
        if self.proxy_config is not None:
            for key, value in self.proxy_config.items():
                proxy_config_entries += f'export {key}={value}{os.linesep}'

        userdata += f'''
echo "{proxy_config_entries}
" > /root/bootstrap/proxy.cfg
source /root/bootstrap/proxy.cfg
        '''

        userdata += """

timestamp=$(date +%s)
mkdir -p /root/bootstrap/logs
if [[ -f /root/bootstrap/logs/userdata.log ]]; then
  mv /root/bootstrap/logs/userdata.log /root/bootstrap/logs/userdata.log.${timestamp}
fi
exec > /root/bootstrap/logs/userdata.log 2>&1

export PATH="${PATH}:/usr/local/bin"

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
    echo "Retrying $* in ${delay}s (exit $status)"
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
  if [[ "${BASE_OS}" == "amazonlinux2023" ]]; then
    retry_bootstrap yum remove -y awscli || return $?
  fi
  cd /root/bootstrap || return $?
  local machine=$(uname -m)
  if [[ ${machine} == "x86_64" ]]; then
    curl -fSL --retry 3 --connect-timeout 10 --max-time 120 ${AWSCLI_X86_64_URL} -o "awscliv2.zip" || return $?
    elif [[ ${machine} == "aarch64" ]]; then
      curl -fSL --retry 3 --connect-timeout 10 --max-time 120 ${AWSCLI_AARCH64_URL} -o "awscliv2.zip" || return $?
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
PACKAGE_DOWNLOAD_URI=\\${1}
PACKAGE_ARCHIVE=\\$(basename \\${PACKAGE_DOWNLOAD_URI})
PACKAGE_NAME=\\${PACKAGE_ARCHIVE%.tar.gz*}
INSTANCE_REGION=\\$(TOKEN=\\$(curl --silent -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 900') && curl --silent -H \\"X-aws-ec2-metadata-token: \\${TOKEN}\\" 'http://169.254.169.254/latest/meta-data/placement/region')
if [[ \\${PACKAGE_DOWNLOAD_URI} == s3://* ]]; then
  AWS=\\$(command -v aws)
  S3_BUCKET=\\$(echo \\${PACKAGE_DOWNLOAD_URI} | cut -f3 -d/)
  if [[ \\${INSTANCE_REGION} =~ ^us-gov-[a-z]+-[0-9]+$ ]]; then
    S3_BUCKET_REGION=\\$(curl -s --head https://\\${S3_BUCKET}.s3.us-gov-west-1.amazonaws.com | grep bucket-region | awk '{print \\$2}' | tr -d '\\r\\n')
    \\$AWS --region \\${S3_BUCKET_REGION} s3 cp \\${PACKAGE_DOWNLOAD_URI} /root/bootstrap/
  else
    #S3_BUCKET_REGION=\\$(curl -s --head https://\\${S3_BUCKET}.s3.us-east-1.amazonaws.com | grep bucket-region | awk '{print \\$2}' | tr -d '\\r\\n')
    \\$AWS --region \\${INSTANCE_REGION} s3 cp \\${PACKAGE_DOWNLOAD_URI} /root/bootstrap/
  fi
else
  cp \\${PACKAGE_DOWNLOAD_URI} /root/bootstrap/
fi
PACKAGE_DIR=/root/bootstrap/\\${PACKAGE_NAME}
if [[ -d \\${PACKAGE_DIR} ]]; then
  rm -rf \\${PACKAGE_DIR}
fi
mkdir -p \\${PACKAGE_DIR}
tar -xvf /root/bootstrap/\\${PACKAGE_ARCHIVE} -C \\${PACKAGE_DIR}
rm -f /root/bootstrap/latest
ln -sf \\${PACKAGE_DIR} /root/bootstrap/latest
" > /root/bootstrap/download_bootstrap.sh

chmod +x /root/bootstrap/download_bootstrap.sh
        """

        userdata += f'''
install_aws_cli || fail_userdata aws-cli
bash /root/bootstrap/download_bootstrap.sh "{self.bootstrap_package_uri}" || fail_userdata bootstrap-download

cd /root/bootstrap/latest || fail_userdata bootstrap-download
'''
        for install_command in self.install_commands:
            userdata += f'{install_command}{os.linesep}'
        return userdata
