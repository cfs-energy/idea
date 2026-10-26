# syntax=docker/dockerfile:1.28@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# Build the control-plane image. IDEA_CONTAINER_ROLE selects the task process.
# Use the repository root as the build context:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     --build-context dcv-packages=/path/to/dcv-packages \
#     -f deployment/ecr/idea-control-plane/Dockerfile --build-arg IDEA_VERSION=26.11.1 .
# Copy the release tarballs into this directory before the build.
# The dcv-packages context contains NICE-GPG-KEY, checksums.txt, the broker RPM,
# both gateway RPMs, and both DCV archives. checksums.txt covers each package
# file, one sha256sum entry per line.
# Each architecture-dependent step declares ARG TARGETARCH inside its stage and reads the
# value the builder supplies. Do not declare TARGETARCH before the first FROM: under the
# dockerfile:1.7 frontend a global declaration with no default shadows the builder's value,
# every stage then sees an empty string, and the build only works if a single architecture is
# passed by hand, which a two-architecture build cannot do.

# OpenPBS 23.06 embeds CPython and needs eval.h, removed after Python 3.10.
# AL2023 supplies python3-devel at 3.9 for OpenPBS and Python 3.13 for the modules.
# The PostgreSQL major version must match an existing PBS_HOME/datastore/PG_VERSION.
# PBS_HOME lives on shared storage; pbs_dblock permits one server at a time.
# A replacement keeps the datastore and server name while jobs stay on execution hosts.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:8272aee6146a36222baaf71d72ab6799770957414ac63b1e940cec58c9197188 AS pbs

ARG OPENPBS_VERSION=23.06.06
ARG OPENPBS_URL=https://github.com/openpbs/openpbs/archive/v23.06.06.tar.gz
ARG OPENPBS_SHA384=8a4d7f9c326fd1de5c103e700422bc4d49edc9d50f142c033e9e7de8d10d52f5c4f92e902e107b8e89d3e12c147ebef4

# python3-devel is 3.9 here, which is what supplies eval.h.
RUN dnf install -y --setopt=install_weak_deps=False \
    gcc gcc-c++ make libtool autoconf automake tar gzip \
    hwloc-devel libX11-devel libXt-devel libedit-devel libical-devel ncurses-devel \
    perl postgresql17-devel python3-devel tcl-devel tk-devel swig \
    expat-devel openssl-devel libXext-devel libXft-devel zlib-devel \
    && dnf clean all

WORKDIR /build
# A checksum failure inside the pipe has to fail the step.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN set -eux; \
    curl -fsSL -o openpbs.tar.gz "${OPENPBS_URL}"; \
    echo "${OPENPBS_SHA384}  openpbs.tar.gz" | sha384sum -c -; \
    tar xzf openpbs.tar.gz; \
    cd "openpbs-${OPENPBS_VERSION}"; \
    ./autogen.sh; \
    ./configure PBS_VERSION="${OPENPBS_VERSION}" --prefix=/opt/pbs; \
    make -j"$(nproc)"; \
    make install; \
    /opt/pbs/libexec/pbs_postinstall; \
    chmod 4755 /opt/pbs/sbin/pbs_iff /opt/pbs/sbin/pbs_rcp

FROM public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:8272aee6146a36222baaf71d72ab6799770957414ac63b1e940cec58c9197188 AS dcv-runtime

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
      adcli krb5-workstation sssd-ad sssd-ldap sssd-tools realmd authselect openssh-server openssh-clients \
      java-11-amazon-corretto-headless \
      nginx openssl nmap-ncat \
      glibc-langpack-en libatomic shadow-utils procps-ng iproute hostname sudo util-linux jq tar gzip which awscli-2 curl-minimal \
      rsync git vim-minimal vim-enhanced nano tmux \
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

FROM dcv-runtime

COPY --from=pbs /opt/pbs /opt/pbs

# Tools users had on the x86 host bastion and expect on the arm64 login node (vi, vim and
# nano are in the package list above; emacs, ~840 MB with its dependencies, is left to a
# site hook). uv comes as a pinned wheel for the build architecture; AL2023 packages no
# hdf5 (h5dump) for either.
ARG UV_VERSION=0.13.5
RUN set -eux; \
    python3.13 -m pip install --no-cache-dir "uv==${UV_VERSION}"; \
    test -x /usr/local/bin/uv || ln -s "$(command -v uv)" /usr/local/bin/uv; \
    /usr/local/bin/uv --version

