#!/usr/bin/env bash
# Scans a local control-plane image with the pinned scanner, for build_control_plane_image.
# Exit 0: no blocking finding. Exit 1: blocking findings (HIGH or CRITICAL with a fixed version,
# not accepted in .github/image-scan-allowlist). Exit 2: the scanner or its advisory database
# could not be fetched, or the scanner failed; the image was not judged, and the build still fails.
set -euo pipefail
IMAGE=${1:?usage: ci-scan-image.sh <image>}
: "${SCANNER_VERSION:?}" "${SCANNER_SHA256:?}"
SCANNER="${RUNNER_TEMP:?}/scanner"
ATTEMPTS=3
RETRY_DELAY=${SCANNER_RETRY_DELAY:-20}

unavailable() {
  echo "::error title=Scanner unavailable, image not scanned::$1 This is an outage, not a vulnerability finding: rerun the job. The build fails because the image was not scanned."
  exit 2
}

mkdir -p "$SCANNER"
curl --fail --location --silent --show-error --retry "$ATTEMPTS" --retry-delay 10 --retry-all-errors \
  -o "$SCANNER/scanner.tar.gz" \
  "https://github.com/aquasecurity/trivy/releases/download/v${SCANNER_VERSION}/trivy_${SCANNER_VERSION}_Linux-ARM64.tar.gz" ||
  unavailable "Could not download the scanner (trivy ${SCANNER_VERSION})."
if ! echo "${SCANNER_SHA256}  ${SCANNER}/scanner.tar.gz" | sha256sum --check -; then
  echo "::error title=Scanner checksum mismatch, image not scanned::The downloaded trivy ${SCANNER_VERSION} does not match its recorded checksum. It was not run. This is not a vulnerability finding."
  exit 2
fi
tar -xzf "$SCANNER/scanner.tar.gz" -C "$SCANNER" trivy

# The advisory databases are fetched first, with retries, so a download failure is never read as a
# verdict on the image. The image holds Java, so the Java database is needed too.
fetched=false
for attempt in $(seq 1 "$ATTEMPTS"); do
  if "$SCANNER/trivy" image --download-db-only --cache-dir "$SCANNER/cache" --no-progress &&
    "$SCANNER/trivy" image --download-java-db-only --cache-dir "$SCANNER/cache" --no-progress; then
    fetched=true
    break
  fi
  if [ "$attempt" -lt "$ATTEMPTS" ]; then
    echo "::warning::Advisory database download failed (attempt ${attempt} of ${ATTEMPTS}); retrying."
    sleep $((attempt * RETRY_DELAY))
  fi
done
[ "$fetched" = true ] || unavailable "The advisory database could not be downloaded after ${ATTEMPTS} attempts."

set +e
"$SCANNER/trivy" image --image-src docker --scanners vuln \
  --severity HIGH,CRITICAL --ignore-unfixed --exit-code 10 \
  --ignorefile .github/image-scan-allowlist \
  --skip-db-update --skip-java-db-update \
  --timeout 20m \
  --cache-dir "$SCANNER/cache" --no-progress \
  "$IMAGE"
status=$?
set -e
case "$status" in
  0) echo "No HIGH or CRITICAL findings with a fixed version." ;;
  10)
    echo "::error title=Vulnerabilities found::The image has HIGH or CRITICAL findings with a fixed version (table above). Fix them, or triage them as .github/image-scan-allowlist describes."
    exit 1
    ;;
  *)
    echo "::error title=Scanner failed, image not scanned::trivy exited ${status} without a verdict. This is not a vulnerability finding: check the log above and rerun."
    exit 2
    ;;
esac
