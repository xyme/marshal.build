# marshal reference codegen runner (external-codegen spec R2; cdk-artifacts R2).
# Build context = repo root. This image runs AS the CodeBuild environment for
# the marshal-codegen-runner project; the buildspec invokes /app/runner_main.py.
# Any engine that speaks the workspace contract can replace this image —
# see docs/codegen-workspace-contract.md.
FROM public.ecr.aws/docker/library/python:3.12-slim

# Node 20 + the vendored CDK closure (cdk-app profile, S10): synth runs HERE,
# never on the backend; generated apps may import ONLY this closure.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir boto3==1.40.16 pydantic==2.11.7 pydantic-settings==2.10.1

COPY runner/cdk-template /opt/cdk-template
RUN mkdir -p /opt/cdk-closure \
 && cp /opt/cdk-template/package.json /opt/cdk-template/package-lock.json /opt/cdk-closure/ \
 && cd /opt/cdk-closure && npm ci --no-audit --no-fund && npm cache clean --force

WORKDIR /app
# Library-mode reuse of the platform's generation logic (R2.1): the runner
# imports app.services.codegen.* and swaps the bedrock seam for a headless shim.
COPY backend/app /app/app
COPY runner/main.py /app/runner_main.py
COPY runner/synth.py /app/runner_synth.py

# CodeBuild ignores ENTRYPOINT (buildspec drives); kept for direct docker runs.
ENTRYPOINT ["python", "/app/runner_main.py"]
