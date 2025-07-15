FROM public.ecr.aws/docker/library/python:3.13.3-slim@sha256:21e39cf1815802d4c6f89a0d3a166cc67ce58f95b6d1639e68a394c99310d2e5

ARG TARGETARCH

ENV OS_ARCH="${TARGETARCH:-amd64}"

WORKDIR /root

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    tar \
    unzip \
    locales \
    gcc \
    python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*


ENV DEBIAN_FRONTEND=noninteractive
ENV LC_ALL="en_US.UTF-8" \
    LC_CTYPE="en_US.UTF-8" \
    LANG="en_US.UTF-8"
ENV CDK_DISABLE_CLI_TELEMETRY=true

RUN sed -i -e "s/# $LANG.*/$LANG UTF-8/" /etc/locale.gen \
    && locale-gen "en_US.UTF-8" \
    && dpkg-reconfigure locales

# install aws cli
RUN if [ "${OS_ARCH}" = "amd64" ]; then \
        curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" ; \
    elif [ "${OS_ARCH}" = "arm64" ]; then \
        curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "awscliv2.zip" ; \
    fi && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf ./aws awscliv2.zip


# install node and npm
# RUN NODE_MAJOR=16 && rm -f /etc/apt/trusted.gpg.d/nodesource.gpg /etc/apt/sources.list.d/nodesource.list \
#     && apt install -y ca-certificates curl gnupg \
#     && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/trusted.gpg.d/nodesource.gpg \
#     && echo "deb [signed-by=/etc/apt/trusted.gpg.d/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
#     && echo "Package: nodejs" >> /etc/apt/preferences.d/preferences \
#     && echo "Pin: origin deb.nodesource.com" >> /etc/apt/preferences.d/preferences \
#     && echo "Pin-Priority: 1001" >> /etc/apt/preferences.d/preferences\
#     && apt update && apt install -y nodejs
# install nvm and node
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN set -uex && \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    NODE_MAJOR=22 && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*



# add all packaged artifacts to container
ARG PUBLIC_ECR_TAG
ENV PUBLIC_ECR_TAG=${PUBLIC_ECR_TAG}
ADD all-*.tar.gz /root/.idea/downloads/

# install administrator app
RUN mkdir -p /root/.idea/downloads/idea-administrator-${PUBLIC_ECR_TAG} && \
    tar -xvf /root/.idea/downloads/idea-administrator-*.tar.gz -C /root/.idea/downloads/idea-administrator-${PUBLIC_ECR_TAG} && \
    /bin/bash /root/.idea/downloads/idea-administrator-${PUBLIC_ECR_TAG}/install.sh && \
    rm -rf /root/.idea/downloads/idea-administrator-${PUBLIC_ECR_TAG}

CMD ["bash"]
