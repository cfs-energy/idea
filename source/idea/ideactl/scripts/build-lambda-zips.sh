#!/bin/bash
#
# Builds the Lambda code assets once, at image build time, so synth is a path lookup.
#
# Builds the matching Lambda asset layout and installs runtime dependencies.
#
# Usage: build-lambda-zips.sh [<lambda_functions dir>] [<output dir>]
set -euo pipefail

PKG_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
SRC_DIR="${1:-${PKG_ROOT}/dist/resources/lambda_functions}"
OUT_DIR="${2:-${PKG_ROOT}/dist/resources/lambda_assets}"
PYTHON="${PYTHON:-python3.13}"
COMMONS="idea_lambda_commons"

[[ -d "${SRC_DIR}/${COMMONS}" ]] || { echo "no ${COMMONS} under ${SRC_DIR}: run npm run build first" >&2; exit 1; }

rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}"

for dir in "${SRC_DIR}"/*/; do
  pkg=$(basename "${dir}")
  [[ "${pkg}" == "${COMMONS}" ]] && continue

  build="${OUT_DIR}/${pkg}"
  mkdir -p "${build}"
  cp -r "${SRC_DIR}/${COMMONS}" "${build}/${COMMONS}"
  cp -r "${dir%/}" "${build}/${pkg}"

  if [[ -f "${build}/${pkg}/requirements.txt" ]]; then
    mv "${build}/${pkg}/requirements.txt" "${build}/requirements.txt"
    (cd "${build}" && "${PYTHON}" -m pip install -r requirements.txt \
        --platform manylinux2014_x86_64 --only-binary=:all: --target . --upgrade)
  fi
  echo "lambda asset: ${build}"
done
