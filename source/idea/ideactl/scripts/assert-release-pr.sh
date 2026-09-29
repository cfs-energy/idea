#!/usr/bin/env bash
# A pull request titled with a version is a release. Its version file and changelog heading must
# already say that version, and the release must not exist, or main publishes nothing after merge.
set -euo pipefail
TITLE_VERSION=$(printf '%s' "${PR_TITLE:-}" | grep -oE '^[0-9]{2}\.[0-9]{2}\.[0-9]+' || true)
if [ -z "$TITLE_VERSION" ]; then
  echo "Not a release pull request."
  exit 0
fi
VERSION=$(tr -d '[:space:]' < IDEA_VERSION.txt)
if [ "$VERSION" != "$TITLE_VERSION" ]; then
  echo "::error::The title says ${TITLE_VERSION} but IDEA_VERSION.txt says ${VERSION}. Bump the version files in this pull request."
  exit 1
fi
if ! grep -qF "## [${TITLE_VERSION}]" CHANGELOG.md; then
  echo "::error::CHANGELOG.md has no ## [${TITLE_VERSION}] heading. Rename [Unreleased] in this pull request."
  exit 1
fi
bash source/idea/ideactl/scripts/assert-new-release.sh
