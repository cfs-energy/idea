# syntax=docker/dockerfile:1.7
# Build the control-plane image. IDEA_CONTAINER_ROLE selects the task process.
# Use the repository root as the build context:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     --build-context dcv-packages=/path/to/dcv-packages \
#     -f deployment/ecr/idea-control-plane/Dockerfile --build-arg IDEA_VERSION=26.10.0 .
# Copy the release tarballs into this directory before the build.
# The dcv-packages context contains NICE-GPG-KEY, checksums.txt, the broker RPM,
# both gateway RPMs, and both DCV archives. checksums.txt covers each package
# file, one sha256sum entry per line.
# Each architecture-dependent step declares ARG TARGETARCH inside its stage and reads the
# value the builder supplies. Do not declare TARGETARCH before the first FROM: under the
# dockerfile:1.7 frontend a global declaration with no default shadows the builder's value,
# every stage then sees an empty string, and the build only works if a single architecture is
# passed by hand, which a two-architecture build cannot do.
# Set PBS_IMAGE_REPO and PBS_IMAGE_TAG for a registry copy, or PBS_IMAGE for a full reference.

ARG IDEA_VERSION
ARG PBS_IMAGE_REPO=idea-scheduler-pbs
ARG PBS_IMAGE_TAG=v${IDEA_VERSION}
ARG PBS_IMAGE=${PBS_IMAGE_REPO}:${PBS_IMAGE_TAG}

FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS dcv-runtime

ARG TARGETARCH

ENV IDEA_APP_DEPLOY_DIR=/opt/idea/app
ENV PYTHONUNBUFFERED=1
ENV LC_ALL="en_US.UTF-8" \
    LC_CTYPE="en_US.UTF-8" \
    LANG="en_US.UTF-8"
ENV PATH="/opt/pbs/bin:/opt/pbs/sbin:${PATH}"

# Install runtime packages for the module, scheduler, broker, and gateway roles.
RUN dnf install -y --setopt=install_weak_deps=False \
      python3.13 python3.13-pip python3.13-devel \
      gcc openldap-devel cyrus-sasl-devel \
      postgresql17-server postgresql17-contrib \
      expat libedit tcl tk libical hwloc-libs \
      adcli krb5-workstation sssd-ad realmd \
      java-11-amazon-corretto-headless \
      nginx openssl nmap-ncat \
      glibc-langpack-en shadow-utils procps-ng iproute hostname sudo util-linux jq tar gzip which awscli-2 curl-minimal \
  && dnf clean all

# Install the session-manager broker, connection gateway, and web viewer from the
# supplied package context. This avoids a build-time dependency on a public host.
COPY --from=dcv-packages / /tmp/dcv-packages/
RUN set -eux; \
    case "${TARGETARCH:-}" in \
      amd64) A=x86_64 ;; \
      arm64) A=aarch64 ;; \
      *) echo "TARGETARCH must be amd64 or arm64 (build with buildx); got '${TARGETARCH:-}'" >&2; exit 1 ;; \
    esac; \
    for F in NICE-GPG-KEY checksums.txt nice-dcv-session-manager-broker-amzn2023.noarch.rpm "nice-dcv-connection-gateway-amzn2023.${A}.rpm" "nice-dcv-amzn2023-${A}.tgz"; do \
      test -f "/tmp/dcv-packages/${F}" || { echo "dcv-packages is missing ${F}" >&2; exit 1; }; \
    done; \
    (cd /tmp/dcv-packages && sha256sum -c checksums.txt); \
    rpm --import /tmp/dcv-packages/NICE-GPG-KEY; \
    rpm -K /tmp/dcv-packages/nice-dcv-session-manager-broker-amzn2023.noarch.rpm; \
    dnf install -y /tmp/dcv-packages/nice-dcv-session-manager-broker-amzn2023.noarch.rpm; \
    rpm -K "/tmp/dcv-packages/nice-dcv-connection-gateway-amzn2023.${A}.rpm"; \
    dnf install -y "/tmp/dcv-packages/nice-dcv-connection-gateway-amzn2023.${A}.rpm"; \
    cp "/tmp/dcv-packages/nice-dcv-amzn2023-${A}.tgz" /tmp/dcv.tgz; \
    mkdir -p /tmp/dcv && tar -xzf /tmp/dcv.tgz -C /tmp/dcv --strip-components=1; \
    rpm -K /tmp/dcv/nice-dcv-web-viewer-*.rpm; dnf install -y /tmp/dcv/nice-dcv-web-viewer-*.rpm; \
    rm -rf /tmp/dcv-packages /tmp/dcv.tgz /tmp/dcv; dnf clean all; \
    sed -i 's|> /dev/null|2>\&1|' /usr/share/dcv-session-manager-broker/bin/dcv-session-manager-broker.sh

FROM ${PBS_IMAGE} AS pbs

