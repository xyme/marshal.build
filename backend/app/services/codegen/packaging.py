"""packaged-cfn packaging pass (agent-substance R1).

The platform NEVER runs pip: after in-process generation, ONE packaging job
is dispatched to the existing marshal-codegen-runner CodeBuild project
(MARSHAL_MODE=package) on the same workspace bucket. The runner pip-installs
the exact-pinned requirements into a target tree, adds the generated
handlers, and returns a deterministic zip + SBOM-lite manifest
(package-manifest.json: name/version/license per dependency — the S14-03
discipline applied to generated agents).

Custody: the zip lands in the workspace `builds/` prefix (30-day lifecycle)
and is server-side-copied to `artifacts/<build_id>/package.zip` (durable,
180-day) as a binary CodegenArtifact row. The deployer stages it into the
Enclave at deploy time; teardown deletes the staged copy (B19 lesson).
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

import boto3

from app.core.config import get_settings
from app.services.codegen import contract

logger = logging.getLogger("marshal.codegen")

PACKAGING_TIMEOUT_S = 420
PACKAGING_POLL_S = 10


class PackagingFailed(Exception):
    """Packaging pass failed — the build fails with this detail."""


def _pkg_root(build_id: uuid.UUID) -> str:
    return f"builds/{build_id}-pkg"


def _s3():
    return boto3.client("s3", region_name=get_settings().aws_region)


def _codebuild():
    return boto3.client("codebuild", region_name=get_settings().aws_region)


async def run_packaging(
    build_id: uuid.UUID, files: dict[str, str], requirements_txt: str
) -> dict:
    """Dispatch → poll → verify → copy. Returns the manifest block
    {zip_sha256, zip_bytes, packages, codebuild_id, artifact_s3_key}."""
    settings = get_settings()
    bucket = settings.codegen_workspace_bucket
    if not bucket:
        raise PackagingFailed(
            "packaged profile needs the workspace runner infrastructure "
            "(CODEGEN_WORKSPACE_BUCKET is unset)"
        )
    errors = contract.requirements_pin_errors(requirements_txt)
    if errors:  # defense in depth — the gate pre-checks before we get here
        raise PackagingFailed("; ".join(errors))

    root = _pkg_root(build_id)
    request = contract.package_request(
        build_id=str(build_id),
        files=files,
        requirements_txt=requirements_txt,
        response_prefix=f"{root}/response/",
        dispatched_at=datetime.now(UTC).isoformat(),
    )

    def dispatch() -> str:
        _s3().put_object(
            Bucket=bucket,
            Key=f"{root}/request/{contract.PACKAGE_REQUEST_KEY}",
            Body=json.dumps(request).encode(),
            ContentType="application/json",
        )
        resp = _codebuild().start_build(
            projectName=settings.codegen_runner_project,
            environmentVariablesOverride=[
                {"name": "WORKSPACE_URI", "value": f"s3://{bucket}/{root}"},
                {"name": "MARSHAL_MODE", "value": "package"},
            ],
        )
        return resp["build"]["id"]

    job_id = await asyncio.to_thread(dispatch)
    logger.info("packaging dispatched for build %s: %s", build_id, job_id)

    results = await _poll_results(bucket, root)
    if results.status == "failed":
        error = results.error or {}
        raise PackagingFailed(
            f"{error.get('code', 'packaging_failure')}: "
            f"{str(error.get('message', 'no detail'))[:800]}"
        )

    # Verify the zip against the manifest, then copy to the durable prefix.
    source_key = f"{root}/response/{contract.PACKAGE_ZIP_KEY}"
    dest_key = f"artifacts/{build_id}/{contract.PACKAGE_ZIP_KEY}"

    def verify_and_copy() -> None:
        import hashlib

        body = _s3().get_object(Bucket=bucket, Key=source_key)["Body"].read()
        digest = hashlib.sha256(body).hexdigest()
        if digest != results.zip_sha256 or len(body) != results.zip_bytes:
            raise contract.ContractViolation(
                f"package.zip hash/size mismatch (got {digest[:12]}…/{len(body)})"
            )
        _s3().put_object(
            Bucket=bucket, Key=dest_key, Body=body, ContentType="application/zip"
        )

    await asyncio.to_thread(verify_and_copy)
    return {
        "zip_sha256": results.zip_sha256,
        "zip_bytes": results.zip_bytes,
        "packages": results.packages,
        "codebuild_id": job_id,
        "artifact_s3_key": dest_key,
    }


async def _poll_results(bucket: str, root: str) -> contract.PackageResults:
    key = f"{root}/response/{contract.PACKAGE_RESULTS_KEY}"
    waited = 0
    while waited < PACKAGING_TIMEOUT_S:
        await asyncio.sleep(PACKAGING_POLL_S)
        waited += PACKAGING_POLL_S

        def fetch() -> bytes | None:
            try:
                return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
            except Exception:  # noqa: BLE001 — 404 until the runner finishes
                return None

        raw = await asyncio.to_thread(fetch)
        if raw is not None:
            try:
                return contract.parse_package_results(json.loads(raw))
            except (json.JSONDecodeError, contract.ContractViolation) as exc:
                raise PackagingFailed(f"package-results.json invalid: {exc}") from exc
    raise PackagingFailed(
        f"packaging produced no results within {PACKAGING_TIMEOUT_S}s"
    )
