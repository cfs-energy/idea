#!/usr/bin/env bash
# Prints the release candidate that a release publishes: the highest-numbered prerelease for this
# version whose manifest records the source tree being released. A release ships only what its
# candidate was tested as, so no match stops publication.
set -euo pipefail
VERSION=$(tr -d '[:space:]' < IDEA_VERSION.txt)
TREE=$(git rev-parse 'HEAD^{tree}')
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
CANDIDATES=$(gh release list --repo "$GITHUB_REPOSITORY" --limit 200 --json tagName,isPrerelease \
  --jq ".[] | select(.isPrerelease) | .tagName | select(test(\"^v${VERSION//./\\\\.}-rc\\\\.[1-9][0-9]*$\"))" \
  | sort -t. -k4,4nr)
for TAG in $CANDIDATES; do
  rm -f "$WORK/candidate.json"
  if ! gh release download "$TAG" --repo "$GITHUB_REPOSITORY" --pattern candidate.json --dir "$WORK" 2>/dev/null; then
    echo "::warning::${TAG} has no candidate.json; skipped." >&2
    continue
  fi
  if [ "$(jq -er '.version' "$WORK/candidate.json")" = "$VERSION" ] && [ "$(jq -er '.tree' "$WORK/candidate.json")" = "$TREE" ]; then
    echo "$TAG"
    exit 0
  fi
  echo "${TAG} was built from tree $(jq -r '.tree' "$WORK/candidate.json"), not ${TREE}." >&2
done
echo "::error::No release candidate for v${VERSION} was built from tree ${TREE}. Cut one from this exact source with the Build and Push dispatch (release_candidate), prove it, then merge." >&2
exit 1
