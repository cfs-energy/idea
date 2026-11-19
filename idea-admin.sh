#!/bin/bash

# Run the control-plane container.
#
# Usage:
# ./idea-admin.sh --help
#
# Environment Variables:
# * IDEA_REVISION - Use to override the default IDEA version.
# * IDEA_DOCKER_REPO - Use to override the default Docker/ECR repository.
# * IDEA_IMAGE_DIGEST - The sha256 digest of the image to run; the image always runs by digest.
#                         The idea-admin.sh attached to a release already carries its digest. A
#                         copy from a checkout uses a copy of <repo>:<revision> pulled earlier, or
#                         reads the digest from the release's candidate.json on GitHub. Set it on a
#                         host that can reach neither, from the "image" field of candidate.json.
# * IDEA_LOCAL_IMAGE - Set to "true" to run a local build tagged idea-control-plane:<revision>
#                         instead of the released image. A local image is never used otherwise.
# * IDEA_ECR_CREDS_RESET - Set to false, if you handle AWS ECR authentication manually.
# * IDEA_ADMIN_AWS_CREDENTIAL_PROVIDER - Set to "Ec2InstanceMetadata", if you want install IDEA from an EC2 Instance
#                         using Instance Profile credentials from EC2 Instance Metadata.
# * IDEA_ADMIN_ENABLE_CDK_NAG_SCAN - Set to "true" to run the cdk-nag scan. Default: false
# * IDEA_DEV_MODE - Set to "true" to build and run the deploy tool from source instead of the
#                         control-plane container. Requires Node.js and a checkout.
# * IDEA_ADMIN_NO_TTY - Set to "true" to drop docker's -t flag even when stdin is a
#                         terminal. Non-interactive stdin (ssm, cron, CI) is detected
#                         automatically. Combine with --force for an unattended run.

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
IDEA_REVISION_CHOSEN=${IDEA_REVISION:+true}
IDEA_REVISION=${IDEA_REVISION:-"v26.11.2"}
# Filled in with this release's image digest in the idea-admin.sh attached to the release.
IDEA_IMAGE_DIGEST_DEFAULT=""
# The release an image holds: ideactl's copy of IDEA_VERSION.txt, kept in every image since 26.10.0
# (the build's /idea-build copy is removed in the same stage).
IDEA_IMAGE_VERSION_FILE="/opt/idea/ideactl/dist/IDEA_VERSION.txt"
IDEA_DOCKER_REPO_DEFAULT="public.ecr.aws/s5o2b4m0/idea-control-plane"
IDEA_DOCKER_REPO=${IDEA_DOCKER_REPO:-"${IDEA_DOCKER_REPO_DEFAULT}"}
IDEA_RELEASES_URL="https://github.com/cfs-energy/idea/releases/download"
IDEA_IMAGE_DIGEST=${IDEA_IMAGE_DIGEST:-""}
IDEA_LOCAL_IMAGE=${IDEA_LOCAL_IMAGE:-"false"}
IDEA_ECR_CREDS_RESET=${IDEA_ECR_CREDS_RESET:-"true"}
IDEA_ADMIN_AWS_CREDENTIAL_PROVIDER=${IDEA_ADMIN_AWS_CREDENTIAL_PROVIDER:=""}
IDEA_ADMIN_ENABLE_CDK_NAG_SCAN=${IDEA_ADMIN_ENABLE_CDK_NAG_SCAN:-"false"}

DOCUMENTATION_ERROR="https://docs.idea-hpc.com"
NC="\033[0m" # No Color
RED="\033[1;31m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"

verify_command() {
  # shellcheck disable=SC2181
  if [[ "$?" -ne "0" ]]; then
    echo -e "${RED}[MESSAGE]: ${1} \n[HELP]: Refer to ${DOCUMENTATION_ERROR} for troubleshooting.${NC}"
    exit 1
  fi
}

