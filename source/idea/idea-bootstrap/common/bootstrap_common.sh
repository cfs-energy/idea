#!/bin/bash
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

BOOTSTRAP_DIR="/root/bootstrap"

function exit_fail () {
  echo "Failed: ${1}"
  exit 1
}

function log_info() {
  echo "[$(date +"%Y-%m-%d %H:%M:%S,%3N")] [INFO] ${1}"
}

function log_warning() {
  echo "[$(date +"%Y-%m-%d %H:%M:%S,%3N")] [WARNING] ${1}"
}

function log_error() {
  echo "[$(date +"%Y-%m-%d %H:%M:%S,%3N")] [ERROR] ${1}"
}

function log_debug() {
  echo "[$(date +"%Y-%m-%d %H:%M:%S,%3N")] [DEBUG] ${1}"
}

function package_transaction() {
  local manager="$1"
  shift
  local output status delay
  # Bound slow network I/O inside the manager; killing an RPM/dpkg transaction is unsafe.
  case "$manager" in
    apt-get)
      set -- -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=2 "$@"
      ;;
    dnf|yum)
      set -- --setopt=timeout=30 --setopt=minrate=64k --setopt=retries=2 --setopt=max_parallel_downloads=10 "$@"
      ;;
  esac
  output=$(mktemp) || return $?
  # RHUI/mirrors can publish AppStream before its matching BaseOS dependencies.
  for delay in 15 30 60 0; do
    # Slow proxies must still be able to finish the final attempt.
    if [[ $delay -eq 0 && "$manager" != apt-get ]]; then
      set -- "$1" --setopt=minrate=0 "${@:3}"
    fi
    # Only final attempts may leave fatal-looking lines in the bake logs.
    if LC_ALL=C "$manager" "$@" > "$output" 2>&1; then
      status=0
    else
      status=$?
    fi
    if [[ $status -eq 0 ]]; then
      cat "$output"
      rm -f "$output"
      return 0
    fi
    # A package no repository carries is not transient; callers rely on that failing at once.
    # Optional missing packages can also precede a resolved transaction's download failure.
    if [[ "${PACKAGE_RETRY_MISSING:-no}" != yes ]] && grep -Eqi 'Unable to find a match|No match for argument' "$output" && ! grep -Eqi 'best candidate|nothing provides|Dependencies resolved' "$output"; then
      cat "$output"
      log_error "$manager transaction failed (exit $status)"
      rm -f "$output"
      return "$status"
    fi
    [[ $delay -ne 0 ]] || break
    sed 's/^/[retried] /' "$output"
    log_warning "$manager transaction failed (exit $status); refreshing metadata and retrying in ${delay}s"
    if [[ "$manager" != apt-get ]]; then
      "$manager" clean packages metadata 2>&1 | sed 's/^/[retried] /' || true
    fi
    sleep "$delay"
  done
  if [[ "${PACKAGE_RETRY_MISSING:-no}" != yes && "$manager" != apt-get ]] && grep -Eqi 'best candidate|nothing provides' "$output"; then
    sed 's/^/[retried] /' "$output"
    log_warning "$manager dependency resolution failed after retries; retrying once with --nobest"
    if LC_ALL=C "$manager" --nobest "$@" > "$output" 2>&1; then
      status=0
    else
      status=$?
    fi
  fi
  cat "$output"
  # Even a silent manager failure must be visible to the compute bake's log scan.
  [[ $status -eq 0 ]] || log_error "$manager transaction failed (exit $status)"
  rm -f "$output"
  return "$status"
}

# apt otherwise returns success when some indexes could not be downloaded.
function apt_update() {
  package_transaction apt-get -o APT::Update::Error-Mode=any -o DPkg::Lock::Timeout=300 update
}

