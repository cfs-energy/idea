#!/usr/bin/env bash
# Copies regenerated lock files from the current directory (the downloaded artifact) into the
# checkout named by $1. Anything that is not one of the repository's lock files, or not a
# regular file, stops the copy before anything is written.
set -euo pipefail
checkout=$1
paths=()
while IFS= read -r -d '' entry; do
  path=${entry#./}
  case "$path" in
    source/idea/ideactl/package-lock.json | source/idea/idea-cluster-manager/webapp/yarn.lock) ;;
    requirements/*.txt)
      name=${path#requirements/}
      if [[ "$name" == */* || ! -f "$checkout/requirements/${name%.txt}.in" ]]; then
        echo "::error::unexpected file in the lock artifact: $path"
        exit 1
      fi
      ;;
    *)
      echo "::error::unexpected file in the lock artifact: $path"
      exit 1
      ;;
  esac
  if [[ -L "$entry" || ! -f "$entry" || -L "$checkout/$path" ]]; then
    echo "::error::not a regular file: $path"
    exit 1
  fi
  paths+=("$path")
done < <(find . -mindepth 1 ! -type d -print0)
if [[ ${#paths[@]} -eq 0 ]]; then
  echo "::error::the lock artifact is empty"
  exit 1
fi
for path in "${paths[@]}"; do
  cp -- "$path" "$checkout/$path"
  echo "accepted $path"
done