if [[ "${IDEA_DEV_MODE}" == "true" ]]; then
  if [[ ! -f ${SCRIPT_DIR}/IDEA_VERSION.txt ]]; then
    echo -e "${RED}idea-admin.sh must be executed from IDEA project root directory when using developer mode."
    exit 1
  fi
  IDEACTL_DIR="${SCRIPT_DIR}/source/idea/ideactl"
  command -v node > /dev/null
  verify_command "Node.js not detected. Install the version in software_versions.yml to run idea-admin.sh in developer mode."
  if [[ ! -d "${IDEACTL_DIR}/node_modules" ]]; then
    echo -e "${RED}Dependencies not installed. Run 'npm ci' in ${IDEACTL_DIR} first.${NC}"
    exit 1
  fi
  # Build quietly so the tool's own output is the only thing on stdout.
  if ! BUILD_LOG=$(cd "${IDEACTL_DIR}" && npm run --silent build 2>&1); then
    echo -e "${RED}Build failed in ${IDEACTL_DIR}:${NC}"
    echo "${BUILD_LOG}"
    exit 1
  fi

  export IDEA_SKIP_WEB_BUILD=${IDEA_SKIP_WEB_BUILD:-'0'}
  export IDEA_ADMIN_AWS_CREDENTIAL_PROVIDER
  export IDEA_ADMIN_ENABLE_CDK_NAG_SCAN
  export AWS_SDK_LOAD_CONFIG=1
  exec node "${IDEACTL_DIR}/dist/src/cli/main.js" "${@}"
fi

cd "${SCRIPT_DIR}" || exit

# Check if Docker is installed
DOCKER_BIN=$(command -v docker)
verify_command "Docker not detected. Download and install it from https://docs.docker.com/get-docker/. Read the Docker Subscription Service Agreement first (https://www.docker.com/legal/docker-subscription-service-agreement/)."
echo -e "${GREEN}✓ Docker detected${NC}"

# Check if aws cli (https://aws.amazon.com/cli/) is installed
command -v aws > /dev/null
verify_command "awscli not detected. Download and install it from https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
echo -e "${GREEN}✓ AWS CLI detected${NC}"

# SSO profiles cannot authenticate inside the container.
if [[ -n "${AWS_PROFILE}" ]] && \
   { aws configure get sso_session --profile "${AWS_PROFILE}" > /dev/null 2>&1 || \
     aws configure get sso_start_url --profile "${AWS_PROFILE}" > /dev/null 2>&1; }; then
  echo -e "${RED}[MESSAGE]: AWS_PROFILE=${AWS_PROFILE} is an SSO profile, which cannot be used inside the container."
  echo -e "[HELP]: Export static credentials on the host and unset AWS_PROFILE, for example:"
  echo -e "  eval \$(aws configure export-credentials --profile ${AWS_PROFILE} --format env)"
  echo -e "  unset AWS_PROFILE${NC}"
  exit 1
fi

# Create folder hierarchy
MKDIR_BIN=$(command -v mkdir)
${MKDIR_BIN} -p "${HOME}"/.idea/clusters
verify_command "Unable to create ${HOME}/.idea/clusters. Verify path and permissions."
echo -e "${GREEN}✓ Created directory structure${NC}"

# Check if Docker is running
${DOCKER_BIN} info >> /dev/null 2>&1
verify_command "Docker is installed on the system but it does not seems to be running. Start Docker first."
echo -e "${GREEN}✓ Docker is running${NC}"

# The image gets the operator's AWS credentials, so it is chosen by digest, never by a tag that
# the registry can move. A local build runs only when asked for by name.
reset_ecr_credentials() {
  if [[ "${IDEA_ECR_CREDS_RESET}" != "true" || "${IDEA_DOCKER_REPO}" != *"/"* ]]; then
    echo -e "${YELLOW}[INFO] Skipping ECR credentials reset (IDEA_ECR_CREDS_RESET=false)${NC}"
    return
  fi
  echo -e "${YELLOW}[INFO] Resetting ECR credentials...${NC}"
  DIG_BIN=$(command -v dig)
  IDEA_DOCKER_REPO_HOSTNAME=$(echo "${IDEA_DOCKER_REPO}" | cut -d '/' -f 1)
  if [[ -z "${DIG_BIN}" ]]; then
    # dig ships in bind-utils, which a stock Amazon Linux 2023 host does not have. The reset is a
    # convenience, so skip it rather than fail the command that was asked for.
    echo -e "${YELLOW}[INFO] dig not found: skipping ECR credentials reset. Install bind-utils, or set IDEA_ECR_CREDS_RESET=false to skip this step without the warning.${NC}"
    return
  fi
  ${DIG_BIN} +tries=1 +time=3 "${IDEA_DOCKER_REPO_HOSTNAME}" >> /dev/null 2>&1
  verify_command "Unable to query ECR host ${IDEA_DOCKER_REPO_HOSTNAME} . Are you connected to internet?"

  ${DOCKER_BIN} logout public.ecr.aws >> /dev/null 2>&1
  verify_command "Failed to refresh ECR credentials. docker logout public.ecr.aws failed"
  echo -e "${GREEN}✓ ECR credentials reset${NC}"
}

