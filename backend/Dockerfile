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

FROM base AS backend-runtime
