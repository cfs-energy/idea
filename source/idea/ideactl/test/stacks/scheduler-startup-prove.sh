#!/bin/bash
#
# Runs the scheduler container role against an endpoint that answers only what a rendered IAM
# policy allows, and proves two things by execution:
#
#   1. with the account-table grant, the role reaches the line that starts the scheduler module
#   2. without it, the role fails on the user sync and never reaches that line
#
# usage: prove.sh <workdir> <policy-with-grant.json> <policy-without-grant.json>
#
# The workdir must be under the home directory so the container engine can bind it.
# Environment: DOCKER_CONTEXT (default: the current context), IDEA_IMAGE, IDEA_CLUSTER_NAME, AWS_REGION_NAME,
# AWS_ACCOUNT_ID, IDEA_ROUTE53_ZONE_ID, SCHED_TIMEOUT, IDEA_ROLE_SOURCE.
#
# IDEA_ROLE_SOURCE is the image's role material in the working tree. It is mounted over the copy
# baked into the image so the run exercises the entrypoint, role scripts and user sync as they
# stand here, which is what a rebuilt image would carry.

set -euo pipefail

WORK="${1:?workdir}"
POLICY_GRANT="${2:?policy with the grant}"
POLICY_NO_GRANT="${3:?policy without the grant}"

CTX="${DOCKER_CONTEXT:-default}"
IMAGE="${IDEA_IMAGE:?IDEA_IMAGE is required}"
CLUSTER="${IDEA_CLUSTER_NAME:?IDEA_CLUSTER_NAME is required}"
REGION="${AWS_REGION_NAME:?AWS_REGION_NAME is required}"
ACCOUNT="${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID is required}"
ZONE="${IDEA_ROUTE53_ZONE_ID:-Z00000000000000000000}"
DDB_IMAGE="${DDB_IMAGE:-amazon/dynamodb-local:latest}"
SRC="${IDEA_ROLE_SOURCE:-$(cd "$(dirname "$0")/../../../../../deployment/ecr/idea-control-plane" && pwd)}"
MARKER="starting scheduler module"
TAG="schedstartup-$$"
NET="${TAG}-net"

d() { docker --context "${CTX}" "$@"; }

