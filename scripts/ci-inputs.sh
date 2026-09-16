#!/usr/bin/env bash
set -euo pipefail
# Compare with the PR base, including deleted inputs; manual and push runs prove everything.
if [ "${GITHUB_EVENT_NAME:-}" = pull_request ]; then
  git diff --name-only "$PR_BASE_SHA" HEAD > "$RUNNER_TEMP/ci-changed-paths"
else
  printf '%s\n' source/idea/ideactl/ > "$RUNNER_TEMP/ci-changed-paths"
fi
images=false
release=false
runtimes=false
if grep -Eq '(^|/)([^/]*Dockerfile[^/]*|docker-bake[^/]*)$|^(deployment/ecr/|requirements/|software_versions.yml$|source/idea/|tasks/|scripts/|IDEA_VERSION.txt$|\.dockerignore$|\.github/)' "$RUNNER_TEMP/ci-changed-paths"; then
  images=true
fi
if grep -Eq '^(source/idea/(ideactl/|idea-bootstrap/)|software_versions.yml$|IDEA_VERSION.txt$|scripts/|\.github/)' "$RUNNER_TEMP/ci-changed-paths"; then
  release=true
fi
if grep -Eq '^(software_versions.yml$|scripts/ci-(inputs|runtime)\.sh$|\.github/)' "$RUNNER_TEMP/ci-changed-paths" || [ "${GITHUB_EVENT_NAME:-}" != pull_request ]; then
  runtimes=true
fi
printf 'runtimes=%s\n' "$runtimes" >> "$GITHUB_OUTPUT"
printf 'images=%s\nrelease=%s\n' "$images" "$release" >> "$GITHUB_OUTPUT"
