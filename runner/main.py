"""marshal reference codegen runner (external-codegen spec R2).

Executes ONE build per invocation against the S3 workspace contract:
read `request/` → generate (the platform's S8 logic, headless) → write
`response/generated/*` + `response/results.json`.

This container is the workspace-runner integration point: a real external
engine replaces this image and speaks the same contract
(docs/codegen-workspace-contract.md) — nothing platform-side changes.
Isolation posture (R3): Bedrock invoke + this build's workspace
prefix only; no DB, no platform APIs. The runner traps its own crashes into a
failed results manifest — a silently dead runner is the platform's
engine_timeout case, not ours.

Env: WORKSPACE_URI = s3://<bucket>/builds/<build_id>[-rN]
"""

import asyncio
import hashlib
import json
import os
import random
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

import boto3

# The backend app package ships in this image for library-mode reuse (R2.1)
from app.services.codegen import contract
from app.services.codegen import internal as internal_mod
from app.services.codegen.literals import contracts_payload, extract_contracts
from app.services.codegen.provider import (  # noqa: F401 - re-exported for engines
    BuildCtx,
    BuildPlan,
)
from app.services.codegen.validate import endpoint_auth_decision

ENGINE = {"name": "marshal-reference-runner", "version": "1.0"}
REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))

s3 = boto3.client("s3", region_name=REGION)


def log(message: str) -> None:
    print(f"[runner] {message}", flush=True)


class BudgetExhausted(Exception):
    pass


class Cancelled(Exception):
    pass


class HeadlessBedrock:
    """Drop-in for app.services.bedrock.converse: direct boto3, no DB, no
    platform settings — usage accumulates for results.json and the request's
    token budget is a HARD stop (R2.3)."""

    # Mirror of app.services.bedrock.RETRYABLE (InternalServerException: 13.5Z).
    RETRYABLE = {
        "ThrottlingException",
        "ServiceUnavailableException",
        "ModelTimeoutException",
        "InternalServerException",
    }

    def __init__(self, token_budget: int):
        from botocore.config import Config

        self.client = boto3.client(
            "bedrock-runtime",
            region_name=REGION,
            config=Config(read_timeout=180, retries={"max_attempts": 0}),
        )
        self.token_budget = token_budget
        self.used_tokens = 0
        self.usage: list[dict] = []

    async def converse(
        self, *, messages, system, model_id, max_tokens=None,
        temperature=None, top_p=None, ctx=None, enforce=True,
    ):
        if self.used_tokens >= self.token_budget:
            raise BudgetExhausted(
                f"token budget exhausted ({self.used_tokens}/{self.token_budget})"
            )
        config = {"maxTokens": min(max_tokens or 8192, 8192)}
        if temperature is not None:
            config["temperature"] = temperature
        elif top_p is not None:
            config["topP"] = top_p
        # Mirror of app.services.bedrock.additional_request_fields (13.5Y):
        # adaptive-thinking Claude models spend billed output tokens on
        # reasoningContent blocks the text-join drops — suppression keeps the
        # HARD token budget (R2.3) paying for code, not deliberation.
        fields = (
            {"thinking": {"type": "disabled"}}
            if "anthropic.claude" in model_id
            else {}
        )

        def call():
            from botocore.exceptions import ClientError

            last = None
            request_config = dict(config)
            request_fields = dict(fields)
            for attempt in range(3):
                try:
                    resp = self.client.converse(
                        modelId=model_id,
                        messages=messages,
                        system=[{"text": system}],
                        inferenceConfig=request_config,
                        **(
                            {"additionalModelRequestFields": request_fields}
                            if request_fields
                            else {}
                        ),
                    )
                    text = "".join(
                        block.get("text", "")
                        for block in resp["output"]["message"]["content"]
                    )
                    return text, resp.get("usage", {}), resp.get("stopReason")
                except ClientError as exc:
                    error = exc.response.get("Error", {})
                    code = error.get("Code", "")
                    last = exc
                    message = str(error.get("Message", "")).lower()
                    thinking_rejected = (
                        bool(request_fields)
                        and code == "ValidationException"
                        and (
                            "thinking" in message
                            or "additionalmodelrequestfields" in message
                            or "additional model request fields" in message
                        )
                    )
                    if thinking_rejected and attempt < 2:
                        request_fields = {}
                        continue
                    optional_rejected = (
                        len(request_config) > 1
                        and code == "ValidationException"
                        and any(
                            marker in message
                            for marker in (
                                "temperature",
                                "top_p",
                                "topp",
                                "top p",
                                "inferenceconfig",
                            )
                        )
                        and any(
                            marker in message
                            for marker in (
                                "not supported",
                                "unsupported",
                                "extraneous",
                                "not allowed",
                                # mirror of bedrock._optional_inference_config_rejected
                                # (5 Oct 2026): Sonnet 5 answers "`temperature` is
                                # deprecated for this model"
                                "deprecated",
                            )
                        )
                    )
                    if optional_rejected and attempt < 2:
                        request_config = {"maxTokens": request_config["maxTokens"]}
                        continue
                    # Mirror of bedrock._transient_invalid_model (13.5Y): the
                    # inference profile intermittently rejects a valid model id.
                    if (
                        code == "ValidationException"
                        and "model identifier is invalid" in message
                        and attempt < 2
                    ):
                        threading.Event().wait(min(2**attempt + random.random(), 8.0))
                        continue
                    if code in self.RETRYABLE and attempt < 2:
                        threading.Event().wait(min(2**attempt + random.random(), 8.0))
                        continue
                    raise
                except (ConnectionError, OSError) as exc:
                    last = exc
                    if attempt < 2:
                        threading.Event().wait(min(2**attempt + random.random(), 8.0))
                        continue
                    raise
            raise last

        text, usage, stop = await asyncio.to_thread(call)
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        self.used_tokens += input_tokens + output_tokens
        self.usage.append(
            {"model_id": model_id, "input_tokens": input_tokens, "output_tokens": output_tokens}
        )
        log(f"model call: {input_tokens}+{output_tokens} tokens "
            f"({self.used_tokens}/{self.token_budget} used)")
        return text, usage, stop