# Exact packages can temporarily be missing while BaseOS and AppStream mirrors converge.
function install_matching_kernel_devel() {
  local kernel installed message
  kernel=$(uname -r)
  if PACKAGE_RETRY_MISSING=yes package_transaction dnf --refresh install -y "kernel-devel-${kernel}" \
      && [[ -f "${KERNEL_SRC_DIR:-/usr/src/kernels}/${kernel}/Makefile" ]]; then
    return 0
  fi
  installed=$(rpm -qa kernel-devel | tr '\n' ' ')
  message="Matching kernel-devel unavailable for running kernel ${kernel}; installed: ${installed:-none}. Cannot build kernel modules."
  log_error "${message}"
  if [[ -n "${BAKE_STAGE:-}" ]]; then
    printf '%s: %s\n' "${BAKE_STAGE}" "${message}" >> /var/lib/idea/bake-failed
  fi
  return 1
}

# A kernel without its initramfs does not boot: GRUB reports the file missing and the kernel panics
# on the root file system, and nothing can reach a host in that state. Anything can leave such a
# kernel as the default: a package update, a transaction that failed halfway, dracut failing on a
# full /boot with the transaction still exiting 0. So ensure_boot_kernel_bootable checks what the
# boot loader will actually boot, whoever selected it, before every bootstrap reboot and at the end
# of every bootstrap that does not reboot.
function kernel_initramfs_ready () {
  local version="${1}" boot="${KERNEL_BOOT_DIR:-/boot}"
  if command -v grubby > /dev/null 2>&1; then
    [[ -s "${boot}/initramfs-${version}.img" ]] && return 0
    log_warning "Kernel ${version} has no initramfs; building it"
    timeout 300 dracut -f "${boot}/initramfs-${version}.img" "${version}" || true
    [[ -s "${boot}/initramfs-${version}.img" ]]
  else
    [[ -s "${boot}/initrd.img-${version}" ]] && return 0
    log_warning "Kernel ${version} has no initrd; building it"
    timeout 300 update-initramfs -c -k "${version}" || true
    [[ -s "${boot}/initrd.img-${version}" ]]
  fi
}

# The kernel GRUB's generated menu selects, including saved entries and nested menu titles and ids.
function grub_default_kernel () {
  GRUB_DIR="${GRUB_DIR:-/boot/grub}" "${BAKE_PYTHON:-python3}" - <<'PY'
import os
import re
import shlex
import subprocess
from pathlib import Path

config = Path(f"{os.environ['GRUB_DIR']}/grub.cfg").read_text()
settings = dict(line.split('=', 1) for line in subprocess.check_output(
    ['grub-editenv', f"{os.environ['GRUB_DIR']}/grubenv", 'list'], text=True).splitlines() if '=' in line)
defaults = re.findall(r'^\s*set default="([^"]+)"', config, re.M)
# Stock GRUB selects next_entry first, then saved_entry or the configured literal.
selection = settings.get('next_entry') or next(
    (settings.get('saved_entry') if d == '${saved_entry}' else d
     for d in defaults if d != '${next_entry}'), None)
if not selection or '$' in selection:
    raise SystemExit('Cannot resolve default GRUB entry')
entries, stack, counts = [], [], {(): 0}
depth = 0
for line in config.splitlines():
    stripped = line.strip()
    words = stripped.split()
    if stripped.startswith(('menuentry ', 'submenu ')):
        parts = shlex.split(stripped)
        parent = tuple(item[0] for item in stack)
        index = counts.get(parent, 0)
        counts[parent] = index + 1
        ident = ''
        for i, part in enumerate(parts[:-1]):
            if part in ('--id', '$menuentry_id_option'):
                ident = parts[i + 1]
            elif part.startswith('--id='):
                ident = part[5:]
        stack.append((index, parts[1], ident, depth))
    # grub.cfg separates the command from its arguments with tabs
    if len(words) > 1 and words[0] in ('linux', 'linuxefi'):
        kernel = words[1].rsplit('/', 1)[-1]
        entries.append((list(stack), kernel.removeprefix('vmlinuz-')))
    depth += line.count('{') - line.count('}')
    while stack and depth <= stack[-1][3]:
        stack.pop()
for path, kernel in entries:
    keys = selection.split('>')
    if len(keys) == len(path) and all(k in (str(i), title, ident) for k, (i, title, ident, _) in zip(keys, path)):
        print(kernel)
        break
else:
    raise SystemExit('Default GRUB entry has no kernel')
PY
}