# shellcheck disable=SC2317  # invoked by the trap below
cleanup() {
  d rm -f "${TAG}-ddb" "${TAG}-iam" "${TAG}-sched" "${TAG}-cluster-manager" "${TAG}-vdc" >/dev/null 2>&1 || true
  d volume rm -f "${TAG}-apps" "${TAG}-pbs" "${TAG}-apps2" >/dev/null 2>&1 || true
  d network rm -f "${NET}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== context ${CTX}"
d image inspect "${IMAGE}" --format '== image {{.Id}} {{.Architecture}}/{{.Os}}'
echo "== role material from ${SRC}"

cp "$(dirname "$0")/iam-endpoint.py" "${WORK}/iam-endpoint.py"

# The image chmods this material at build time, so stage an executable copy to mount.
mkdir -p "${WORK}/image"
cp -R "${SRC}/entrypoint.sh" "${SRC}/sync_users.py" "${SRC}/roles" "${WORK}/image/"
chmod +x "${WORK}/image/entrypoint.sh" "${WORK}/image/roles/"*.sh

FAILED=0
check() {
  if [[ "$1" == "yes" ]]; then echo "PASS $2"; else echo "FAIL $2"; FAILED=1; fi
}
yn() { if eval "$1"; then echo yes; else echo no; fi; }

# The evaluator that gates the runs below has to be shown to answer both ways on this document,
# and to stay inside the three tables the grant names.
echo
echo "===== the policy evaluator, on the two rendered documents ====="
control() {
  local policy="$1" action="$2" table="$3" expect="$4" got
  got="$(IAM_POLICY_FILE="${policy}" python3 "${WORK}/iam-endpoint.py" --decide "${action}" \
    "arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${CLUSTER}.${table}")"
  check "$([[ "${got}" == "${expect}" ]] && echo yes || echo no)" \
    "$(basename "${policy}"): ${action} on ${table} is ${got}, expected ${expect}"
}
control "${POLICY_GRANT}" dynamodb:Scan accounts.users allow
control "${POLICY_NO_GRANT}" dynamodb:Scan accounts.users deny
control "${POLICY_GRANT}" dynamodb:PutItem accounts.users deny
control "${POLICY_GRANT}" dynamodb:Scan accounts.sso-state deny
control "${POLICY_GRANT}" dynamodb:GetItem cluster-settings allow
control "${POLICY_NO_GRANT}" dynamodb:GetItem cluster-settings allow

d network create "${NET}" >/dev/null
d run -d --name "${TAG}-ddb" --network "${NET}" --network-alias ddb "${DDB_IMAGE}" \
  -jar DynamoDBLocal.jar -inMemory -sharedDb >/dev/null

# Seed the four tables the role reads, with the image's own client against the local DynamoDB.
d run --rm -i --network "${NET}" \
  -e AWS_ENDPOINT_URL=http://ddb:8000 -e AWS_DEFAULT_REGION="${REGION}" \
  -e AWS_ACCESS_KEY_ID=seed -e AWS_SECRET_ACCESS_KEY=seed -e AWS_EC2_METADATA_DISABLED=true \
  -e CLUSTER="${CLUSTER}" -e HOME_DIR="/apps/${CLUSTER}" -e ZONE_NAME="${CLUSTER}.${REGION}.local" \
  --entrypoint bash "${IMAGE}" -s <<'SEED'
set -euo pipefail
for _ in $(seq 1 60); do
  aws dynamodb list-tables >/dev/null 2>&1 && break
  sleep 1
done
# shellcheck disable=SC2086
mk() { aws dynamodb create-table --table-name "$1" --attribute-definitions $2 --key-schema $3 \
         --billing-mode PAY_PER_REQUEST >/dev/null; }
mk "${CLUSTER}.cluster-settings" 'AttributeName=key,AttributeType=S' 'AttributeName=key,KeyType=HASH'
mk "${CLUSTER}.accounts.users" 'AttributeName=username,AttributeType=S' 'AttributeName=username,KeyType=HASH'
mk "${CLUSTER}.accounts.groups" 'AttributeName=group_name,AttributeType=S' 'AttributeName=group_name,KeyType=HASH'
mk "${CLUSTER}.accounts.group-members" \
  'AttributeName=group_name,AttributeType=S AttributeName=username,AttributeType=S' \
  'AttributeName=group_name,KeyType=HASH AttributeName=username,KeyType=RANGE'
put() { aws dynamodb put-item --table-name "$1" --item "$2" >/dev/null; }
put "${CLUSTER}.cluster-settings" "{\"key\":{\"S\":\"cluster.home_dir\"},\"value\":{\"S\":\"${HOME_DIR}\"}}"
put "${CLUSTER}.cluster-settings" "{\"key\":{\"S\":\"cluster.route53.private_hosted_zone_name\"},\"value\":{\"S\":\"${ZONE_NAME}\"}}"
put "${CLUSTER}.accounts.users" '{"username":{"S":"testuser"},"uid":{"N":"5000"},"gid":{"N":"5000"},"enabled":{"BOOL":true},"home_dir":{"S":"/data/home/testuser"},"login_shell":{"S":"/bin/bash"},"additional_groups":{"L":[{"S":"testgroup"}]}}'
put "${CLUSTER}.accounts.groups" '{"group_name":{"S":"testgroup"},"gid":{"N":"5000"},"enabled":{"BOOL":true}}'
put "${CLUSTER}.accounts.group-members" '{"group_name":{"S":"testgroup"},"username":{"S":"testuser"}}'
aws dynamodb list-tables --query 'TableNames' --output text
SEED
echo "== seeded"

start_endpoint() {
  d rm -f "${TAG}-iam" >/dev/null 2>&1 || true
  cp "$1" "${WORK}/policy.json"
  d run -d --name "${TAG}-iam" --network "${NET}" --network-alias iam \
    -v "${WORK}":/work:ro \
    -e IAM_POLICY_FILE=/work/policy.json -e DDB_ENDPOINT=http://ddb:8000 \
    -e AWS_ACCOUNT_ID="${ACCOUNT}" -e AWS_REGION_NAME="${REGION}" \
    python:3.13-slim python /work/iam-endpoint.py >/dev/null
  for _ in $(seq 1 30); do
    if d logs "${TAG}-iam" 2>&1 | grep -q 'listening on'; then return 0; fi
    sleep 1
  done
  echo "the endpoint did not start" >&2
  d logs "${TAG}-iam" >&2
  return 1
}

run_scheduler() {
  d rm -f "${TAG}-sched" >/dev/null 2>&1 || true
  d volume rm -f "${TAG}-apps" "${TAG}-pbs" >/dev/null 2>&1 || true
  d run -d --name "${TAG}-sched" --network "${NET}" \
    -v "${TAG}-apps":/apps -v "${TAG}-pbs":/var/spool/pbs \
    -v "${WORK}/image/entrypoint.sh":/opt/idea/entrypoint.sh:ro \
    -v "${WORK}/image/sync_users.py":/opt/idea/sync_users.py:ro \
    -v "${WORK}/image/roles":/opt/idea/roles:ro \
    -e AWS_DEFAULT_REGION="${REGION}" \
    -e AWS_ENDPOINT_URL=http://iam:8099 \
    -e AWS_ACCESS_KEY_ID=task -e AWS_SECRET_ACCESS_KEY=task -e AWS_EC2_METADATA_DISABLED=true \
    -e DD_DOGSTATSD_URL=unix:///var/run/datadog/dsd.socket \
    -e IDEA_CLUSTER_NAME="${CLUSTER}" \
    -e IDEA_CONTAINER_ROLE=scheduler \
    -e IDEA_MODULE_ID=scheduler \
    -e IDEA_MODULE_NAME=scheduler \
    -e IDEA_MODULE_SET=default \
    -e IDEA_ROUTE53_ZONE_ID="${ZONE}" \
    -e IDEA_SCHEDULER_DNS_NAME="scheduler.${CLUSTER}.${REGION}.local" \
    -e PBS_HOME=/var/spool/pbs \
    -e PBS_NODE_FAIL_REQUEUE=600 \
    "${IMAGE}" >/dev/null
  local deadline=$((SECONDS + ${SCHED_TIMEOUT:-360}))
  while (( SECONDS < deadline )); do
    if d logs "${TAG}-sched" 2>&1 | grep -q "${MARKER}"; then
      echo "reached"
      return 0
    fi
    if [[ "$(d inspect -f '{{.State.Running}}' "${TAG}-sched" 2>/dev/null)" != "true" ]]; then
      # The marker and the exit can both land inside one poll interval, and the log driver can
      # still be flushing the last lines when the state flips, so settle, then read twice more
      # before calling it never reached.
      sleep 2
      for _ in 1 2; do
        if d logs "${TAG}-sched" 2>&1 | grep -q "${MARKER}"; then
          echo "reached"
          return 0
        fi
        sleep 1
      done
      echo "exited $(d inspect -f '{{.State.ExitCode}}' "${TAG}-sched" 2>/dev/null)"
      return 0
    fi
    sleep 2
  done
  echo "timeout"
}

transcript() {
  d logs "${TAG}-sched" 2>&1 | sed 's/^/  sched| /'
  d logs "${TAG}-iam" 2>&1 | grep -E '^(ALLOW|DENY|ANSWER|UNDECIDABLE|UNHANDLED|ERROR|RETRY)' | sed 's/^/  iam  | /' || true
}

echo
echo "===== RUN 1: the rendered policy including the account-table grant ====="
start_endpoint "${POLICY_GRANT}"
OUTCOME_GRANT="$(run_scheduler)"
transcript
echo "  outcome: ${OUTCOME_GRANT}"
echo "  certificate material left on the applications volume:"
# shellcheck disable=SC2016  # the quoted script runs inside the container
d run --rm -v "${TAG}-apps":/apps -e C="${CLUSTER}" --entrypoint bash "${IMAGE}" -c '
set -euo pipefail
ls -ld "/apps/${C}/certs"
ls -l "/apps/${C}/certs"
openssl x509 -noout -subject -enddate -in "/apps/${C}/certs/idea.crt"
echo -n "certificate modulus "; openssl x509 -noout -modulus -in "/apps/${C}/certs/idea.crt" | sha256sum
echo -n "private key modulus "; openssl rsa -noout -modulus -in "/apps/${C}/certs/idea.key" | sha256sum
' 2>&1 | sed 's/^/    /'

echo
echo "===== RUN 2: the same policy with the grant removed (sabotage) ====="
start_endpoint "${POLICY_NO_GRANT}"
OUTCOME_NO_GRANT="$(run_scheduler)"
transcript
echo "  outcome: ${OUTCOME_NO_GRANT}"
# shellcheck disable=SC2034  # read inside the strings handed to yn below
SCHED_LOG="$(d logs "${TAG}-sched" 2>&1)"
# shellcheck disable=SC2034  # read inside the strings handed to yn below
IAM_LOG="$(d logs "${TAG}-iam" 2>&1)"

echo
echo "===== RUN 3: two module roles starting together on one applications volume ====="
start_endpoint "${POLICY_GRANT}"
for ROLE in cluster-manager vdc; do
  case "${ROLE}" in
    cluster-manager) MODULE_NAME=cluster-manager ;;
    vdc) MODULE_NAME=virtual-desktop-controller ;;
  esac
  d run -d --name "${TAG}-${ROLE}" --network "${NET}" \
    -v "${TAG}-apps2":/apps \
    -v "${WORK}/image/entrypoint.sh":/opt/idea/entrypoint.sh:ro \
    -v "${WORK}/image/sync_users.py":/opt/idea/sync_users.py:ro \
    -v "${WORK}/image/roles":/opt/idea/roles:ro \
    -e AWS_DEFAULT_REGION="${REGION}" -e AWS_ENDPOINT_URL=http://iam:8099 \
    -e AWS_ACCESS_KEY_ID=task -e AWS_SECRET_ACCESS_KEY=task -e AWS_EC2_METADATA_DISABLED=true \
    -e IDEA_CLUSTER_NAME="${CLUSTER}" -e IDEA_CONTAINER_ROLE="${ROLE}" \
    -e IDEA_MODULE_ID="${ROLE}" -e IDEA_MODULE_NAME="${MODULE_NAME}" -e IDEA_MODULE_SET=default \
    "${IMAGE}" >/dev/null
