#!/bin/bash
#
# Write gateway settings, start nginx, and run the gateway.
#
# Required: DCV_GATEWAY_CERT_PEM, DCV_GATEWAY_KEY_PEM (PEM text),
#           IDEA_INTERNAL_ALB_ENDPOINT (https://<load balancer the broker is behind>)
# Optional: DCV_BROKER_GATEWAY_PORT (8446), DCV_GATEWAY_LOG_LEVEL (info)

set -euo pipefail

log() { echo "[entrypoint] $*"; }

: "${DCV_GATEWAY_CERT_PEM:?}" "${DCV_GATEWAY_KEY_PEM:?}" "${IDEA_INTERNAL_ALB_ENDPOINT:?}"
BROKER_PORT="${DCV_BROKER_GATEWAY_PORT:-8446}"

CERTS=/etc/dcv-connection-gateway/certs
install -d -m 700 -o dcvcgw -g dcvcgw "${CERTS}"
printf '%s\n' "${DCV_GATEWAY_CERT_PEM}" > "${CERTS}/default_cert.pem"
printf '%s\n' "${DCV_GATEWAY_KEY_PEM}" | openssl pkcs8 -topk8 -inform PEM -outform PEM -nocrypt -out "${CERTS}/default_key_pkcs8.pem"
chmod 600 "${CERTS}"/default_*.pem
chown dcvcgw:dcvcgw "${CERTS}"/default_*.pem
unset DCV_GATEWAY_CERT_PEM DCV_GATEWAY_KEY_PEM

# Every listener binds IPv4 only. The task network namespace has one address family, so a bind on
# "::" fails and the health port the load balancer checks would never come up.
cat > /etc/dcv-connection-gateway/dcv-connection-gateway.conf <<EOF
[log]
level = "${DCV_GATEWAY_LOG_LEVEL:-info}"

[gateway]
quic-listen-endpoints = ["0.0.0.0:8443"]
web-listen-endpoints = ["0.0.0.0:8443"]
cert-file = "${CERTS}/default_cert.pem"
cert-key-file = "${CERTS}/default_key_pkcs8.pem"

[health-check]
bind-addr = "0.0.0.0"
port = 8989

[dcv]
tls-strict = false

[resolver]
url = "${IDEA_INTERNAL_ALB_ENDPOINT}:${BROKER_PORT}"
tls-strict = false

[web-resources]
url = "http://localhost:80"
tls-strict = false
EOF

log "starting nginx for the web viewer"
nginx

log "starting the gateway; resolver ${IDEA_INTERNAL_ALB_ENDPOINT}:${BROKER_PORT}"
exec setpriv --reuid=dcvcgw --regid=dcvcgw --init-groups \
  /usr/libexec/dcv-connection-gateway/dcv-connection-gateway --config /etc/dcv-connection-gateway/dcv-connection-gateway.conf
