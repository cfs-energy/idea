# syntax=docker/dockerfile:1.28@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# Carries the Linux release file. Amazon Linux needs libatomic for the official Node binary.

FROM public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y libatomic && dnf clean all

COPY ideactl /usr/local/bin/ideactl
RUN chmod 0755 /usr/local/bin/ideactl

ENTRYPOINT ["/usr/local/bin/ideactl"]