done
for _ in $(seq 1 90); do
  RUNNING=0
  for ROLE in cluster-manager vdc; do
    [[ "$(d inspect -f '{{.State.Running}}' "${TAG}-${ROLE}" 2>/dev/null)" == "true" ]] && RUNNING=1
  done
  (( RUNNING == 0 )) && break
  sleep 2
done
RACE_LOG="$(d logs "${TAG}-cluster-manager" 2>&1; d logs "${TAG}-vdc" 2>&1)"
GENERATED="$(grep -c 'generated the application certificate pair' <<< "${RACE_LOG}" || true)"
echo "  roles that generated the pair: ${GENERATED}"
grep -E 'entrypoint. (generated|no application)' <<< "${RACE_LOG}" | sed 's/^/  race | /' || true
# shellcheck disable=SC2016  # the quoted script runs inside the container
d run --rm -v "${TAG}-apps2":/apps -e C="${CLUSTER}" --entrypoint bash "${IMAGE}" -c '
set -euo pipefail
ls -l "/apps/${C}/certs"
echo -n "certificate modulus "; openssl x509 -noout -modulus -in "/apps/${C}/certs/idea.crt" | sha256sum
echo -n "private key modulus "; openssl rsa -noout -modulus -in "/apps/${C}/certs/idea.key" | sha256sum
' 2>&1 | tee "${WORK}/race-certs.txt" | sed 's/^/    /'