# neovim for the login node: AL2023 packages none, so the release tarball, pinned and
# checked against the digest GitHub records for each asset.
ARG TARGETARCH
ARG NVIM_VERSION=0.12.5
RUN set -eux; \
    case "${TARGETARCH:-}" in \
      amd64) A=x86_64; SUM=bce0f56eda1f1b1db6eee8f4133d7a38813ea07933837dd1777411ca384c6875 ;; \
      arm64) A=arm64; SUM=1aa5ca085249580ae0f91eb14f27ec0919773ff2d99a163d03f3d6c21ac29725 ;; \
      *) echo "TARGETARCH must be amd64 or arm64 (build with buildx); got '${TARGETARCH:-}'" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/nvim.tar.gz "https://github.com/neovim/neovim/releases/download/v${NVIM_VERSION}/nvim-linux-${A}.tar.gz"; \
    printf '%s  %s\n' "${SUM}" /tmp/nvim.tar.gz > /tmp/nvim.sha256; \
    sha256sum -c /tmp/nvim.sha256; \
    mkdir -p /opt/nvim; \
    tar -xzf /tmp/nvim.tar.gz -C /opt/nvim --strip-components=1 --no-same-owner; \
    ln -sf /opt/nvim/bin/nvim /usr/local/bin/nvim; \
    rm -f /tmp/nvim.tar.gz /tmp/nvim.sha256; \
    /usr/local/bin/nvim --version > /dev/null

# Build ideactl and include its pinned CDK CLI.
# The build uses the repository path expected by its resource-copy script.
ARG NODE_VERSION=26.8.2
# Node archive checksums for 26.8.2
RUN set -eux; \
    case "${TARGETARCH:-}" in \
      amd64) N=x64; SUM=badb3fe6a61b85e1352ca6564dc56b8b7bd5ecd5c474c52acc21dd0cdd586f35 ;; \
      arm64) N=arm64; SUM=746cdbf21565b4ea06f77642bb0e85466de8bb722242be2c5e006f272c361c63 ;; \
      *) echo "TARGETARCH must be amd64 or arm64 (build with buildx); got '${TARGETARCH:-}'" >&2; exit 1 ;; \
    esac; \
    mkdir -p /usr/local/node; \
    curl -fsSL -o /tmp/node.tar.gz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${N}.tar.gz"; \
    printf '%s  %s\n' "${SUM}" /tmp/node.tar.gz > /tmp/node.sha256; \
    sha256sum -c /tmp/node.sha256; \
    tar -xzf /tmp/node.tar.gz -C /usr/local/node --strip-components=1 --no-same-owner; \
    rm -f /tmp/node.tar.gz /tmp/node.sha256

ENV PATH="/usr/local/node/bin:/opt/idea/ideactl/node_modules/.bin:${PATH}"
ENV CDK_DISABLE_CLI_TELEMETRY=true

# Keep runtime tools cached when only the application bundle changes.
ARG IDEA_VERSION
# Install all module packages from the release bundle. The selected role runs at startup.
RUN --mount=type=bind,source=deployment/ecr/idea-control-plane/all-${IDEA_VERSION}.tar.gz,target=/tmp/release.tar.gz \
    set -eux; \
    mkdir -p /tmp/release; \
    tar -xzf /tmp/release.tar.gz -C /tmp/release; \
    for MODULE in idea-cluster-manager idea-virtual-desktop-controller idea-scheduler; do \
      APP_NAME="${MODULE#idea-}"; \
      PKG="/tmp/idea/${MODULE}"; \
      mkdir -p "${PKG}"; \
      tar -xf "/tmp/release/${MODULE}-${IDEA_VERSION}.tar.gz" -C "${PKG}"; \
      python3.13 -m pip install --no-cache-dir -r "${PKG}/requirements.txt"; \
      python3.13 -m pip install --no-cache-dir "${PKG}"/*-lib.tar.gz; \
      mkdir -p "${IDEA_APP_DEPLOY_DIR}/${APP_NAME}"; \
      cp -r "${PKG}/resources" "${IDEA_APP_DEPLOY_DIR}/${APP_NAME}/"; \
      if [ -d "${PKG}/webapp" ]; then cp -r "${PKG}/webapp" "${IDEA_APP_DEPLOY_DIR}/${APP_NAME}/"; fi; \
    done; \
    rm -rf /tmp/release /tmp/idea /root/.cache/pip; \
    mkdir -p "${IDEA_APP_DEPLOY_DIR}/logs"

# Overlay the gateway web resources.
ADD deployment/ecr/idea-control-plane/idea-dcv-connection-gateway-${IDEA_VERSION}.tar.gz /tmp/idea-gateway/
RUN cp -a /tmp/idea-gateway/static_resources/. /usr/share/dcv/www/ && rm -rf /tmp/idea-gateway /tmp/idea

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
COPY deployment/ecr/idea-control-plane/roles /opt/idea/roles
COPY source/idea/idea-bootstrap/common/terminal_host.py /opt/idea/roles/terminal_host.py
RUN chmod +x /opt/idea/entrypoint.sh /opt/idea/roles/*.sh

# module 8443; broker 8444/8445/8446 client/agent/gateway and 47100/47500 between brokers;
# gateway 8443 web and QUIC and 8989 health.
EXPOSE 22 8443 8444 8445 8446 8989 47100 47500

ENTRYPOINT ["/opt/idea/entrypoint.sh"]
