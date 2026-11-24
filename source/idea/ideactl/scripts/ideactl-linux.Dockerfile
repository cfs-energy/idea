# syntax=docker/dockerfile:1.29@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183
# Carries the Linux release file. Amazon Linux needs libatomic for the official Node binary.

FROM public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y libatomic && dnf clean all

COPY ideactl /usr/local/bin/ideactl
RUN chmod 0755 /usr/local/bin/ideactl

ENTRYPOINT ["/usr/local/bin/ideactl"]
