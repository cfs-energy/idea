#!/usr/bin/env bash
set -euo pipefail
NVM_VERSION=$(awk '/^nvm_version:/ {print $2}' software_versions.yml)
NODE_VERSION=$(awk '/^node_version:/ {print $2}' software_versions.yml)
RUNTIME_ROOT=$(mktemp -d)
trap 'rm -rf "$RUNTIME_ROOT"' EXIT
export NVM_DIR="$RUNTIME_ROOT/nvm"
mkdir -p "$NVM_DIR"
# The installer and the loader it downloads come from a tag, which can move, so each runs only if
# it is the reviewed file. A new nvm_version needs both checksums here.
case "$NVM_VERSION" in
  0.41.0)
    NVM_INSTALL_SHA256=045932fe05f0d4247c946f2ec0ec4026dbfd8ce88a5f67ea28d8ea833227f48b
    NVM_SH_SHA256=e7ffd3d192735da61e5eb04e68f1aca4a293af4cd0000864568d6de7aa8f6fc4
    ;;
  *)
    echo "::error::No reviewed install.sh and nvm.sh checksums for nvm ${NVM_VERSION}; add them to scripts/ci-runtime.sh."
    exit 1
    ;;
esac
curl -fsSL "https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh" -o "$RUNTIME_ROOT/install.sh"
echo "${NVM_INSTALL_SHA256}  $RUNTIME_ROOT/install.sh" | sha256sum --check -
PROFILE=/dev/null METHOD=script bash "$RUNTIME_ROOT/install.sh"
echo "${NVM_SH_SHA256}  $NVM_DIR/nvm.sh" | sha256sum --check -
# Use the installed loader and runtime, so a renamed tag or incompatible installer fails CI.
# shellcheck source=/dev/null
. "$NVM_DIR/nvm.sh" --no-use
nvm install "$NODE_VERSION"
test "$(node --version)" = "v${NODE_VERSION}"
