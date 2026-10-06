"""WorkspaceRunnerProvider: external codegen over the S3 workspace contract
(external-codegen spec R1/R4) — OQ-1's "workspace sync" made concrete.

This is the platform side of the published workspace contract
(docs/codegen-workspace-contract.md). The provider only speaks the contract:
write request → start the CodeBuild runner → observe the response prefix →
ingest. The engine on the other side is whatever the runner image contains;
the reference engine is `runner/main.py`, and any engine that speaks the
contract can replace it. Selected as codegen provider `runner` (`kiro` is
accepted as a legacy alias).
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import boto3

from app.core.config import get_settings
from app.services.codegen import contract
from app.services.codegen.provider import BuildCtx

logger = logging.getLogger("marshal.codegen")

_s3 = None
_codebuild = None


def s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=get_settings().aws_region)
    return _s3


def codebuild_client():
    global _codebuild
    if _codebuild is None:
        _codebuild = boto3.client("codebuild", region_name=get_settings().aws_region)
    return _codebuild


@dataclass(frozen=True)
class ExternalStatus:
    """One poll observation of the response prefix."""

    started: bool
    files_seen: list[str]
    results: contract.Results | None
    results_raw: dict | None = None


class WorkspaceRunnerProvider:
    name = "runner"
    mode = "external"

    def __init__(self) -> None:
        settings = get_settings()
        self.bucket = settings.codegen_workspace_bucket
        self.project = settings.codegen_runner_project
        if not self.bucket:
            raise RuntimeError("CODEGEN_WORKSPACE_BUCKET is not configured")

    # ------------------------------------------------------------ prefixes

    @staticmethod
    def prefix_root(build_id: uuid.UUID, attempt: int) -> str:
        suffix = "" if attempt <= 1 else f"-r{attempt}"
        return f"builds/{build_id}{suffix}"

    # ------------------------------------------------------------ dispatch

    async def dispatch(
        self, ctx: BuildCtx, *, slug: str, artifact_profile: str, attempt: int
    ) -> str:
        """Write the request prefix, start the runner. Returns external_job_id."""
        root = self.prefix_root(ctx.build_id, attempt)
        request = contract.build_request(
            build_id=str(ctx.build_id),
            project_slug=slug,
            project_name=ctx.project_name,
            artifact_profile=artifact_profile,
            model_id=ctx.model_id,
            token_budget=get_settings().codegen_token_budget,
            allowlist=[],  # §4.1.4 enforcement stays platform-side at the gate
            guardrail_rules=list(ctx.guardrail_rules),
            endpoint_auth=dict(ctx.endpoint_auth),
            literal_contract=dict(ctx.literal_contract),
            require_web_console=ctx.require_web_console,
            response_prefix=f"{root}/response/",
            dispatched_at=datetime.now(UTC).isoformat(),
        )

        def put_all() -> None:
            client = s3_client()
            client.put_object(
                Bucket=self.bucket,
                Key=f"{root}/request/{contract.REQUEST_KEY}",
                Body=json.dumps(request, indent=2).encode(),
                ContentType="application/json",
            )
            for doc_type, content in ctx.spec_docs.items():
                client.put_object(
                    Bucket=self.bucket,
                    Key=f"{root}/request/.kiro/specs/{slug}/{doc_type}.md",
                    Body=content.encode(),
                    ContentType="text/markdown",
                )

        await asyncio.to_thread(put_all)

        def start() -> str:
            response = codebuild_client().start_build(
                projectName=self.project,
                environmentVariablesOverride=[
                    {
                        "name": "WORKSPACE_URI",
                        "value": f"s3://{self.bucket}/{root}",
                        "type": "PLAINTEXT",
                    }
                ],
            )
            return response["build"]["id"]

        job_id = await asyncio.to_thread(start)
        logger.info("build %s dispatched to runner job %s (attempt %d)", ctx.build_id, job_id, attempt)
        return job_id

    # ------------------------------------------------------------ poll

    async def poll(self, build_id: uuid.UUID, attempt: int) -> ExternalStatus:
        root = self.prefix_root(build_id, attempt)
        response_prefix = f"{root}/response/"

        def observe() -> ExternalStatus:
            client = s3_client()
            keys: list[str] = []
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=response_prefix):
                keys.extend(obj["Key"] for obj in page.get("Contents", []))
            rel = {k[len(response_prefix):] for k in keys}
            started = contract.STARTED_KEY in rel or bool(rel)
            files_seen = sorted(
                k[len(contract.GENERATED_PREFIX):]
                for k in rel
                if k.startswith(contract.GENERATED_PREFIX)
            )
            results = None
            results_raw = None
            if contract.RESULTS_KEY in rel:
                body = client.get_object(
                    Bucket=self.bucket, Key=f"{response_prefix}{contract.RESULTS_KEY}"
                )["Body"].read()
                try:
                    results_raw = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise contract.ContractViolation(f"results.json is not valid JSON: {exc}") from exc
                results = contract.parse_results(results_raw)
            return ExternalStatus(
                started=started, files_seen=files_seen, results=results, results_raw=results_raw
            )

        return await asyncio.to_thread(observe)

    # ------------------------------------------------------------ ingest

    async def fetch_artifacts(
        self, build_id: uuid.UUID, attempt: int, results: contract.Results
    ) -> dict[str, str]:
        """Download + hash-verify the artifact tree (R5.2). Raises ContractViolation."""
        import hashlib

        root = self.prefix_root(build_id, attempt)
        response_prefix = f"{root}/response/"

        def fetch_all() -> dict[str, str]:
            client = s3_client()
            artifacts: dict[str, str] = {}
            for entry in results.files:
                key = f"{response_prefix}{contract.GENERATED_PREFIX}{entry.path}"
                try:
                    body = client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
                except client.exceptions.NoSuchKey as exc:
                    raise contract.ContractViolation(
                        f"results.json lists {entry.path} but generated/{entry.path} is missing"
                    ) from exc
                digest = hashlib.sha256(body).hexdigest()
                if digest != entry.sha256:
                    raise contract.ContractViolation(
                        f"{entry.path}: sha256 mismatch (manifest {entry.sha256[:12]}…, "
                        f"object {digest[:12]}…)"
                    )
                if entry.binary:
                    continue  # hash-verified above; S3-copied at ingest, never decoded (S10)
                try:
                    artifacts[entry.path] = body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise contract.ContractViolation(
                        f"{entry.path}: not valid UTF-8 (mark binary files with binary:true)"
                    ) from exc
            return artifacts

        return await asyncio.to_thread(fetch_all)

    # ------------------------------------------------------------ cancel

    async def cancel(self, build_id: uuid.UUID, attempt: int, external_job_id: str | None) -> None:
        """Tombstone the request prefix + stop the CodeBuild job (best effort)."""
        root = self.prefix_root(build_id, attempt)

        def do_cancel() -> None:
            s3_client().put_object(
                Bucket=self.bucket,
                Key=f"{root}/request/{contract.CANCELLED_KEY}",
                Body=json.dumps({"cancelled_at": datetime.now(UTC).isoformat()}).encode(),
                ContentType="application/json",
            )
            if external_job_id:
                try:
                    codebuild_client().stop_build(id=external_job_id)
                except Exception:  # noqa: BLE001 — job may have finished already
                    logger.info("stop_build %s skipped (already finished?)", external_job_id)

        await asyncio.to_thread(do_cancel)

    # ------------------------------------------------------------ health

    async def health(self) -> dict:
        """Runner reachability for the admin card (R6.3)."""
        def check() -> dict:
            try:
                out = codebuild_client().batch_get_projects(names=[self.project])
                found = bool(out.get("projects"))
                return {
                    "project": self.project,
                    "reachable": found,
                    "detail": None if found else "CodeBuild project not found",
                }
            except Exception as exc:  # noqa: BLE001
                return {"project": self.project, "reachable": False, "detail": str(exc)[:200]}

        return await asyncio.to_thread(check)
