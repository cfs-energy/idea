#!/usr/bin/env bash
set -euo pipefail
SMOKE_ROOT=$(mktemp -d)
trap 'rm -rf "$SMOKE_ROOT"' EXIT
mkdir -p "$SMOKE_ROOT/config"
cp source/idea/ideactl/test/cli/shell-path-values.yml "$SMOKE_ROOT/values.yml"
docker run --rm --entrypoint /bin/bash idea-scheduler-ci:latest -c '/opt/pbs/sbin/pbs_server --version'
docker run --rm --user "$(id -u):$(id -g)" --env HOME=/tmp --workdir /tmp/work idea-control-plane-ci:latest ideactl about
docker run --rm --user "$(id -u):$(id -g)" --env HOME=/tmp --workdir /tmp/work \
  --volume "$SMOKE_ROOT/values.yml:/tmp/values.yml:ro" \
  --volume "$SMOKE_ROOT/config:/tmp/config" idea-control-plane-ci:latest \
  ideactl config generate --values-file /tmp/values.yml --config-dir /tmp/config --force
test -s "$SMOKE_ROOT/config/config/idea.yml"
