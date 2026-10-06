# syntax=docker/dockerfile:1.27@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# Carries the Linux release file. Amazon Linux needs libatomic for the official Node binary.

FROM public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y libatomic && dnf clean all

COPY ideactl /usr/local/bin/ideactl
RUN chmod 0755 /usr/local/bin/ideactl

ENTRYPOINT ["/usr/local/bin/ideactl"]
