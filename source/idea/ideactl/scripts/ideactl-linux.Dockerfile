# syntax=docker/dockerfile:1.29@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183
# Carries the Linux release file. Amazon Linux needs libatomic for the official Node binary.

# The same base digest as the control-plane image, which Renovate moves for both.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:8272aee6146a36222baaf71d72ab6799770957414ac63b1e940cec58c9197188

RUN dnf install -y libatomic && dnf clean all

COPY ideactl /usr/local/bin/ideactl
RUN chmod 0755 /usr/local/bin/ideactl

ENTRYPOINT ["/usr/local/bin/ideactl"]