FROM dcv-runtime

ARG IDEA_VERSION

COPY --from=pbs /opt/pbs /opt/pbs

# Install all module packages from the release bundle. The selected role runs at startup.
# Keep the release bundle in /root/.idea/downloads for package commands.
ADD deployment/ecr/idea-control-plane/all-${IDEA_VERSION}.tar.gz /root/.idea/downloads/
RUN set -eux; \
    for MODULE in idea-cluster-manager idea-virtual-desktop-controller idea-scheduler; do \
      APP_NAME="${MODULE#idea-}"; \
      PKG="/tmp/idea/${MODULE}"; \
      mkdir -p "${PKG}"; \
      tar -xf "/root/.idea/downloads/${MODULE}-${IDEA_VERSION}.tar.gz" -C "${PKG}"; \
      python3.13 -m pip install --no-cache-dir -r "${PKG}/requirements.txt"; \
      python3.13 -m pip install --no-cache-dir "${PKG}"/*-lib.tar.gz; \
      mkdir -p "${IDEA_APP_DEPLOY_DIR}/${APP_NAME}"; \
      cp -r "${PKG}/resources" "${IDEA_APP_DEPLOY_DIR}/${APP_NAME}/"; \
      if [ -d "${PKG}/webapp" ]; then cp -r "${PKG}/webapp" "${IDEA_APP_DEPLOY_DIR}/${APP_NAME}/"; fi; \
    done; \
    rm -rf /root/.cache/pip; \
    mkdir -p "${IDEA_APP_DEPLOY_DIR}/logs"

# Overlay the gateway web resources.
ADD deployment/ecr/idea-control-plane/idea-dcv-connection-gateway-${IDEA_VERSION}.tar.gz /tmp/idea-gateway/
RUN cp -a /tmp/idea-gateway/static_resources/. /usr/share/dcv/www/ && rm -rf /tmp/idea-gateway /tmp/idea

# Build ideactl and include its pinned CDK CLI.
# The build uses the repository path expected by its resource-copy script.
ARG NODE_VERSION=22.23.2
RUN set -eux; \
    case "${TARGETARCH:-}" in \
      amd64) N=x64 ;; \
      arm64) N=arm64 ;; \
      *) echo "TARGETARCH must be amd64 or arm64 (build with buildx); got '${TARGETARCH:-}'" >&2; exit 1 ;; \
    esac; \
    mkdir -p /usr/local/node; \
    curl -fsSL -o /tmp/node.tar.gz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${N}.tar.gz"; \
    tar -xzf /tmp/node.tar.gz -C /usr/local/node --strip-components=1 --no-same-owner; \
    rm -f /tmp/node.tar.gz

ENV PATH="/usr/local/node/bin:/opt/idea/ideactl/node_modules/.bin:${PATH}"
ENV CDK_DISABLE_CLI_TELEMETRY=true

COPY IDEA_VERSION.txt /idea-build/IDEA_VERSION.txt
COPY source/idea/ideactl/resources /idea-build/source/idea/ideactl/resources
# scripts/copy-resources.mjs stages this tree into dist/resources/bootstrap.
COPY source/idea/idea-bootstrap /idea-build/source/idea/idea-bootstrap
COPY source/idea/ideactl/package.json source/idea/ideactl/package-lock.json source/idea/ideactl/tsconfig.json source/idea/ideactl/cdk.json /idea-build/source/idea/ideactl/
COPY source/idea/ideactl/src /idea-build/source/idea/ideactl/src
COPY source/idea/ideactl/scripts /idea-build/source/idea/ideactl/scripts
RUN set -eux; \
    cd /idea-build/source/idea/ideactl; \
    npm ci; \
    npm run build; \
    npm prune --omit=dev; \
    npm cache clean --force; \
    find node_modules -type f -name '*.d.ts' -delete; \
    mkdir -p /opt/idea; \
    mv /idea-build/source/idea/ideactl /opt/idea/ideactl; \
    rm -rf /idea-build; \
    cd /opt/idea/ideactl; \
    node /opt/idea/ideactl/dist/src/cli/main.js about

COPY deployment/ecr/idea-control-plane/nginx.conf /etc/nginx/conf.d/default.conf
COPY deployment/ecr/idea-control-plane/entrypoint.sh /opt/idea/entrypoint.sh
COPY deployment/ecr/idea-control-plane/sync_users.py /opt/idea/sync_users.py
COPY deployment/ecr/idea-control-plane/roles /opt/idea/roles
RUN chmod +x /opt/idea/entrypoint.sh /opt/idea/roles/*.sh

# module 8443; broker 8444/8445/8446 client/agent/gateway and 47100/47500 between brokers;
# gateway 8443 web and QUIC and 8989 health.
EXPOSE 8443 8444 8445 8446 8989 47100 47500

ENTRYPOINT ["/opt/idea/entrypoint.sh"]