class SynthFailed(Exception):
    pass


def synth_and_upload(bucket: str, root: str, generated: dict[str, str]) -> tuple[list[dict], dict]:
    """cdk-app profile (cdk-artifacts R2): assemble → synth → package → upload.

    Returns (files[], synth_block) for results.json. Raises SynthFailed with
    the compiler output as the message.
    """
    import runner_synth as synth

    workdir = Path("/tmp/cdk-build")
    if workdir.exists():
        import shutil

        shutil.rmtree(workdir)
    try:
        synth.assemble_workdir(workdir, generated)
        log("running cdk synth (vendored closure, offline)")
        cdk_out = synth.run_synth(workdir)
        template_body, assets = synth.package_assembly(cdk_out, workdir / "asset-zips")
    except synth.SynthFailure as exc:
        raise SynthFailed(f"{exc}: {exc.output}") from exc

    # Full repo = generated sources + the templated scaffolding (bundle v2 is
    # clone-and-deploy ready) + synth outputs.
    for name in ("package.json", "cdk.json", "tsconfig.json"):
        generated[name] = (synth.CDK_TEMPLATE_DIR / name).read_text()
    generated["bin/app.ts"] = (synth.CDK_TEMPLATE_DIR / "bin" / "app.ts").read_text()
    generated["synth/template.json"] = template_body

    files = []
    for path, content in sorted(generated.items()):
        body = content.encode()
        write_key(
            bucket, f"{root}/response/{contract.GENERATED_PREFIX}{path}", body,
            "application/json" if path.endswith(".json") else "text/plain",
        )
        files.append(
            {"path": path, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
        )

    synth_assets = []
    for asset in assets:
        zip_bytes = Path(asset["zip_path"]).read_bytes()
        rel = f"synth/assets/{asset['source_hash']}.zip"
        write_key(bucket, f"{root}/response/{contract.GENERATED_PREFIX}{rel}",
                  zip_bytes, "application/zip")
        files.append(
            {"path": rel, "sha256": asset["sha256"], "bytes": asset["bytes"], "binary": True}
        )
        synth_assets.append(
            {
                "id": asset["id"], "path": rel, "source_hash": asset["source_hash"],
                "bucket_parameter": asset["bucket_parameter"],
                "key_parameter": asset["key_parameter"],
                "hash_parameter": asset["hash_parameter"],
            }
        )
    log(f"synth packaged: {len(assets)} assets, template {len(template_body)} bytes")
    return files, {"assets": synth_assets}


def parse_workspace(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(f"WORKSPACE_URI must be s3://bucket/prefix, got {uri!r}")
    return parsed.netloc, parsed.path.strip("/")


def read_key(bucket: str, key: str) -> bytes:
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def write_key(bucket: str, key: str, body: bytes, content_type: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


def is_cancelled(bucket: str, root: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=f"{root}/request/{contract.CANCELLED_KEY}")
        return True
    except Exception:  # noqa: BLE001 — 404 = not cancelled
        return False


def write_results(bucket: str, root: str, doc: dict) -> None:
    write_key(
        bucket, f"{root}/response/{contract.RESULTS_KEY}",
        json.dumps(doc, indent=2).encode(), "application/json",
    )


async def run(bucket: str, root: str) -> int:
    request = json.loads(read_key(bucket, f"{root}/request/{contract.REQUEST_KEY}"))
    log(f"build {request['build_id']} · profile {request['artifact_profile']} "
        f"· budget {request['token_budget']} tokens")
    write_key(
        bucket, f"{root}/response/{contract.STARTED_KEY}",
        json.dumps({"engine": ENGINE, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}).encode(),
        "application/json",
    )

    slug = request["project_slug"]
    spec_docs: dict[str, str] = {}
    for doc_type in ("requirements", "design", "tasks"):
        key = f"{root}/request/.kiro/specs/{slug}/{doc_type}.md"
        try:
            spec_docs[doc_type] = read_key(bucket, key).decode()
        except Exception:  # noqa: BLE001 — tasks.md is optional
            pass

    bedrock = HeadlessBedrock(int(request["token_budget"]))
    internal_mod.converse = bedrock.converse  # library-mode seam swap (R2.1)
    provider = internal_mod.InternalProvider()

    import uuid as uuid_mod

    requirements = spec_docs.get("requirements", "")
    endpoint_auth = request.get("endpoint_auth") or endpoint_auth_decision(requirements)
    literal_contract = request.get("literal_contract") or contracts_payload(
        extract_contracts(requirements)
    )
    ctx = BuildCtx(
        build_id=uuid_mod.UUID(request["build_id"]),
        project_id=uuid_mod.uuid4(),  # unused headless; providers never touch the DB
        project_name=request["project_name"],
        user_id=None,
        spec_docs=spec_docs,
        model_id=request["model_id"],
        max_tokens=8192,
        guardrail_rules=list(request.get("guardrail_rules", [])),
        endpoint_auth=endpoint_auth,
        require_api_key=endpoint_auth.get("mode") == "key_required",
        require_web_console=bool(request.get("require_web_console", False)),
        literal_contract=literal_contract,
    )

    generated: dict[str, str] = {}

    def flush_generated() -> list[dict]:
        files = []
        for path, content in sorted(generated.items()):
            body = content.encode()
            write_key(
                bucket, f"{root}/response/{contract.GENERATED_PREFIX}{path}", body,
                "application/json" if path.endswith(".json") else "text/plain",
            )
            files.append(
                {"path": path, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
            )
        return files

    profile = request.get("artifact_profile", "inline-cfn")
    plan = None
    try:
        log("planning")
        plan = await provider.plan(ctx, profile=profile)
        for index, planned in enumerate(plan.files, start=1):
            if is_cancelled(bucket, root):
                raise Cancelled()
            log(f"generating {index}/{len(plan.files) + 1}: {planned.path}")
            generated[planned.path] = await provider.generate_file(ctx, plan, planned, generated)
            # Flush incrementally so the platform's poll shows real progress
            body = generated[planned.path].encode()
            write_key(
                bucket, f"{root}/response/{contract.GENERATED_PREFIX}{planned.path}",
                body, "text/plain",
            )
        if is_cancelled(bucket, root):
            raise Cancelled()

        synth_block = None
        if profile == "cdk-app":
            files, synth_block = synth_and_upload(bucket, root, generated)
        else:
            log(f"assembling template.json ({len(plan.files) + 1}/{len(plan.files) + 1})")
            generated["template.json"] = await provider.assemble_template(ctx, plan, generated)
            files = flush_generated()
        write_results(
            bucket, root,
            contract.results_manifest(
                status="succeeded", files=files, usage=bedrock.usage, engine=ENGINE,
                app_name=plan.app_name, architecture_notes=plan.architecture_notes,
                synth=synth_block,
            ),
        )
        log(f"succeeded: {len(files)} files, {bedrock.used_tokens} tokens")
        return 0
    except SynthFailed as exc:
        files = flush_generated()
        write_results(
            bucket, root,
            contract.results_manifest(
                status="failed", files=files, usage=bedrock.usage, engine=ENGINE,
                app_name=plan.app_name if plan else None,
                error={"code": "synth_failed", "message": str(exc)[:2000]},
            ),
        )
        return 0
    except Cancelled:
        log("cancelled by platform tombstone — stopping without results")
        return 0
    except BudgetExhausted as exc:
        files = flush_generated()  # partials aid diagnosis (R2.3)
        write_results(
            bucket, root,
            contract.results_manifest(
                status="failed", files=files, usage=bedrock.usage, engine=ENGINE,
                app_name=plan.app_name if plan else None,
                error={"code": "token_budget_exhausted", "message": str(exc)},
            ),
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — trap crashes into results (R2 doc)
        log(f"crashed: {exc}\n{traceback.format_exc()}")
        files = flush_generated()
        write_results(
            bucket, root,
            contract.results_manifest(
                status="failed", files=files, usage=bedrock.usage, engine=ENGINE,
                error={"code": "engine_crash", "message": str(exc)[:500]},
            ),
        )
        return 0


# ---------------------------------------------------------------- packaging
# MARSHAL_MODE=package (agent-substance R1): pip-install the exact-pinned
# requirements into a target tree, add the generated handler modules, and
# return a DETERMINISTIC zip (sorted entries, fixed timestamps) plus an
# SBOM-lite manifest (name/version/license via importlib.metadata). pip runs
# HERE, never on the control plane.

_PKG_MAX_ZIP_BYTES = 45 * 1024 * 1024
_PKG_PIP_TIMEOUT_S = 300


def _pkg_results(bucket: str, root: str, doc: dict) -> None:
    doc.setdefault("contract_version", contract.CONTRACT_VERSION)
    write_key(
        bucket, f"{root}/response/{contract.PACKAGE_RESULTS_KEY}",
        json.dumps(doc, indent=2).encode(), "application/json",
    )


def _deterministic_zip(tree: Path) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in tree.rglob("*") if p.is_file()):
            rel = str(path.relative_to(tree))
            if "__pycache__" in rel or rel.endswith((".pyc", ".pyo")):
                continue
            info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def _installed_manifest(tree: Path) -> list[dict]:
    import importlib.metadata as md

    packages: list[dict] = []
    for dist in md.distributions(path=[str(tree)]):
        meta = dist.metadata
        license_text = (meta.get("License") or "").strip()
        if not license_text or len(license_text) > 100:
            classifier = [
                c.split("::")[-1].strip()
                for c in (meta.get_all("Classifier") or [])
                if c.startswith("License ::")
            ]
            license_text = classifier[0] if classifier else (license_text[:100] or "unknown")
        packages.append(
            {
                "name": (meta.get("Name") or "unknown")[:80],
                "version": (dist.version or "")[:40],
                "license": license_text[:120],
            }
        )
    return sorted(packages, key=lambda p: p["name"].lower())


def run_package(bucket: str, root: str) -> int:
    """One packaging pass. Failures land in package-results.json — a silently
    dead packager is the platform's timeout case, not ours."""
    import shutil
    import subprocess

    try:
        request = json.loads(
            read_key(bucket, f"{root}/request/{contract.PACKAGE_REQUEST_KEY}")
        )
        requirements_txt = str(request.get("requirements_txt") or "")
        errors = contract.requirements_pin_errors(requirements_txt)
        if errors:  # defense in depth — the platform validated pre-dispatch
            _pkg_results(bucket, root, {
                "status": "failed",
                "error": {"code": "bad_requirements", "message": "; ".join(errors)[:800]},
            })
            return 0
        workdir = Path("/tmp/package-build")
        if workdir.exists():
            shutil.rmtree(workdir)
        target = workdir / "tree"
        target.mkdir(parents=True)
        req_file = workdir / "requirements.txt"
        req_file.write_text(requirements_txt)
        log(f"pip install -r requirements.txt ({len(requirements_txt.splitlines())} lines)")
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--no-cache-dir", "--disable-pip-version-check", "--no-compile",
             "--target", str(target), "-r", str(req_file)],
            capture_output=True, text=True, timeout=_PKG_PIP_TIMEOUT_S,
        )
        if proc.returncode != 0:
            _pkg_results(bucket, root, {
                "status": "failed",
                "error": {"code": "pip_failed",
                          "message": (proc.stderr or proc.stdout)[-1500:]},
            })
            return 0
        manifest = _installed_manifest(target)
        for name, content in (request.get("files") or {}).items():
            clean = str(name).strip()
            if "/" in clean or "\\" in clean or not clean.endswith(".py"):
                _pkg_results(bucket, root, {
                    "status": "failed",
                    "error": {"code": "bad_files",
                              "message": f"file name escapes the zip root: {clean!r}"},
                })
                return 0
            (target / clean).write_text(str(content))
        zip_bytes = _deterministic_zip(target)
        if len(zip_bytes) > _PKG_MAX_ZIP_BYTES:
            _pkg_results(bucket, root, {
                "status": "failed",
                "error": {"code": "package_too_large",
                          "message": f"package is {len(zip_bytes)} bytes "
                                     f"(limit {_PKG_MAX_ZIP_BYTES})"},
            })
            return 0
        digest = hashlib.sha256(zip_bytes).hexdigest()
        write_key(bucket, f"{root}/response/{contract.PACKAGE_ZIP_KEY}",
                  zip_bytes, "application/zip")
        write_key(
            bucket, f"{root}/response/{contract.PACKAGE_MANIFEST_KEY}",
            json.dumps({"packages": manifest}, indent=2).encode(), "application/json",
        )
        _pkg_results(bucket, root, {
            "status": "succeeded",
            "zip_sha256": digest,
            "zip_bytes": len(zip_bytes),
            "packages": manifest,
        })
        log(f"packaged: {len(zip_bytes)} bytes, {len(manifest)} distributions")
        return 0
    except Exception as exc:  # noqa: BLE001 — trap crashes into results
        log(f"packaging crashed: {exc}\n{traceback.format_exc()}")
        try:
            _pkg_results(bucket, root, {
                "status": "failed",
                "error": {"code": "packaging_crash", "message": str(exc)[:500]},
            })
        except Exception:  # noqa: BLE001
            pass
        return 0


def main() -> int:
    uri = os.environ.get("WORKSPACE_URI", "")
    bucket, root = parse_workspace(uri)
    log(f"workspace: s3://{bucket}/{root}")
    if os.environ.get("MARSHAL_MODE", "").strip().lower() == "package":
        return run_package(bucket, root)
    return asyncio.run(run(bucket, root))


if __name__ == "__main__":
    sys.exit(main())
