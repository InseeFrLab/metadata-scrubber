# syntax=docker/dockerfile:1
# ============================================================
# Build Metadata Scrubber
# ============================================================
#
# Phase de build : ghcr.io/astral-sh/uv (image uv avec Python 3.13 bookworm-slim)
# Runtime        : python 3.13 bookworm-slim (image officielle)
# Entrée         : scrubber-web → FastAPI sur le port 8000
# Secrets        : injectés en variables d'environnement
# ============================================================

# --- Stage 1 : compilation des dépendances + du projet ---
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

# 1. Dépendances seules (cache Docker maximal)
COPY pyproject.toml uv.lock ./

# 2. Code source + le projet avec ses entrypoints console
COPY . .
# venv construit directement dans /.venv
ENV UV_PROJECT_ENVIRONMENT=/.venv

RUN uv sync --frozen

# --- Stage 2 : image finale minimale ---
FROM python:3.13-slim

WORKDIR /app

# Virtualenv + code depuis builder
COPY --from=builder /app/.venv  /.venv
COPY --from=builder /app/src    ./src
COPY --from=builder /app/pyproject.toml  ./

ENV PATH="/.venv/bin:$PATH"

RUN ls -alR /.venv

CMD ["scrubber-web"]