echo
check "$([[ "${OUTCOME_GRANT}" == "reached" ]] && echo yes || echo no)" \
  "run 1 reached '${MARKER}'"
check "$(yn "grep -q 'DENY  dynamodb:Scan .*accounts.users' <<< \"\${IAM_LOG}\"")" \
  "run 2 was refused dynamodb:Scan on the account tables by the policy"
check "$(yn "grep -q 'sync_users. failed:.*AccessDeniedException' <<< \"\${SCHED_LOG}\"")" \
  "run 2 failed the user sync on that refusal"
check "$(yn "! grep -q '${MARKER}' <<< \"\${SCHED_LOG}\"")" \
  "run 2 never reached '${MARKER}'"
check "$([[ "${OUTCOME_NO_GRANT}" == exited* && "${OUTCOME_NO_GRANT}" != "exited 0" ]] && echo yes || echo no)" \
  "run 2 ${OUTCOME_NO_GRANT}"
check "$([[ "${GENERATED}" == "1" ]] && echo yes || echo no)" \
  "run 3 had exactly one of the two roles generate the pair (${GENERATED} generated)"
check "$(yn "! grep -q 'no application certificate pair' <<< \"\${RACE_LOG}\"")" \
  "run 3 left neither role without a pair"
CERT_MOD="$(awk '/^certificate modulus/ {print $3}' "${WORK}/race-certs.txt")"
KEY_MOD="$(awk '/^private key modulus/ {print $4}' "${WORK}/race-certs.txt")"
check "$([[ -n "${CERT_MOD}" && "${CERT_MOD}" == "${KEY_MOD}" ]] && echo yes || echo no)" \
  "run 3 published a certificate and key with one modulus (${CERT_MOD:-none})"
exit "${FAILED}"
