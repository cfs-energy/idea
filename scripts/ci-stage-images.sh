#!/usr/bin/env bash
set -euo pipefail
PACKAGE_DIR=/tmp/dcv-packages
DOWNLOAD_HOST=https://d1uj6qtbmh3dt5.cloudfront.net
mkdir -p "$PACKAGE_DIR"
curl --fail --location --silent --show-error -o "$PACKAGE_DIR/NICE-GPG-KEY" "$DOWNLOAD_HOST/NICE-GPG-KEY"
curl --fail --location --silent --show-error -o "$PACKAGE_DIR/nice-dcv-session-manager-broker-amzn2023.noarch.rpm" "$DOWNLOAD_HOST/nice-dcv-session-manager-broker-amzn2023.noarch.rpm"
curl --fail --location --silent --show-error -o "$PACKAGE_DIR/nice-dcv-connection-gateway-amzn2023.x86_64.rpm" "$DOWNLOAD_HOST/nice-dcv-connection-gateway-amzn2023.x86_64.rpm"
curl --fail --location --silent --show-error -o "$PACKAGE_DIR/nice-dcv-connection-gateway-amzn2023.aarch64.rpm" "$DOWNLOAD_HOST/nice-dcv-connection-gateway-amzn2023.aarch64.rpm"
curl --fail --location --silent --show-error -o "$PACKAGE_DIR/nice-dcv-amzn2023-x86_64.tgz" "$DOWNLOAD_HOST/nice-dcv-amzn2023-x86_64.tgz"
curl --fail --location --silent --show-error -o "$PACKAGE_DIR/nice-dcv-amzn2023-aarch64.tgz" "$DOWNLOAD_HOST/nice-dcv-amzn2023-aarch64.tgz"
(
  cd "$PACKAGE_DIR"
  sha256sum \
    nice-dcv-session-manager-broker-amzn2023.noarch.rpm \
    nice-dcv-connection-gateway-amzn2023.x86_64.rpm \
    nice-dcv-connection-gateway-amzn2023.aarch64.rpm \
    nice-dcv-amzn2023-x86_64.tgz \
    nice-dcv-amzn2023-aarch64.tgz \
    > checksums.txt
)
VERSION=$(tr -d '[:space:]' < IDEA_VERSION.txt)
cp "dist/all-${VERSION}.tar.gz" "deployment/ecr/idea-scheduler-pbs/all-${VERSION}.tar.gz"
cp "dist/all-${VERSION}.tar.gz" "deployment/ecr/idea-control-plane/all-${VERSION}.tar.gz"
cp "dist/idea-dcv-connection-gateway-${VERSION}.tar.gz" "deployment/ecr/idea-control-plane/idea-dcv-connection-gateway-${VERSION}.tar.gz"
printf 'IDEA_VERSION=%s\n' "$VERSION" >> "$GITHUB_ENV"
