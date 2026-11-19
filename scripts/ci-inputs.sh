#!/usr/bin/env bash
set -euo pipefail
# Compare with the PR base, including both sides of renames; manual and push runs prove everything.
if [ "${GITHUB_EVENT_NAME:-}" = pull_request ]; then
  # core.quotePath=false: a non-ASCII path stays as written, so the prefixes below still match it
  git -c core.quotePath=false diff --no-renames --name-only "$PR_BASE_SHA" HEAD > "$RUNNER_TEMP/ci-changed-paths"
else
  printf '%s\n' source/idea/ideactl/ > "$RUNNER_TEMP/ci-changed-paths"
fi
images=false
release=false
runtimes=false
if grep -Eq '(^|/)([^/]*Dockerfile[^/]*|docker-bake[^/]*)$|^(deployment/ecr/|requirements/|software_versions.yml$|mise(\.[^/]+)?\.toml$|hk\.pkl$|source/idea/|scripts/|IDEA_VERSION.txt$|idea-admin\.sh$|\.dockerignore$|\.github/)' "$RUNNER_TEMP/ci-changed-paths"; then
  images=true
fi
if grep -Eq '^(source/idea/(ideactl/|idea-bootstrap/)|software_versions.yml$|mise(\.[^/]+)?\.toml$|hk\.pkl$|IDEA_VERSION.txt$|scripts/|\.github/)' "$RUNNER_TEMP/ci-changed-paths"; then
  release=true
fi
if grep -Eq '^(software_versions.yml$|mise(\.[^/]+)?\.toml$|hk\.pkl$|scripts/ci-(inputs|runtime)\.sh$|\.github/)' "$RUNNER_TEMP/ci-changed-paths" || [ "${GITHUB_EVENT_NAME:-}" != pull_request ]; then
  runtimes=true
fi
printf 'runtimes=%s\n' "$runtimes" >> "$GITHUB_OUTPUT"
printf 'images=%s\nrelease=%s\n' "$images" "$release" >> "$GITHUB_OUTPUT"
# An unrelated PR must not inherit a release-version mismatch from its base.
version=false
if [ "${GITHUB_EVENT_NAME:-}" != pull_request ] || grep -Eq '^(IDEA_VERSION\.txt|idea-admin\.sh)$' "$RUNNER_TEMP/ci-changed-paths"; then
  version=true
fi
printf 'version=%s\n' "$version" >> "$GITHUB_OUTPUT"
