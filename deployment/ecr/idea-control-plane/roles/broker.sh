#!/bin/bash
#
# Render broker settings, register the authorization server, and run the broker.
#
# Required: IDEA_CLUSTER_NAME, IDEA_MODULE_ID (the virtual desktop module id, for the
#           DynamoDB table prefix), AWS_DEFAULT_REGION, and one of
#           IDEA_SERVICE_DISCOVERY_NAME (a name resolving to every broker; the ECS
#           module sets it), IDEA_BROKER_DISCOVERY_ADDRESSES (host:port list) or
#           IDEA_BROKER_CLIENT_TARGET_GROUP_ARN (the target group the client port is
#           registered in; finds EC2 instance targets only)
# Optional: IDEA_COGNITO_PROVIDER_URL (read from the cluster settings when unset),
#           IDEA_BROKER_CONF_FILE (where the rendered broker properties are written)
#
# The broker runs on a Java virtual machine in a task network namespace with one address family.
# JAVA_TOOL_OPTIONS carries -Djava.net.preferIPv4Stack=true from the task definition; the virtual
# machine reads that variable itself, so this script passes the environment through untouched.

set -euo pipefail

log() { echo "[entrypoint] $*"; }

: "${IDEA_CLUSTER_NAME:?}" "${IDEA_MODULE_ID:?}" "${AWS_DEFAULT_REGION:?}"

# Use service discovery for tasks registered by IP.
IDEA_BROKER_DISCOVERY_ADDRESSES="${IDEA_BROKER_DISCOVERY_ADDRESSES:-${IDEA_SERVICE_DISCOVERY_NAME:+${IDEA_SERVICE_DISCOVERY_NAME}:47500}}"
if [ -n "${IDEA_BROKER_DISCOVERY_ADDRESSES}" ]; then
  DISCOVERY="broker-to-broker-discovery-addresses = ${IDEA_BROKER_DISCOVERY_ADDRESSES}"
  if [ -n "${IDEA_SERVICE_DISCOVERY_NAME:-}" ]; then
    # Wait for service discovery before the broker starts.
    for _ in $(seq 30); do getent hosts "${IDEA_SERVICE_DISCOVERY_NAME}" >/dev/null && break; sleep 2; done
    log "${IDEA_SERVICE_DISCOVERY_NAME} resolves to: $(getent hosts "${IDEA_SERVICE_DISCOVERY_NAME}" | awk '{print $1}' | tr '\n' ' ')"
  fi
else
  : "${IDEA_BROKER_CLIENT_TARGET_GROUP_ARN:?set IDEA_BROKER_DISCOVERY_ADDRESSES or IDEA_BROKER_CLIENT_TARGET_GROUP_ARN}"
  DISCOVERY="broker-to-broker-discovery-aws-region = ${AWS_DEFAULT_REGION}
broker-to-broker-discovery-aws-alb-target-group-arn = ${IDEA_BROKER_CLIENT_TARGET_GROUP_ARN}"
fi

setting() {
  aws dynamodb get-item --region "${AWS_DEFAULT_REGION}" \
    --table-name "${IDEA_CLUSTER_NAME}.cluster-settings" \
    --key "{\"key\":{\"S\":\"$1\"}}" \
    --query "Item.value.S || Item.value.N" --output text 2>/dev/null | grep -v '^None$' || echo "$2"
}

CLIENT_PORT="$(setting virtual-desktop-controller.dcv_broker.client_communication_port 8444)"
AGENT_PORT="$(setting virtual-desktop-controller.dcv_broker.agent_communication_port 8445)"
GATEWAY_PORT="$(setting virtual-desktop-controller.dcv_broker.gateway_communication_port 8446)"
TOKEN_MINUTES="$(setting virtual-desktop-controller.dcv_broker.session_token_validity 1440)"
RCU="$(setting virtual-desktop-controller.dcv_broker.dynamodb_table.read_capacity.min_units 5)"
WCU="$(setting virtual-desktop-controller.dcv_broker.dynamodb_table.write_capacity.min_units 5)"
PROVIDER_URL="${IDEA_COGNITO_PROVIDER_URL:-$(setting identity-provider.cognito.provider_url "")}"
: "${PROVIDER_URL:?identity-provider.cognito.provider_url is not set}"

CONF="${IDEA_BROKER_CONF_FILE:-/etc/dcv-session-manager-broker/session-manager-broker.properties}"
cat > "${CONF}" <<EOF
enable-authorization-server = false
enable-authorization = true
enable-agent-authorization = false
enable-persistence = true
persistence-db = dynamodb
dynamodb-region = ${AWS_DEFAULT_REGION}
dynamodb-table-rcu = ${RCU}
dynamodb-table-wcu = ${WCU}
dynamodb-table-name-prefix = ${IDEA_CLUSTER_NAME}.${IDEA_MODULE_ID}.dcv-broker.
connect-session-token-duration-minutes = ${TOKEN_MINUTES}
delete-session-duration-seconds = 3600
seconds-before-deleting-unreachable-dcv-server = 900
seconds-before-deleting-sessions-unreachable-server = 900
client-to-broker-connector-https-port = ${CLIENT_PORT}
client-to-broker-connector-bind-host = 0.0.0.0
agent-to-broker-connector-https-port = ${AGENT_PORT}
agent-to-broker-connector-bind-host = 0.0.0.0
enable-gateway = true
gateway-to-broker-connector-https-port = ${GATEWAY_PORT}
gateway-to-broker-connector-bind-host = 0.0.0.0
broker-to-broker-port = 47100
cli-to-broker-port = 47200
broker-to-broker-bind-host = 0.0.0.0
broker-to-broker-discovery-port = 47500
${DISCOVERY}
broker-to-broker-distributed-memory-max-size-mb = 4096
broker-to-broker-connection-login = dcvsm-user
broker-to-broker-connection-pass = dcvsm-pass
metrics-fleet-name-dimension = ${IDEA_CLUSTER_NAME}
enable-cloud-watch-metrics = ${IDEA_BROKER_CLOUDWATCH_METRICS:-true}
# Without this the broker asks the EC2 metadata service for its region, which a task has no access to.
cloud-watch-region = ${AWS_DEFAULT_REGION}
session-manager-working-path = /var/lib/dcvsmbroker
session-screenshot-max-height = 600
session-screenshot-max-width = 800
EOF
chown root:dcvsmbroker "${CONF}"
chmod 640 "${CONF}"

log "registering ${PROVIDER_URL} as the authorization server"
dcv-session-manager-broker register-auth-server --url "${PROVIDER_URL}/.well-known/jwks.json"

log "starting the broker on ${CLIENT_PORT}/${AGENT_PORT}/${GATEWAY_PORT}"
exec setpriv --reuid=dcvsmbroker --regid=dcvsmbroker --init-groups \
  /usr/share/dcv-session-manager-broker/bin/dcv-session-manager-broker.sh
