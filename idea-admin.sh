#!/bin/bash

# Run the control-plane container.
#
# Usage:
# ./idea-admin.sh --help
#
# Environment Variables:
# * IDEA_REVISION - Use to override the default IDEA version.
# * IDEA_DOCKER_REPO - Use to override the default Docker/ECR repository.
# * IDEA_ECR_CREDS_RESET - Set to false, if you handle AWS ECR authentication manually.
# * IDEA_ADMIN_AWS_CREDENTIAL_PROVIDER - Set to "Ec2InstanceMetadata", if you want install IDEA from an EC2 Instance
#                         using Instance Profile credentials from EC2 Instance Metadata.
# * IDEA_ADMIN_ENABLE_CDK_NAG_SCAN - Set to "false", if you want to disable cdk-nag scan. Default: true
# * IDEA_DEV_MODE - Set to "true" to build and run the deploy tool from source instead of the
#                         control-plane container. Requires Node.js and a checkout.
# * IDEA_ADMIN_NO_TTY - Set to "true" to drop docker's -t flag even when stdin is a
#                         terminal. Non-interactive stdin (ssm, cron, CI) is detected
#                         automatically. Combine with --force for an unattended run.

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
IDEA_REVISION=${IDEA_REVISION:-"v26.09.4"}
IDEA_DOCKER_REPO_DEFAULT="public.ecr.aws/s5o2b4m0/idea-control-plane"
IDEA_DOCKER_REPO=${IDEA_DOCKER_REPO:-"${IDEA_DOCKER_REPO_DEFAULT}"}
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

# A local build is tagged with no registry prefix, so the pull check below cannot see it and
# would fetch the released image over it. Prefer the local one.
if [[ "${IDEA_DOCKER_REPO}" == "${IDEA_DOCKER_REPO_DEFAULT}" ]] && \
   ${DOCKER_BIN} image inspect "idea-control-plane:${IDEA_REVISION}" >> /dev/null 2>&1; then
  IDEA_DOCKER_REPO="idea-control-plane"
fi

# Reset ECR credentials
if [[ "${IDEA_ECR_CREDS_RESET}" == "true" && "${IDEA_DOCKER_REPO}" == *"/"* ]]; then
  echo -e "${YELLOW}[INFO] Resetting ECR credentials...${NC}"
  DIG_BIN=$(command -v dig)
  IDEA_DOCKER_REPO_HOSTNAME=$(echo "${IDEA_DOCKER_REPO}" | cut -d '/' -f 1)
  if [[ -z "${DIG_BIN}" ]]; then
    # dig ships in bind-utils, which a stock Amazon Linux 2023 host does not have. The reset is a
    # convenience, so skip it rather than fail the command that was asked for.
    echo -e "${YELLOW}[INFO] dig not found: skipping ECR credentials reset. Install bind-utils, or set IDEA_ECR_CREDS_RESET=false to skip this step without the warning.${NC}"
  else
    ${DIG_BIN} +tries=1 +time=3 "${IDEA_DOCKER_REPO_HOSTNAME}" >> /dev/null 2>&1
    verify_command "Unable to query ECR host ${IDEA_DOCKER_REPO_HOSTNAME} . Are you connected to internet?"

    ${DOCKER_BIN} logout public.ecr.aws >> /dev/null 2>&1
    verify_command "Failed to refresh ECR credentials. docker logout public.ecr.aws failed"
    echo -e "${GREEN}✓ ECR credentials reset${NC}"
  fi
else
  echo -e "${YELLOW}[INFO] Skipping ECR credentials reset (IDEA_ECR_CREDS_RESET=false)${NC}"
fi

# Pull IDEA docker image if needed
if ! ${DOCKER_BIN} images | grep "${IDEA_DOCKER_REPO}" | grep -q "${IDEA_REVISION}"; then
  echo -e "${YELLOW}[INFO] Pulling IDEA Docker image: ${IDEA_DOCKER_REPO}:${IDEA_REVISION}${NC}"
  ${DOCKER_BIN} pull "${IDEA_DOCKER_REPO}":"${IDEA_REVISION}"
  verify_command "Unable to download IDEA container image. Refer to the error above."
  echo -e "${GREEN}✓ Docker image downloaded${NC}"
else
  echo -e "${GREEN}✓ Docker image already available${NC}"
fi

IDEA_IMAGE_CREATED=$(${DOCKER_BIN} image inspect --format "{{.Created}}" "${IDEA_DOCKER_REPO}:${IDEA_REVISION}" 2>/dev/null)
echo -e "${YELLOW}[INFO] Control plane image: ${IDEA_DOCKER_REPO}:${IDEA_REVISION} (created ${IDEA_IMAGE_CREATED:-unknown})${NC}"
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
              -v ~/.aws:/root/.aws "${IDEA_DOCKER_REPO}:${IDEA_REVISION}" \
              ideactl "${@}"
