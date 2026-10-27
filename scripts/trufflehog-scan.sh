#!/usr/bin/env bash
set -euo pipefail
# The one secret scan: hk and CI both run this. Exclude paths are relative, so scan from the tree root.
cd "${1:-.}"
exec trufflehog filesystem --no-update --log-level=-1 \
  --config=.github/trufflehog-config.yaml \
  --exclude-paths=.github/trufflehog-exclude-paths.txt \
  --exclude-detectors=jdbc \
  --results=verified,unknown,unverified \
  --filter-entropy=3.0 \
  --fail .