function boot_loader_default_kernel () {
  local kernel
  if command -v grubby > /dev/null 2>&1; then
    kernel=$(grubby --default-kernel 2>/dev/null) || return 1
    echo "${kernel##*/vmlinuz-}"
  else
    grub_default_kernel 2>/dev/null
  fi
}

function restore_default_kernel () {
  if command -v grubby > /dev/null 2>&1; then
    grubby --set-default "${KERNEL_BOOT_DIR:-/boot}/vmlinuz-${1}" || return 1
  else
    sed -i "s/^GRUB_DEFAULT=.*/GRUB_DEFAULT=\"Advanced options for Ubuntu>Ubuntu, with Linux ${1}\"/" "${GRUB_DEFAULTS_FILE:-/etc/default/grub}" || return 1
    update-grub || return 1
  fi
  [[ "$(boot_loader_default_kernel)" == "${1}" ]]
}

function ensure_boot_kernel_bootable () {
  local next running
  next=$(boot_loader_default_kernel) || next=""
  if [[ -z "${next}" ]]; then
    log_warning "Could not read which kernel this host boots next, so it was not checked"
    return 0
  fi
  kernel_initramfs_ready "${next}" && return 0
  running=$(uname -r)
  log_warning "Kernel ${next} has no initramfs and none could be built; keeping ${running}"
  if [[ "${next}" != "${running}" ]] && kernel_initramfs_ready "${running}" && restore_default_kernel "${running}"; then
    return 0
  fi
  echo -n "no" > "${BOOTSTRAP_DIR}/reboot_required.txt"
  fail_bootstrap kernel-boot-mismatch "Kernel ${next} would not boot: it has no initramfs, one could not be built, and the running kernel could not be made the default."
}

