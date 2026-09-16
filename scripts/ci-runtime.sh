#!/usr/bin/env bash
set -euo pipefail
NVM_VERSION=$(awk '/^nvm_version:/ {print $2}' software_versions.yml)
NODE_VERSION=$(awk '/^node_version:/ {print $2}' software_versions.yml)
RUNTIME_ROOT=$(mktemp -d)
trap 'rm -rf "$RUNTIME_ROOT"' EXIT
export NVM_DIR="$RUNTIME_ROOT/nvm"
mkdir -p "$NVM_DIR"
curl -fsSL "https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh" -o "$RUNTIME_ROOT/install.sh"
PROFILE=/dev/null METHOD=script bash "$RUNTIME_ROOT/install.sh"
# Use the installed loader and runtime, so a renamed tag or incompatible installer fails CI.
# shellcheck source=/dev/null
. "$NVM_DIR/nvm.sh" --no-use
nvm install "$NODE_VERSION"
test "$(node --version)" = "v${NODE_VERSION}"
