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
  # A failed read stops promotion: skipping it could promote a lower candidate of the same tree.
  # Only a release that answers, and is not one of ours or has no record, is skipped.
  if ! VIEW=$(gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json author,assets \
    --jq '[.author.login, ([.assets[].name] | index("candidate.json") != null)] | @tsv'); then
    echo "::error::Could not read release ${TAG}. Promotion stopped; re-run when GitHub answers." >&2
    exit 1
  fi
  IFS=$'\t' read -r AUTHOR HAS_RECORD <<< "$VIEW"
  if [[ "$AUTHOR" != 'github-actions[bot]' ]]; then
    echo "::warning::${TAG} was not published by github-actions[bot]; skipped." >&2
    continue
  fi
  if [[ "$HAS_RECORD" != true ]]; then
    echo "::warning::${TAG} has no candidate.json; skipped." >&2
    continue
  fi
  rm -f "$WORK/candidate.json"
  if ! gh release download "$TAG" --repo "$GITHUB_REPOSITORY" --pattern candidate.json --dir "$WORK"; then
    echo "::error::Could not download candidate.json of ${TAG}. Promotion stopped; re-run when GitHub answers." >&2
    exit 1
  fi
  if ! MANIFEST=$(jq -er 'select(type == "object") | [.version, .tree] | select(all(.[]; type == "string" and length > 0)) | @tsv' "$WORK/candidate.json" 2>/dev/null); then
    echo "::warning::${TAG} has invalid or incomplete candidate.json; skipped." >&2
    continue
  fi
  IFS=$'\t' read -r CANDIDATE_VERSION CANDIDATE_TREE <<< "$MANIFEST"
  if [ "$CANDIDATE_VERSION" = "$VERSION" ] && [ "$CANDIDATE_TREE" = "$TREE" ]; then
    echo "$TAG"
    exit 0
  fi
  echo "${TAG} was built from tree ${CANDIDATE_TREE}, not ${TREE}." >&2
done
echo "::error::No release candidate for v${VERSION} was built from tree ${TREE}. Cut one from this exact source with the Build and Push dispatch (release_candidate), prove it, then merge." >&2
exit 1
