# marshal backend — built by CodeBuild (repo root as context):
#   docker build -f backend/Dockerfile .
# ECR Public base image: Docker Hub anonymous pulls from AWS IPs hit 429 rate limits
FROM public.ecr.aws/docker/library/python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# uv from PyPI (avoids ghcr.io pulls)
RUN pip install --no-cache-dir "uv>=0.9,<1"

WORKDIR /app

# Dependency layer (cached until lockfile changes)
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code
COPY backend/app ./app
COPY backend/fixtures ./fixtures
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY backend/entrypoint.sh /entrypoint.sh
# Sample app template deployed into sandboxes (kept outside the package on disk)
COPY sample-app/template.json /opt/marshal/sample-app/template.json
# Ops scripts for one-off ECS tasks (seeding etc. via entrypoint command override)
COPY scripts/seed_templates.py /opt/marshal/scripts/seed_templates.py
COPY scripts/seed_marketplace.py /opt/marshal/scripts/seed_marketplace.py
# Showcase demo data (idempotent; --purge removes exactly what it seeded)
COPY scripts/seed_demo.py /opt/marshal/scripts/seed_demo.py
# Manifest-first reset + browser/API portfolio rehearsal one-off task sources.
# The .mjs driver requires a Node+Playwright runner; it is packaged here so a
# task can hand it to that runner without pulling source at execution time.
COPY scripts/reset_demo.py /opt/marshal/scripts/reset_demo.py
COPY scripts/rehearse-demo-portfolio.mjs /opt/marshal/scripts/rehearse-demo-portfolio.mjs
RUN chmod +x /entrypoint.sh && uv sync --frozen --no-dev

# S16-06 release surface: stamped by CodeBuild (--build-arg), read by
# GET /api/v1/meta/build-info. Empty in local dev (git fallback applies).
ARG GIT_SHA=""
ARG BUILD_TIME=""
ENV GIT_SHA=$GIT_SHA \
    BUILD_TIME=$BUILD_TIME

ENV PATH="/app/.venv/bin:$PATH" \
    SAMPLE_APP_TEMPLATE_PATH=/opt/marshal/sample-app/template.json

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]

# Optional runnable browser-rehearsal target. The normal/default image remains
# `backend-runtime` below; build this one explicitly with:
#   docker build --target portfolio-rehearsal -f backend/Dockerfile .
# The image/tag and npm package are intentionally pinned to the same version.
FROM mcr.microsoft.com/playwright:v1.56.1-noble AS portfolio-rehearsal
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-minimal \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
RUN npm install --prefix /workspace --no-save --package-lock=false "playwright@1.56.1"
COPY backend/fixtures ./backend/fixtures
COPY scripts/rehearse-demo-portfolio.mjs ./scripts/rehearse-demo-portfolio.mjs
ENV PYTHON_BIN=python3
ENTRYPOINT ["node", "/workspace/scripts/rehearse-demo-portfolio.mjs"]

# Keep the production backend as the default final target; the optional
# Playwright stage above is built only when explicitly selected.
FROM base AS backend-runtime
