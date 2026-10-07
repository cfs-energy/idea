#!/usr/bin/env bash
# A release candidate is built only for a version that is not yet released, under a number not
# yet used. Its prerelease and image tag are never overwritten.
set -euo pipefail
CANDIDATE="${1:?release candidate number}"
if ! [[ "$CANDIDATE" =~ ^[1-9][0-9]*$ ]]; then
  echo "::error::Release candidate must be a positive whole number, got '${CANDIDATE}'."
  exit 1
fi
bash "$(dirname "$0")/assert-new-release.sh"
VERSION=$(tr -d '[:space:]' < IDEA_VERSION.txt)
TAG="v${VERSION}-rc.${CANDIDATE}"
ERROR_FILE=$(mktemp)
trap 'rm -f "$ERROR_FILE"' EXIT
if gh api "repos/${GITHUB_REPOSITORY}/releases/tags/${TAG}" >/dev/null 2>"$ERROR_FILE"; then
  echo "::error::Release candidate ${TAG} already exists. Use the next number."
  exit 1
fi
if ! grep -q '(HTTP 404)' "$ERROR_FILE"; then
  cat "$ERROR_FILE" >&2
  echo '::error::Could not verify that the release candidate is absent. Publication stopped.' >&2
  exit 1
fi