IDEA_RELEASE_TAG_REF="${IDEA_DOCKER_REPO}:${IDEA_REVISION}"
if [[ "${IDEA_LOCAL_IMAGE}" == "true" ]]; then
  IDEA_IMAGE="idea-control-plane:${IDEA_REVISION}"
  ${DOCKER_BIN} image inspect "${IDEA_IMAGE}" >> /dev/null 2>&1
  verify_command "IDEA_LOCAL_IMAGE=true but no local image ${IDEA_IMAGE} exists. Build it first, or unset IDEA_LOCAL_IMAGE to run the released image."
  echo -e "${YELLOW}[INFO] Using the local build ${IDEA_IMAGE} (IDEA_LOCAL_IMAGE=true)${NC}"
else
  IDEA_IMAGE_SOURCE="IDEA_IMAGE_DIGEST"
  if [[ -z "${IDEA_IMAGE_DIGEST}" && -z "${IDEA_REVISION_CHOSEN}" && -n "${IDEA_IMAGE_DIGEST_DEFAULT}" ]]; then
    IDEA_IMAGE_DIGEST="${IDEA_IMAGE_DIGEST_DEFAULT}"
    IDEA_IMAGE_SOURCE="this release's idea-admin.sh"
  fi
  IDEA_TAG_ONLY_RELEASE=""
  IDEA_RECORD_URL=""
  if [[ -z "${IDEA_IMAGE_DIGEST}" ]]; then
    # A copy of <repo>:<revision> pulled earlier carries its registry digest; one built or
    # loaded locally carries none. It is compared with the release record below, because a
    # candidate image reports the same release and must not stand in for it.
    # Matched in bash: older mawk (Ubuntu 22.04, Debian) has no {n} intervals.
    IDEA_LOCAL_DIGEST=""
    while IFS= read -r IDEA_REPO_DIGEST; do
      if [[ "${IDEA_REPO_DIGEST}" == "${IDEA_DOCKER_REPO}@"* && "${IDEA_REPO_DIGEST#"${IDEA_DOCKER_REPO}@"}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        IDEA_LOCAL_DIGEST="${IDEA_REPO_DIGEST#"${IDEA_DOCKER_REPO}@"}"
        break
      fi
    done < <(${DOCKER_BIN} image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${IDEA_RELEASE_TAG_REF}" 2>/dev/null)
    # candidate.json is the release's record of the image it was built and proven with.
    IDEA_RECORD_URL="${IDEA_RELEASES_URL}/v${IDEA_REVISION#v}/candidate.json"
    IDEA_RECORD=$(mktemp)
    IDEA_RECORD_STATUS=$(curl --silent --show-error --location --proto '=https' --max-time 30 \
      --output "${IDEA_RECORD}" --write-out '%{http_code}' "${IDEA_RECORD_URL}") || IDEA_RECORD_STATUS=000
    IDEA_RELEASE_IMAGE=$(tr -d '\n' < "${IDEA_RECORD}" | grep -oE '"image"[[:space:]]*:[[:space:]]*"[^"]+"' | head -n 1 | sed -E 's/.*"([^"]+)"$/\1/')
    rm -f "${IDEA_RECORD}"
    if [[ "${IDEA_RECORD_STATUS}" == 200 && "${IDEA_RELEASE_IMAGE%@*}" == "${IDEA_DOCKER_REPO_DEFAULT}" ]]; then
      IDEA_IMAGE_DIGEST="${IDEA_RELEASE_IMAGE#*@}"
    fi
    IDEA_IMAGE_SOURCE="${IDEA_RECORD_URL}"
    if [[ -n "${IDEA_LOCAL_DIGEST}" && -n "${IDEA_IMAGE_DIGEST}" && "${IDEA_LOCAL_DIGEST}" != "${IDEA_IMAGE_DIGEST}" ]]; then
      echo -e "${RED}[MESSAGE]: The local tag ${IDEA_RELEASE_TAG_REF} names ${IDEA_LOCAL_DIGEST}, but the release record ${IDEA_RECORD_URL} names ${IDEA_IMAGE_DIGEST}: the local image is an earlier candidate or another build, not this release."
      echo -e "[HELP]: Remove the local tag and run again: docker image rm ${IDEA_RELEASE_TAG_REF}${NC}"
      exit 1
    fi
    if [[ -n "${IDEA_LOCAL_DIGEST}" && -z "${IDEA_IMAGE_DIGEST}" && "${IDEA_RECORD_STATUS}" != 200 && "${IDEA_RECORD_STATUS}" != 404 ]]; then
      # No route to the record (an offline host): the local copy is the only image there is.
      # It is used, but not silently: it cannot be compared with the record here.
      IDEA_IMAGE_DIGEST="${IDEA_LOCAL_DIGEST}"
      IDEA_IMAGE_SOURCE="the local copy of ${IDEA_RELEASE_TAG_REF}"
      echo -e "${YELLOW}[WARNING] ${IDEA_RECORD_URL} could not be read, so the local copy of ${IDEA_RELEASE_TAG_REF} (${IDEA_LOCAL_DIGEST}) is used unchecked. Compare that digest with the \"image\" field of candidate.json on the release page, or set IDEA_IMAGE_DIGEST to the release's digest to skip this.${NC}"
    fi
    # Releases before 26.10.4 published no candidate.json; they are run by the tag they
    # always used, and from then on by the digest that tag named when it was pulled.
    IDEA_REVISION_PARTS=$(echo "${IDEA_REVISION#v}" | sed -nE 's/^([0-9]+)\.([0-9]+)\.([0-9]+)$/\1 \2 \3/p')
    if [[ -z "${IDEA_IMAGE_DIGEST}" && "${IDEA_RECORD_STATUS}" == 404 && -n "${IDEA_REVISION_PARTS}" ]]; then
      read -r IDEA_REV_YEAR IDEA_REV_MONTH IDEA_REV_PATCH <<< "${IDEA_REVISION_PARTS}"
      # 10#: a zero-padded month such as 08 is not read as octal
      if (( 10#${IDEA_REV_YEAR} * 10000 + 10#${IDEA_REV_MONTH} * 100 + 10#${IDEA_REV_PATCH} < 261004 )); then
        IDEA_TAG_ONLY_RELEASE="${IDEA_RELEASE_TAG_REF}"
      fi
    fi
    if [[ -z "${IDEA_TAG_ONLY_RELEASE}" && ! "${IDEA_IMAGE_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo -e "${RED}[MESSAGE]: Could not choose the image for ${IDEA_REVISION}: ${IDEA_RECORD_URL} could not be read, and no copy of ${IDEA_RELEASE_TAG_REF} pulled from that registry is present locally."
      echo -e "[HELP]: Set IDEA_IMAGE_DIGEST to the digest of the release image and run again, for example IDEA_IMAGE_DIGEST=sha256:<64 hex digits> ./idea-admin.sh ... . The value is the part after \"@\" in the \"image\" field of candidate.json on the release page, https://github.com/cfs-energy/idea/releases/tag/v${IDEA_REVISION#v}, which any machine with a browser can open; the idea-admin.sh attached to that release already carries it. For a release without candidate.json, use the digest of ${IDEA_DOCKER_REPO_DEFAULT}:${IDEA_REVISION} from your registry. Also set IDEA_DOCKER_REPO for a mirror. Offline hosts: see \"Mirrored registries and offline hosts\" in the upgrade guide.${NC}"
      exit 1
    fi
  fi
  if [[ -n "${IDEA_TAG_ONLY_RELEASE}" ]]; then
    IDEA_IMAGE="${IDEA_TAG_ONLY_RELEASE}"
    echo -e "${YELLOW}[INFO] ${IDEA_REVISION} predates the release image record; running ${IDEA_IMAGE} by its tag, as that release did${NC}"
  elif [[ ! "${IDEA_IMAGE_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo -e "${RED}[MESSAGE]: The image digest from ${IDEA_IMAGE_SOURCE} is not sha256:<64 hex digits>: '${IDEA_IMAGE_DIGEST}'.${NC}"
    exit 1
  else
    IDEA_IMAGE="${IDEA_DOCKER_REPO}@${IDEA_IMAGE_DIGEST}"
    echo -e "${YELLOW}[INFO] Image digest from ${IDEA_IMAGE_SOURCE}${NC}"
  fi
fi

# Pull IDEA docker image if needed; a local image needs no network.
if ! ${DOCKER_BIN} image inspect "${IDEA_IMAGE}" >> /dev/null 2>&1; then
  reset_ecr_credentials
  echo -e "${YELLOW}[INFO] Pulling IDEA Docker image: ${IDEA_IMAGE}${NC}"
  ${DOCKER_BIN} pull "${IDEA_IMAGE}"
  verify_command "Unable to download IDEA container image ${IDEA_IMAGE}. Refer to the error above. A mirror must hold the release image under the same digest (copy it with its digest preserved), or set IDEA_IMAGE_DIGEST to the mirror's digest."
  echo -e "${GREEN}✓ Docker image downloaded${NC}"
else
  echo -e "${GREEN}✓ Docker image already available${NC}"
fi
if [[ "${IDEA_LOCAL_IMAGE}" != "true" ]]; then
  # The image must be the release asked for: a digest set for one release (a mirror's, say) and
  # left in the environment would otherwise run under another release's IDEA_REVISION. A release
  # run by its tag (before 26.10.4) has no digest to be wrong about and is not checked.
  if [[ -z "${IDEA_TAG_ONLY_RELEASE:-}" ]]; then
    IDEA_IMAGE_VERSION=$(${DOCKER_BIN} run --rm --network none --entrypoint cat "${IDEA_IMAGE}" "${IDEA_IMAGE_VERSION_FILE}" 2>/dev/null | tr -d '[:space:]')
    # a candidate's image (v<version>-rc.N) holds the release it would become
    IDEA_REVISION_RELEASE="${IDEA_REVISION#v}"
    if [[ "${IDEA_IMAGE_VERSION}" != "${IDEA_REVISION_RELEASE%%-rc.*}" ]]; then
      echo -e "${RED}[MESSAGE]: ${IDEA_IMAGE} is release '${IDEA_IMAGE_VERSION:-unknown}', not ${IDEA_REVISION} (IDEA_REVISION). Its digest came from ${IDEA_IMAGE_SOURCE}."
      if [[ "${IDEA_IMAGE_SOURCE}" == "the local copy of"* ]]; then
        echo -e "[HELP]: The local tag ${IDEA_RELEASE_TAG_REF} names another release's image. Remove it (docker image rm ${IDEA_RELEASE_TAG_REF}) and run again.${NC}"
      else
        echo -e "[HELP]: Set IDEA_IMAGE_DIGEST to the digest of ${IDEA_REVISION}'s image, or unset it (env -u IDEA_IMAGE_DIGEST ./idea-admin.sh ...). Nothing was tagged, so the next run chooses again.${NC}"
      fi
      exit 1
    fi
  fi
  # Only the image the release record names is tagged as the release, so an offline run later
  # finds it. An image chosen by IDEA_IMAGE_DIGEST or a release-attached script is found by its
  # digest next time and is never tagged: it may be a candidate that reports the same release.
  if [[ -n "${IDEA_RECORD_URL:-}" && "${IDEA_IMAGE_SOURCE}" == "${IDEA_RECORD_URL}" ]]; then
    ${DOCKER_BIN} tag "${IDEA_IMAGE}" "${IDEA_RELEASE_TAG_REF}" >> /dev/null 2>&1 || true
  fi
fi

IDEA_IMAGE_CREATED=$(${DOCKER_BIN} image inspect --format "{{.Created}}" "${IDEA_IMAGE}" 2>/dev/null)
echo -e "${YELLOW}[INFO] Control plane image: ${IDEA_IMAGE} (created ${IDEA_IMAGE_CREATED:-unknown})${NC}"
echo -e "${YELLOW}[INFO] Launching IDEA administrator...${NC}"
# Drop -t when stdin is not a terminal, so docker does not try to attach a TTY to a pipe.
if [[ -t 0 && "${IDEA_ADMIN_NO_TTY}" != "true" ]]; then
  DOCKER_TTY_FLAGS="-it"
else
  DOCKER_TTY_FLAGS="-i"
fi
# Launch installer
${DOCKER_BIN} run --rm ${DOCKER_TTY_FLAGS} -v "${HOME}/.idea/clusters:/root/.idea/clusters" \
              -e AWS_SESSION_TOKEN -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_PROFILE \
              -e AWS_REGION -e AWS_DEFAULT_REGION \
              -e AWS_SDK_LOAD_CONFIG=1 \
              -e IDEA_ADMIN_AWS_CREDENTIAL_PROVIDER="${IDEA_ADMIN_AWS_CREDENTIAL_PROVIDER}" \
              -e IDEA_ADMIN_ENABLE_CDK_NAG_SCAN="${IDEA_ADMIN_ENABLE_CDK_NAG_SCAN}" \
              -v ~/.aws:/root/.aws "${IDEA_IMAGE}" \
              ideactl "${@}"
