#!/usr/bin/env bash
set -euo pipefail
umask 077
scratch="$RUNNER_TEMP/ideactl-private-parity"
mkdir -m 700 "$scratch"
cleanup() {
  rm -rf "$scratch" tools/parity/live tools/parity/fixtures docs/port
}
trap cleanup EXIT
# Do not echo failed assertions: captures can appear in diffs and diagnostics.
set +e
(
  set -e
  [[ "$IDEACTL_PARITY_SHA256" =~ ^[a-f0-9]{64}$ ]]
  [[ -n "${IDEACTL_PARITY_BUCKET:-}" ]]
  aws s3api get-object --bucket "$IDEACTL_PARITY_BUCKET" \
    --key captures/v1.tar.gz "$scratch/captures.tar.gz" >/dev/null
  python3 scripts/unpack-private-parity.py "$scratch/captures.tar.gz" "$IDEACTL_PARITY_SHA256"
  # Require all captures while retaining explicit platform skips (e.g. macOS builds).
  export IDEACTL_PUBLIC_CHECKOUT=1 IDEACTL_REQUIRE_PRIVATE_CAPTURES=1
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  export AWS_EC2_METADATA_DISABLED=true
  # Test code can read the captures, so it runs in a network namespace with only loopback
  # (DynamoDB Local), as this user again, with only the variables it needs: nothing of the job's
  # environment, such as the OIDC request token, reaches it. No namespace means no run. The
  # redirect stays this user's, into the private scratch directory.
  keep=(PATH="$PATH" HOME="$HOME" CI=true IDEACTL_PUBLIC_CHECKOUT=1 IDEACTL_REQUIRE_PRIVATE_CAPTURES=1
    AWS_EC2_METADATA_DISABLED=true PYTHONPATH="$PWD/.parity-oracles")
  for name in JAVA_HOME LANG TMPDIR; do
    if [[ -n "${!name:-}" ]]; then keep+=("${name}=${!name}"); fi
  done
  # shellcheck disable=SC2024
  sudo -n unshare --net -- bash -ec \
    'ip link set lo up; exec setpriv --reuid="$1" --regid="$2" --init-groups -- env -i "${@:3}" node --test --test-reporter=tap "test/**/*.test.ts"' \
    private-parity "$(id -u)" "$(id -g)" "${keep[@]}" > "$scratch/tests.tap" 2>&1
) > "$scratch/setup.log" 2>&1
status=$?
set -e
if [[ "$status" != 0 ]]; then
  echo '::error::Private parity failed. Reproduce locally with the approved capture bundle; private diagnostics are intentionally not published.'
  exit 1
fi
# Only fixed-format numeric totals are safe to send to a public Actions log.
python3 - "$scratch/tests.tap" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
if 'PRIVATE CAPTURE ABSENT' in text:
    raise SystemExit('Private parity unexpectedly skipped a capture')
for line in text.splitlines():
    if re.fullmatch(r'# (tests|suites|pass|fail|cancelled|skipped|todo) [0-9]+', line):
        print(line)
PY
