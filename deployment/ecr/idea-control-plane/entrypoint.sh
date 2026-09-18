#!/bin/bash
#
# Run the process selected by IDEA_CONTAINER_ROLE.
set -euo pipefail

# A role passed as the first argument overrides IDEA_CONTAINER_ROLE.
case "${1:-}" in
  ideactl|cost-metrics|cluster-manager|vdc|scheduler|dcv-broker|dcv-gateway|bastion-host) ROLE="$1"; shift ;;
  *) ROLE="${IDEA_CONTAINER_ROLE:?IDEA_CONTAINER_ROLE is required: ideactl|cost-metrics|cluster-manager|vdc|scheduler|dcv-broker|dcv-gateway|bastion-host}" ;;
esac

# Module packages share an ideaserver command, so run each module's main directly.
run_module() {
  exec python3.13 -c "import sys; sys.argv=['ideaserver']; from ${1}.app.app_main import main; sys.exit(main())"
}

setting() {
  aws dynamodb get-item --region "${AWS_DEFAULT_REGION}" \
    --table-name "${IDEA_CLUSTER_NAME}.cluster-settings" \
    --key "{\"key\":{\"S\":\"$1\"}}" --query 'Item.value.S' --output text
}

# The three module roles bind TLS against one certificate pair under the cluster home on the
# applications file system. This substrate owns creating it: no host bootstrap runs, so on a fresh
# install the pair does not exist and the module raises a file-not-found before it binds its port.
# The roles start together on the same shared volume, so the pair is published under a link that
# elects one writer rather than by two roles each writing half of it.
ensure_app_certs() {
  local home certs key crt zone tmp
  home="$(setting cluster.home_dir)"
  case "${home}" in ''|None) echo "[entrypoint] cluster.home_dir is not set" >&2; return 1 ;; esac
  certs="${home}/certs"
  key="${certs}/idea.key"
  crt="${certs}/idea.crt"
  if [[ -s "${key}" && -s "${crt}" ]]; then
    return 0
  fi
  zone="$(setting cluster.route53.private_hosted_zone_name)"
  case "${zone}" in ''|None) echo "[entrypoint] cluster.route53.private_hosted_zone_name is not set" >&2; return 1 ;; esac
  install -d -m 700 "${certs}"
  tmp="$(mktemp -d "${certs}/.new.XXXXXX")"
  openssl req -new -newkey rsa:4096 -days 3650 -nodes -x509 \
    -subj "/C=US/ST=California/L=Sunnyvale/CN=*.${zone}" \
    -keyout "${tmp}/idea.key" -out "${tmp}/idea.crt" 2>/dev/null
  chmod 600 "${tmp}/idea.key"
  # A hard link fails when the name is taken, so the first role to get there publishes the pair it
  # generated and the rest wait for that one.
  if ln "${tmp}/idea.key" "${key}" 2>/dev/null; then
    mv -f "${tmp}/idea.crt" "${crt}"
    echo "[entrypoint] generated the application certificate pair in ${certs}"
  else
    for _ in $(seq 1 60); do
      [[ -s "${crt}" ]] && break
      sleep 1
    done
  fi
  rm -rf "${tmp}"
  if [[ ! -s "${key}" || ! -s "${crt}" ]]; then
    echo "[entrypoint] no application certificate pair in ${certs}" >&2
    return 1
  fi
}

case "${ROLE}" in
  cluster-manager|vdc|scheduler) ensure_app_certs ;;
esac

case "${ROLE}" in
  ideactl)         exec node /opt/idea/ideactl/dist/src/cli/main.js "$@" ;;
  cost-metrics)    exec python3.13 -m ideaclustermanager.app.metrics.standalone ;;
  cluster-manager) run_module ideaclustermanager ;;
  vdc)             run_module ideavirtualdesktopcontroller ;;
  scheduler)       exec /opt/idea/roles/scheduler.sh ;;
  dcv-broker)      exec /opt/idea/roles/broker.sh ;;
  bastion-host)    exec /opt/idea/roles/bastion.sh ;;
  dcv-gateway)     exec /opt/idea/roles/gateway.sh ;;
  *) echo "[entrypoint] unknown IDEA_CONTAINER_ROLE=${ROLE}" >&2; exit 1 ;;
esac