# /boot is a 1 GB partition on EL images and a generic desktop initramfs passed 260 MB. With a
# second kernel the rebuild a later package triggers (it writes a new copy beside the old one)
# ran out of room. A rescue image, one per machine id, has no use on an instance nobody can
# choose a boot entry on, so drop them and stop new ones being made.
function drop_rescue_images () {
  local boot="${KERNEL_BOOT_DIR:-/boot}"
  local conf="${DRACUT_CONF_DIR:-/etc/dracut.conf.d}"
  mkdir -p "${conf}"
  echo 'dracut_rescue_image="no"' > "${conf}/90-idea-no-rescue.conf"
  rm -f "${boot}"/*-0-rescue-* "${boot}"/.vmlinuz-0-rescue-* "${boot}"/loader/entries/*-0-rescue.conf
}

function set_reboot_required () {
  log_info "Reboot Required: ${1}"
  echo -n "yes" > ${BOOTSTRAP_DIR}/reboot_required.txt
  echo "$(date): ${1}" >> ${BOOTSTRAP_DIR}/reboot_history.log
}

function reboot_after_cloud_init () {
  # An independent worker can bound the wait; After=cloud-final cannot time out inside its command.
  # shellcheck disable=SC2016
  timeout 15 systemd-run --unit=idea-bootstrap-reboot --no-block --on-active=5s \
    --timer-property=AccuracySec=1s --property=StandardOutput=journal+console \
    --property=StandardError=journal+console \
    /bin/bash -c '[[ ! -f /etc/environment ]] || source /etc/environment; source "$1"; complete_bootstrap_reboot' \
    bash "${BASH_SOURCE[0]}" || fail_bootstrap reboot-failed "Unable to schedule bootstrap reboot"
}

function fail_bootstrap () {
  local status="${1}"
  shift
  log_error "${1}"
  mkdir -p "${BOOTSTRAP_DIR}"
  printf '%s\n' "${1}" > "${BOOTSTRAP_DIR}/bootstrap_failed.txt"
  # Failure reporting must not hang on metadata or AWS either, including under cron.
  set +x
  local token id region
  token=$(curl -fsS --connect-timeout 2 --max-time 5 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60') || true
  id=$(curl -fsS --connect-timeout 2 --max-time 5 -H "X-aws-ec2-metadata-token: ${token}" http://169.254.169.254/latest/meta-data/instance-id) || true
  region=${AWS_REGION:-${AWS_DEFAULT_REGION:-}}
  if [[ -z "${region}" ]]; then
    region=$(curl -fsS --connect-timeout 2 --max-time 5 -H "X-aws-ec2-metadata-token: ${token}" http://169.254.169.254/latest/meta-data/placement/region) || true
  fi
  if [[ -z "${id}" ]] || ! AWS_MAX_ATTEMPTS=1 timeout 30 aws --region "${region}" --cli-connect-timeout 5 --cli-read-timeout 10 \
      ec2 create-tags --resources "${id}" --tags "Key=idea:BootstrapStatus,Value=${status}"; then
    log_error "Unable to tag instance with idea:BootstrapStatus=${status}"
  fi
  exit 1
}

function complete_bootstrap_reboot () {
  local attempt state
  for ((attempt=0; attempt<60; attempt++)); do
    state=$(timeout 5 systemctl show cloud-final.service --property=SubState --property=Job) \
      || fail_bootstrap reboot-failed "Unable to read cloud-final state"
    # Cron can run before cloud-final starts: an inactive unit with a queued job is not finished.
    if grep -Eq '^SubState=(exited|dead|failed)$' <<< "${state}" && ! grep -Eq '^Job=[1-9]' <<< "${state}"; then
      ensure_boot_kernel_bootable
      log_info "cloud-final finished; requesting bootstrap reboot"
      timeout 30 /sbin/reboot || fail_bootstrap reboot-failed "Bootstrap reboot request failed"
      sleep 60
      fail_bootstrap reboot-failed "Bootstrap reboot did not shut down the node within 60 seconds"
    fi
    sleep 5
  done
  fail_bootstrap reboot-failed "Timed out waiting for cloud-final before bootstrap reboot"
}

function check_reboot_loop () {
  local max_reboots=10
  local reason="${1}"

  if [[ -f ${BOOTSTRAP_DIR}/reboot_history.log ]]; then
    local reboot_count
    reboot_count=$(wc -l < ${BOOTSTRAP_DIR}/reboot_history.log)
    if [[ $reboot_count -ge $max_reboots ]]; then
      log_error "INFINITE LOOP DETECTED: Too many reboots ($reboot_count). Last reason: ${reason}"
      log_error "Reboot history:"
      cat ${BOOTSTRAP_DIR}/reboot_history.log
      # Disable further reboots and exit
      echo -n "no" > ${BOOTSTRAP_DIR}/reboot_required.txt
      exit 1
    fi
  fi
}

function get_reboot_required () {
  if [[ -f ${BOOTSTRAP_DIR}/reboot_required.txt ]]; then
    cat ${BOOTSTRAP_DIR}/reboot_required.txt
  fi
  echo -n "no"
}

function imds_get () {
  local SLASH=''
  local IMDS_HOST="http://169.254.169.254"
  local IMDS_TTL="300"
  # prepend a slash if needed
  if [[ "${1:0:1}" == '/' ]]; then
    SLASH=''
  else
    SLASH='/'
  fi
  local URL="${IMDS_HOST}${SLASH}${1}"

  # Get an Auth token
  local TOKEN
  TOKEN=$(curl --silent -X PUT "${IMDS_HOST}/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: ${IMDS_TTL}")

  # Get the requested value and echo it back
  local OUTPUT
  OUTPUT=$(curl --silent -H "X-aws-ec2-metadata-token: ${TOKEN}" "${URL}")
  echo -n "${OUTPUT}"
}

function instance_type () {
  local INSTANCE_TYPE
  INSTANCE_TYPE=$(imds_get /latest/meta-data/instance-type)
  echo -n "${INSTANCE_TYPE}"
}

function instance_family () {
  local INSTANCE_FAMILY
  INSTANCE_FAMILY=$(imds_get /latest/meta-data/instance-type | cut -d. -f1)
  echo -n "${INSTANCE_FAMILY}"
}

function instance_id () {
  local INSTANCE_ID
  INSTANCE_ID=$(imds_get /latest/meta-data/instance-id)
  echo -n "${INSTANCE_ID}"
}

function instance_region () {
  local INSTANCE_REGION
  INSTANCE_REGION=$(imds_get /latest/meta-data/placement/region)
  echo -n "${INSTANCE_REGION}"
}

function get_secret() {
    local SECRET_ID="${1}"
    local MAX_ATTEMPT=10
    local CURRENT_ATTEMPT=0
    local SLEEP_INTERVAL=180
    local AWS
    AWS=$(which aws)
    local command="${AWS} secretsmanager get-secret-value --secret-id ${SECRET_ID} --query SecretString --region ${AWS_DEFAULT_REGION} --output text"
    while ! secret=$($command); do
        ((CURRENT_ATTEMPT=CURRENT_ATTEMPT+1))
        if [[ ${CURRENT_ATTEMPT} -ge ${MAX_ATTEMPT} ]]; then
            echo "error: Timed out waiting for secret from secrets manager"
            return 1
        fi
        echo "Secret Manager is not ready yet ... Waiting ${SLEEP_INTERVAL} s... Loop count is: ${CURRENT_ATTEMPT}/${MAX_ATTEMPT}"
        sleep ${SLEEP_INTERVAL}
    done
    echo -n "${secret}"
}

function get_server_ip() {
  # hostname -I prints every local address on one whitespace-separated line, so the split
  # has to be on words. SERVER_IP is the first IPv4; PBS does not accept IPv6 here.
  local SERVER_IP_ARRAY
  read -r -a SERVER_IP_ARRAY <<< "$(hostname -I)"
  local SERVER_IP=""
  local IPV4_REGEX="^(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
  for ip in "${SERVER_IP_ARRAY[@]}"; do
      # anchored, so an IPv6 address carrying an embedded IPv4 is not accepted
      if [[ -z "${SERVER_IP}" ]] && echo "${ip}" | grep -Eq "${IPV4_REGEX}"; then
          SERVER_IP="${ip}"
      fi
  done
  echo "${SERVER_IP}"
}

# Generate a Shake256 value from the input
# fsx for lustre
function add_fsx_lustre_to_fstab () {
  local FS_DOMAIN="${1}"
  local MOUNT_DIR="${2}"
  local MOUNT_OPTIONS=${3}
  local FS_MOUNT_NAME="${4}"

  if [[ -z "${MOUNT_OPTIONS}" ]]; then
    MOUNT_OPTIONS="lustre defaults,noatime,flock,_netdev 0 0"
  fi

  # handle cases for scratch file systems during SOCA job submission
  if [[ -z "${FS_MOUNT_NAME}" ]]; then
    local FSX_ID
    FSX_ID=$(echo "${FS_DOMAIN}" | cut -d. -f1)
    local AWS
    AWS=$(command -v aws)
    FS_MOUNT_NAME=$($AWS fsx describe-file-systems \
                              --file-system-ids "${FSX_ID}"  \
                              --query FileSystems[].LustreConfiguration.MountName \
                              --region "${AWS_DEFAULT_REGION}" \
                              --output text)
  fi

  local NEW_ENTRY="${FS_DOMAIN}@tcp:/${FS_MOUNT_NAME} ${MOUNT_DIR}/ ${MOUNT_OPTIONS}"

  # Check if exact same entry already exists
  if grep -qF "${NEW_ENTRY}" /etc/fstab; then
    log_info "skip add_fsx_lustre_to_fstab: identical entry already exists for ${MOUNT_DIR}"
    return 0
  fi

  # Check if different entry exists for same mount directory
  if grep -q " ${MOUNT_DIR}/" /etc/fstab; then
    log_info "Updating existing fsx_lustre fstab entry for mount dir: ${MOUNT_DIR}"
    # Remove existing entry and add new one
    remove_fsx_lustre_from_fstab "${MOUNT_DIR}"
  fi

  echo "${NEW_ENTRY}" >> /etc/fstab
}

function remove_fsx_lustre_from_fstab () {
  local MOUNT_DIR="${1}"
  sed -i.bak "\@ ${MOUNT_DIR}/@d" /etc/fstab
}

# efs
function add_efs_to_fstab () {
  local FS_DOMAIN="${1}"
  local MOUNT_DIR="${2}"
  local MOUNT_OPTIONS=${3}

  # Fallback to NFS options for EFS
  if [[ -z "${MOUNT_OPTIONS}" ]]; then
    log_info "# INFO - Using Fallback NFS options for EFS due to no mount_options"
    MOUNT_OPTIONS="nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0"
  fi

  local NEW_ENTRY="${FS_DOMAIN}:/ ${MOUNT_DIR}/ ${MOUNT_OPTIONS}"

  # Check if exact same entry already exists
  if grep -qF "${NEW_ENTRY}" /etc/fstab; then
    log_info "skip add_efs_to_fstab: identical entry already exists for ${MOUNT_DIR}"
    return 0
  fi

  # Check if different entry exists for same mount directory
  if grep -q " ${MOUNT_DIR}/" /etc/fstab; then
    log_info "Updating existing efs fstab entry for mount dir: ${MOUNT_DIR}"
    # Remove existing entry and add new one
    remove_efs_from_fstab "${MOUNT_DIR}"
  fi

  echo "${NEW_ENTRY}" >> /etc/fstab
}

function remove_efs_from_fstab () {
  local MOUNT_DIR="${1}"
  sed -i.bak "\@ ${MOUNT_DIR}/@d" /etc/fstab
}

# openzfs
function add_fsx_openzfs_to_fstab () {
  local FS_DOMAIN="${1}"
  local MOUNT_DIR="${2}"
  local MOUNT_OPTIONS=${3}
  local FS_VOLUME_PATH="${4}"

  if [[ -z "${MOUNT_OPTIONS}" ]]; then
    MOUNT_OPTIONS="nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0"
  fi

  local NEW_ENTRY="${FS_DOMAIN}:${FS_VOLUME_PATH} ${MOUNT_DIR}/ ${MOUNT_OPTIONS}"

  # Check if exact same entry already exists
  if grep -qF "${NEW_ENTRY}" /etc/fstab; then
    log_info "skip add_openzfs_to_fstab: identical entry already exists for ${MOUNT_DIR}"
    return 0
  fi

  # Check if different entry exists for same mount directory
  if grep -q " ${MOUNT_DIR}/" /etc/fstab; then
    log_info "Updating existing openzfs fstab entry for mount dir: ${MOUNT_DIR}"
    # Remove existing entry and add new one
    remove_fsx_openzfs_from_fstab "${MOUNT_DIR}"
  fi

  # eg. filesystem-dns-name:volume-path /localpath nfs nfsver=version defaults 0 0
  echo "${NEW_ENTRY}" >> /etc/fstab
}

function remove_fsx_openzfs_from_fstab () {
  local MOUNT_DIR="${1}"
  sed -i.bak "\@ ${MOUNT_DIR}/@d" /etc/fstab
}

# netapp ontap
function add_fsx_netapp_ontap_to_fstab () {
  local FS_DOMAIN="${1}"
  local MOUNT_DIR="${2}"
  local MOUNT_OPTIONS=${3}
  local FS_VOLUME_PATH="${4}"

  if [[ -z "${MOUNT_OPTIONS}" ]]; then
    MOUNT_OPTIONS="nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0"
  fi

  local NEW_ENTRY="${FS_DOMAIN}:${FS_VOLUME_PATH} ${MOUNT_DIR}/ ${MOUNT_OPTIONS}"

  # Check if exact same entry already exists
  if grep -qF "${NEW_ENTRY}" /etc/fstab; then
    log_info "skip add_netapp_ontap_to_fstab: identical entry already exists for ${MOUNT_DIR}"
    return 0
  fi

  # Check if different entry exists for same mount directory
  if grep -q " ${MOUNT_DIR}/" /etc/fstab; then
    log_info "Updating existing netapp ontap fstab entry for mount dir: ${MOUNT_DIR}"
    # Remove existing entry and add new one
    remove_fsx_netapp_ontap_from_fstab "${MOUNT_DIR}"
  fi

  # eg. svm-dns-name:volume-junction-path /fsx nfs nfsvers=version,defaults 0 0
  echo "${NEW_ENTRY}" >> /etc/fstab
}

function remove_fsx_netapp_ontap_from_fstab () {
  local MOUNT_DIR="${1}"
  sed -i.bak "\@ ${MOUNT_DIR}/@d" /etc/fstab
}

function create_jq_ddb_filter () {
  echo "
def convert_from_dynamodb_object:
    def get_property(\$key): select(keys == [\$key])[\$key];
       ((objects | { value: get_property(\"S\") })
    // (objects | { value: get_property(\"N\") | tonumber })
    // (objects | { value: get_property(\"B\") })
    // (objects | { value: get_property(\"M\") | map_values(convert_from_dynamodb_object) })
    // (objects | { value: get_property(\"L\") | map(convert_from_dynamodb_object) })
    // (objects | { value: get_property(\"SS\") })
    // (objects | { value: get_property(\"NS\") | map(tonumber) })
    // (objects | { value: get_property(\"BOOL\") })
    // (objects | { value: get_property(\"BS\") })
    // (objects | { value: map_values(convert_from_dynamodb_object) })
    // (arrays | { value: map(convert_from_dynamodb_object) })
    // { value: . }).value
    ;
convert_from_dynamodb_object
" > /root/.convert_from_dynamodb_object.jq
}

# mount -a's status says nothing useful: util-linux returns 32 for "already mounted" and for
# every real failure alike. The check is that each shared filesystem the bootstrap put in fstab
# (its helpers write the mount point with a trailing slash) is a mount point afterwards.
# Arguments go to mount -a (an Ubuntu bake passes -t nolustre until it boots a kernel with
# the Lustre module; the Lustre entry is not in fstab yet, so it is not expected either).
function is_cluster_mount_dir () {
  # IDEA_SHARED_MOUNT_DIRS is rendered from the cluster's shared storage; without it, every entry counts
  declare -p IDEA_SHARED_MOUNT_DIRS >/dev/null 2>&1 || return 0
  local dir
  for dir in "${IDEA_SHARED_MOUNT_DIRS[@]}"; do
    [[ "${dir%/}" != "${1%/}" ]] || return 0
  done
  return 1
}

function mount_shared_filesystems () {
  local mount_dir
  local not_mounted=()
  mount -a "$@" || true
  while read -r mount_dir; do
    if ! is_cluster_mount_dir "${mount_dir}"; then
      log_warning "Ignoring fstab entry ${mount_dir}: not shared storage of this cluster"
      continue
    fi
    mountpoint -q "${mount_dir}" || not_mounted+=("${mount_dir}")
  done < <(awk '$1 !~ /^#/ && $2 ~ /.\/$/ {print $2}' /etc/fstab)
  if [[ ${#not_mounted[@]} -gt 0 ]]; then
    log_error "Shared storage not mounted: ${not_mounted[*]}"
  fi
  # the last command is the one a bake records, so it says what failed
  [[ ${#not_mounted[@]} -eq 0 ]]
}

function remove_from_fstab () {
  # drop every fstab entry mounted at a directory (with or without a trailing slash)
  local MOUNT_DIR="$1"
  [[ -n "${MOUNT_DIR}" ]] || return 0
  sed -i "\| ${MOUNT_DIR}/\{0,1\} |d" /etc/fstab
}
