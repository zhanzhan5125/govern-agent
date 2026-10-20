# Build is an operator action, not an Agent capability. Record the built image RepoDigest.
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}
USER root
RUN pip install --no-cache-dir pytest==8.3.5
ENTRYPOINT []
USER 65534:65534
WORKDIR /work
