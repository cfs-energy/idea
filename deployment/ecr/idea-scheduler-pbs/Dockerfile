# The scheduler module plus the OpenPBS server, scheduler and comm daemons in one
# container. They belong together: the hooks run inside pbs_server and the module shells
# out to /opt/pbs/bin, so splitting them would put a network hop in the middle of both.
#
# The base is Amazon Linux 2023, the same as the module host, for two reasons that are
# not preferences:
#   - OpenPBS 23.06 embeds CPython and needs eval.h, which CPython removed after 3.10.
#     AL2023 carries python3-devel at 3.9 for the build and python3.13 for the module,
#     so one image serves both.
#   - The datastore is a PostgreSQL cluster. Its major version has to match the packages
#     here or an existing PBS_HOME cannot be reused. Check datastore/PG_VERSION.
#
# PBS_HOME lives on shared storage, which is the configuration OpenPBS uses for failover;
# pbs_dblock guards it, so exactly one of these runs at a time. A replacement keeps the
# datastore and the server name, and running jobs stay on their execution hosts.

ARG OPENPBS_VERSION=23.06.06

FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS pbs-builder

ARG OPENPBS_VERSION
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


FROM public.ecr.aws/amazonlinux/amazonlinux:2023

ARG IDEA_VERSION
ARG MODULE=idea-scheduler

ENV IDEA_APP_DEPLOY_DIR=/opt/idea/app
ENV PYTHONUNBUFFERED=1
ENV LC_ALL="en_US.UTF-8" \
    LC_CTYPE="en_US.UTF-8" \
    LANG="en_US.UTF-8"
ENV PATH="/opt/pbs/bin:/opt/pbs/sbin:${PATH}"

# postgresql17-server carries the datastore binaries and creates the postgres user the
# PBS database runs as. glibc-langpack-en provides en_US, which the module requires and
# a bare container image does not have.
RUN dnf install -y --setopt=install_weak_deps=False \
    python3.13 python3.13-pip python3.13-devel \
    gcc openldap-devel cyrus-sasl-devel \
    postgresql17-server postgresql17-contrib \
    expat libedit tcl tk libical hwloc-libs \
    glibc-langpack-en shadow-utils procps-ng iproute hostname sudo tar gzip which awscli-2 curl-minimal \
    && dnf clean all

COPY --from=pbs-builder /opt/pbs /opt/pbs

ADD all-${IDEA_VERSION}.tar.gz /tmp/idea/

RUN set -eux; \
    APP_NAME="${MODULE#idea-}"; \
    PACKAGE_DIR="/tmp/idea/${MODULE}"; \
    mkdir -p "${PACKAGE_DIR}"; \
    tar -xf "/tmp/idea/${MODULE}-${IDEA_VERSION}.tar.gz" -C "${PACKAGE_DIR}"; \
    python3.13 -m pip install --no-cache-dir -r "${PACKAGE_DIR}/requirements.txt"; \
    python3.13 -m pip install --no-cache-dir "${PACKAGE_DIR}"/*-lib.tar.gz; \
    mkdir -p "${IDEA_APP_DEPLOY_DIR}/${APP_NAME}" "${IDEA_APP_DEPLOY_DIR}/logs"; \
    cp -r "${PACKAGE_DIR}/resources" "${IDEA_APP_DEPLOY_DIR}/${APP_NAME}/"; \
    rm -rf /tmp/idea

COPY entrypoint.sh /opt/idea/entrypoint.sh
COPY sync_users.py /opt/idea/sync_users.py
RUN chmod +x /opt/idea/entrypoint.sh

ENTRYPOINT ["/opt/idea/entrypoint.sh"]
