#!/usr/bin/env bash
set -euo pipefail
VERSION=$(tr -d '[:space:]' < IDEA_VERSION.txt)
ERROR_FILE=$(mktemp)
trap 'rm -f "$ERROR_FILE"' EXIT
if gh api "repos/${GITHUB_REPOSITORY}/releases/tags/v${VERSION}" >/dev/null 2>"$ERROR_FILE"; then
  echo "::error::Release v${VERSION} already exists. Refusing to overwrite its archives or image tags. Bump the version before publishing."
  exit 1
fi
if ! grep -q '(HTTP 404)' "$ERROR_FILE"; then
  cat "$ERROR_FILE" >&2
  echo '::error::Could not verify that the release is absent. Publication stopped.' >&2
  exit 1
fi
