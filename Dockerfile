# syntax=docker/dockerfile:1.7

# Multi-stage build. The runtime stage is a slim Debian Python rather than
# distroless: distroless carries no Python interpreter, and the `python3`
# variants are deprecated upstream. Everything else follows the same shape as
# the Rust servers in this fleet — bases pinned by digest, dependencies cached
# in a layer separate from source, non-root at runtime, OCI labels from build
# args.

# Digest pinned to python:3.13-slim-bookworm (OCI index). Update via Renovate.
FROM python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1 AS builder

# uv comes from its own digest-pinned image rather than `pip install uv`, so the
# resolver version is pinned by the same mechanism as everything else and the
# builder never reaches PyPI for its own toolchain.
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /uvx /usr/local/bin/

# The venv is built at its FINAL path, not under /build. uv writes an absolute
# interpreter path into every console-script shebang, so a venv built at
# /build/.venv and copied to /app/.venv exec's `/build/.venv/bin/python3` and
# dies with "no such file or directory" — at container start, not at build.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /build

# Dependency layer, separate from source. `--no-install-project` resolves and
# installs everything in uv.lock *except* listmonk-mcp itself, so editing
# src/ does not invalidate the slow layer. `--frozen` fails rather than
# silently re-resolving if uv.lock and pyproject.toml have drifted apart.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Source layer. README.md is required: pyproject declares it as the readme, so
# the hatchling build fails without it.
COPY README.md ./
COPY src ./src
# `--no-editable` matters: uv installs the project editable by default, which
# leaves a .pth pointing at /build/src. That path does not exist in the runtime
# stage, so the entrypoint dies with ModuleNotFoundError at container start.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Runtime.
FROM python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

ARG BUILD_VERSION=0.2.0
ARG BUILD_REVISION=unknown
ARG BUILD_CREATED=unknown
LABEL org.opencontainers.image.title="listmonk-mcp" \
      org.opencontainers.image.description="Streamable-HTTP MCP server for Listmonk newsletter management" \
      org.opencontainers.image.url="https://forge.oddie.app/jlxq0/listmonk-mcp" \
      org.opencontainers.image.source="https://forge.oddie.app/jlxq0/listmonk-mcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_REVISION}" \
      org.opencontainers.image.created="${BUILD_CREATED}"

# Read back by the /health endpoint, so a running pod can be tied to a commit
# without shelling into it.
ENV LISTMONK_MCP_BUILD_REVISION=${BUILD_REVISION} \
    LISTMONK_MCP_TRANSPORT=streamable-http \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# UID 65532 matches the distroless `nonroot` the Rust servers run as, so one
# PodSecurityContext covers every server in the fleet.
RUN groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --home-dir /home/nonroot --create-home nonroot

WORKDIR /app
COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv

USER 65532:65532

# 3000 is the public listener. 9090 is metrics and is deliberately NOT exposed:
# it binds to {POD_IP} or loopback, is reached inside the cluster, and must not
# be routable from the Service.
EXPOSE 3000

ENTRYPOINT ["listmonk-mcp"]
