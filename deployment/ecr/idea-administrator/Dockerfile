# Use a specific Python 3.9 slim image from Docker Hub
FROM public.ecr.aws/docker/library/python:3.9.19-slim@sha256:fd8693fc6115345452a19654c1388bc9a1029cda7b98e1bce45ede67e74190f0

# Set build-time variables
ARG TARGETARCH
ARG PUBLIC_ECR_TAG

# Environment settings
ENV OS_ARCH="${TARGETARCH:-amd64}" \
    DEBIAN_FRONTEND=noninteractive \
    LC_ALL="en_US.UTF-8" \
    LC_CTYPE="en_US.UTF-8" \
    LANG="en_US.UTF-8"

# Setting the working directory to /idea
WORKDIR /idea

# Adding necessary packages
RUN apt-get update && \
    apt-get install -y curl tar unzip locales gcc python3-dev && \
    apt-get clean && \
    sed -i -e "s/# $LANG.*/$LANG UTF-8/" /etc/locale.gen && \
    locale-gen "en_US.UTF-8" && \
    dpkg-reconfigure locales

# Install AWS CLI
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-${OS_ARCH}.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf ./aws awscliv2.zip

# Install Node.js
RUN apt-get update && \
    apt-get install -y ca-certificates curl gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_18.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y nodejs

# Create a non-root user 'idea' and switch to it
RUN groupadd -r idea && \
    useradd --no-log-init -r -g idea idea && \
    chown -R idea:idea /idea

USER idea

# Add all packaged artifacts to the container
ADD --chown=idea:idea all-*.tar.gz /idea/.idea/downloads/

# Install administrator app
RUN mkdir -p /idea/.idea/downloads/idea-administrator-${PUBLIC_ECR_TAG} && \
    tar -xvf /idea/.idea/downloads/idea-administrator-*.tar.gz -C /idea/.idea/downloads/idea-administrator-${PUBLIC_ECR_TAG} && \
    /bin/bash /idea/.idea/downloads/idea-administrator-${PUBLIC_ECR_TAG}/install.sh && \
    rm -rf /idea/.idea/downloads/idea-administrator-${PUBLIC_ECR_TAG}

# Set the default command for the container
CMD ["bash"]
