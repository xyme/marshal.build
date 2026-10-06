"""Workspace-sync contract v1 (external-codegen spec R1).

The wire format between marshal and ANY codegen engine:

    s3://<bucket>/builds/<build_id>[-r2]/
      request/
        build-request.json
        .kiro/specs/<slug>/{requirements,design,tasks}.md
        cancelled.json            # tombstone — engines must stop, results ignored
      response/
        started.json              # optional early marker {engine, at}
        generated/<path>          # artifact tree
        results.json              # completion manifest (schema below)

Pure functions only — no boto3, no DB — so the runner imports this module too
and the schemas cannot drift between the two sides (R1.3: the doc examples are
validated against these functions by test).
"""

import re
from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = 1

REQUEST_KEY = "build-request.json"
RESULTS_KEY = "results.json"
STARTED_KEY = "started.json"
CANCELLED_KEY = "cancelled.json"
GENERATED_PREFIX = "generated/"

RESULT_STATUSES = ("succeeded", "failed")


class ContractViolation(Exception):
    """Engine response violates the contract — fails the build with detail."""


def build_request(
    *,
    build_id: str,
    project_slug: str,
    project_name: str,
    artifact_profile: str,
    model_id: str,
    token_budget: int,
    allowlist: list[str],
    guardrail_rules: list[str],
    response_prefix: str,
    dispatched_at: str,
    endpoint_auth: dict | None = None,
    literal_contract: dict | None = None,
    require_web_console: bool = False,
) -> dict:
    """The request manifest an engine reads (additive B21/B23 fields)."""
    return {
        "contract_version": CONTRACT_VERSION,
        "build_id": build_id,
        "project_slug": project_slug,
        "project_name": project_name,
        "artifact_profile": artifact_profile,
        "model_id": model_id,
        "token_budget": token_budget,
        "allowlist": allowlist,
        "guardrail_rules": guardrail_rules,
        "endpoint_auth": endpoint_auth
        or {"mode": "key_required", "source": "default"},
        "literal_contract": literal_contract or {"version": 1, "entries": []},
        "require_web_console": bool(require_web_console),
        "response_prefix": response_prefix,
        "dispatched_at": dispatched_at,
    }


ARTIFACT_PROFILES = ("inline-cfn", "cdk-app", "packaged-cfn")

# ---- packaged-cfn packaging pass (agent-substance R1) -----------------------
# A SECOND workspace exchange on the same bucket + runner project: the
# platform generates artifacts in-process (internal provider), then dispatches
# ONE packaging job (MARSHAL_MODE=package) that pip-installs the exact-pinned
# requirements INTO the runner container — pip never runs on the control
# plane — and returns a deterministic Lambda zip + SBOM-lite manifest.
#
#     s3://<bucket>/builds/<build_id>-pkg/
#       request/package-request.json    # {files, requirements_txt, ...}
#       response/package.zip            # the Lambda deployment package
#       response/package-manifest.json  # SBOM-lite (browsable artifact)
#       response/package-results.json   # completion manifest

PACKAGE_REQUEST_KEY = "package-request.json"
PACKAGE_RESULTS_KEY = "package-results.json"
PACKAGE_ZIP_KEY = "package.zip"
PACKAGE_MANIFEST_KEY = "package-manifest.json"

MAX_PACKAGED_DEPENDENCIES = 8
MAX_PACKAGE_ZIP_BYTES = 45 * 1024 * 1024  # Lambda's 50MB zipped limit, headroom

# Exact-pin discipline (S14-03 applied to generated agents): name==version,
# optionally with extras. Ranges, URLs, editable installs and options refused.
PIN_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.!+_-]+$"
)


def requirements_pin_errors(requirements_txt: str) -> list[str]:
    """Validate the requirements manifest both platform-side (pre-dispatch)
    and runner-side (defense in depth). Returns human-readable errors."""
    errors: list[str] = []
    pins = [
        line.strip()
        for line in (requirements_txt or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # An EMPTY manifest is valid (13.5Z): agents pick the packaged profile for
    # SIZE (handlers past CloudFormation's 4 KB inline ceiling) at least as
    # often as for third-party libraries, and stdlib + boto3 need no pin.
    # `pip install -r` of an empty file is a no-op; the zip is just the modules.
    if len(pins) > MAX_PACKAGED_DEPENDENCIES:
        errors.append(
            f"requirements.txt declares {len(pins)} dependencies "
            f"(max {MAX_PACKAGED_DEPENDENCIES})"
        )
    for line in pins:
        if not PIN_RE.match(line):
            errors.append(
                f"'{line}' is not an exact pin — packaged dependencies must be "
                "name==version (no ranges, URLs or options)"
            )
    return errors


def package_request(
    *,
    build_id: str,
    files: dict[str, str],
    requirements_txt: str,
    response_prefix: str,
    dispatched_at: str,
) -> dict:
    """The packaging-request manifest the runner reads (MARSHAL_MODE=package).
    `files` are zip-root-relative module files ("<name>.py" → Handler
    "<name>.handler")."""
    return {
        "contract_version": CONTRACT_VERSION,
        "build_id": build_id,
        "python_version": "3.12",
        "files": files,
        "requirements_txt": requirements_txt,
        "response_prefix": response_prefix,
        "dispatched_at": dispatched_at,
    }


@dataclass(frozen=True)
class PackageResults:
    status: str  # succeeded|failed
    zip_sha256: str | None = None
    zip_bytes: int = 0
    packages: list[dict] = field(default_factory=list)  # [{name, version, license}]
    error: dict | None = None  # {code, message}


def parse_package_results(data: Any) -> PackageResults:
    """Validate + parse package-results.json. Raises ContractViolation."""
    if not isinstance(data, dict):
        raise ContractViolation("package-results.json is not a JSON object")
    if data.get("contract_version") != CONTRACT_VERSION:
        raise ContractViolation("package-results.json contract_version unsupported")
    status = data.get("status")
    if status not in RESULT_STATUSES:
        raise ContractViolation(f"status must be one of {RESULT_STATUSES}, got {status!r}")
    if status == "failed":
        error = data.get("error") or {
            "code": "packaging_failure",
            "message": "runner reported failure without detail",
        }
        if not isinstance(error, dict):
            raise ContractViolation("error must be an object when present")
        return PackageResults(status="failed", error=error)
    sha = data.get("zip_sha256")
    size = data.get("zip_bytes")
    if not isinstance(sha, str) or len(sha) != 64:
        raise ContractViolation("zip_sha256 must be a 64-char hex digest")
    if not isinstance(size, int) or size <= 0:
        raise ContractViolation("zip_bytes must be a positive integer")
    if size > MAX_PACKAGE_ZIP_BYTES:
        raise ContractViolation(
            f"package zip is {size} bytes — over the {MAX_PACKAGE_ZIP_BYTES} limit"
        )
    packages = data.get("packages")
    if not isinstance(packages, list):
        raise ContractViolation("packages must be a list")
    cleaned: list[dict] = []
    for index, entry in enumerate(packages):
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ContractViolation(f"packages[{index}] must be an object with a name")
        cleaned.append(
            {
                "name": str(entry.get("name"))[:80],
                "version": str(entry.get("version", ""))[:40],
                "license": str(entry.get("license", "unknown"))[:120],
            }
        )
    return PackageResults(
        status="succeeded", zip_sha256=sha.lower(), zip_bytes=size, packages=cleaned
    )


@dataclass(frozen=True)
class ResultFile:
    path: str
    sha256: str
    bytes: int
    binary: bool = False  # v1.1 (S10): zips etc — hash-verified, never gate-parsed


@dataclass(frozen=True)
class SynthAsset:
    """One CDK asset (v1.1, S10): parameter names come from the cloud assembly."""

    id: str
    path: str  # within generated/, e.g. synth/assets/<hash>.zip
    source_hash: str
    bucket_parameter: str
    key_parameter: str
    hash_parameter: str


@dataclass(frozen=True)
class ResultUsage:
    model_id: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Results:
    status: str  # succeeded|failed
    files: list[ResultFile] = field(default_factory=list)
    usage: list[ResultUsage] = field(default_factory=list)
    engine: dict = field(default_factory=dict)  # {name, version}
    app_name: str | None = None
    architecture_notes: str | None = None
    error: dict | None = None  # {code, message}
    synth_assets: list[SynthAsset] = field(default_factory=list)  # v1.1 (cdk-app)


def results_manifest(
    *,
    status: str,
    files: list[dict],
    usage: list[dict],
    engine: dict,
    app_name: str | None = None,
    architecture_notes: str | None = None,
    error: dict | None = None,
    synth: dict | None = None,
) -> dict:
    """The completion manifest an engine writes (R1.2) — used by the runner."""
    doc: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "files": files,
        "usage": usage,
        "engine": engine,
    }
    if app_name:
        doc["app_name"] = app_name
    if architecture_notes:
        doc["architecture_notes"] = architecture_notes
    if error:
        doc["error"] = error
    if synth:
        doc["synth"] = synth
    return doc


def parse_results(data: Any) -> Results:
    """Validate + parse an engine's results.json (R1.4). Raises ContractViolation."""
    if not isinstance(data, dict):
        raise ContractViolation("results.json is not a JSON object")
    version = data.get("contract_version")
    if version != CONTRACT_VERSION:
        raise ContractViolation(
            f"contract_version {version!r} unsupported (expected {CONTRACT_VERSION})"
        )
    status = data.get("status")
    if status not in RESULT_STATUSES:
        raise ContractViolation(f"status must be one of {RESULT_STATUSES}, got {status!r}")

    files: list[ResultFile] = []
    raw_files = data.get("files", [])
    if not isinstance(raw_files, list):
        raise ContractViolation("files must be a list")
    for index, entry in enumerate(raw_files):
        if not isinstance(entry, dict):
            raise ContractViolation(f"files[{index}] is not an object")
        path = entry.get("path")
        sha = entry.get("sha256")
        size = entry.get("bytes")
        if not isinstance(path, str) or not path.strip():
            raise ContractViolation(f"files[{index}].path missing")
        clean = path.strip()
        if clean.startswith(("/", "..")) or "/../" in clean or "\\" in clean:
            raise ContractViolation(f"files[{index}].path escapes the tree: {clean!r}")
        if not isinstance(sha, str) or len(sha) != 64:
            raise ContractViolation(f"files[{index}].sha256 must be a 64-char hex digest")
        if not isinstance(size, int) or size < 0:
            raise ContractViolation(f"files[{index}].bytes must be a non-negative integer")
        files.append(
            ResultFile(
                path=clean, sha256=sha.lower(), bytes=size,
                binary=bool(entry.get("binary", False)),
            )
        )
    if status == "succeeded" and not files:
        raise ContractViolation("succeeded results must list at least one file")

    usage: list[ResultUsage] = []
    raw_usage = data.get("usage", [])
    if not isinstance(raw_usage, list):
        raise ContractViolation("usage must be a list")
    for index, entry in enumerate(raw_usage):
        if not isinstance(entry, dict):
            raise ContractViolation(f"usage[{index}] is not an object")
        model_id = entry.get("model_id")
        input_tokens = entry.get("input_tokens", 0)
        output_tokens = entry.get("output_tokens", 0)
        if not isinstance(model_id, str) or not model_id:
            raise ContractViolation(f"usage[{index}].model_id missing")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            raise ContractViolation(f"usage[{index}] token counts must be integers")
        if input_tokens < 0 or output_tokens < 0:
            raise ContractViolation(f"usage[{index}] token counts must be non-negative")
        usage.append(
            ResultUsage(model_id=model_id, input_tokens=input_tokens, output_tokens=output_tokens)
        )

    engine = data.get("engine") or {}
    if not isinstance(engine, dict):
        raise ContractViolation("engine must be an object")

    error = data.get("error")
    if error is not None and not isinstance(error, dict):
        raise ContractViolation("error must be an object when present")
    if status == "failed" and not error:
        error = {"code": "engine_failure", "message": "engine reported failure without detail"}

    # v1.1: synth asset metadata (cdk-app profile)
    synth_assets: list[SynthAsset] = []
    synth = data.get("synth")
    if synth is not None:
        if not isinstance(synth, dict) or not isinstance(synth.get("assets", []), list):
            raise ContractViolation("synth must be an object with an assets list")
        known_paths = {f.path for f in files}
        for index, entry in enumerate(synth.get("assets", [])):
            if not isinstance(entry, dict):
                raise ContractViolation(f"synth.assets[{index}] is not an object")
            required = ("id", "path", "source_hash", "bucket_parameter",
                        "key_parameter", "hash_parameter")
            missing = [k for k in required if not isinstance(entry.get(k), str) or not entry[k]]
            if missing:
                raise ContractViolation(f"synth.assets[{index}] missing {', '.join(missing)}")
            if entry["path"] not in known_paths:
                raise ContractViolation(
                    f"synth.assets[{index}].path {entry['path']!r} not listed in files"
                )
            synth_assets.append(
                SynthAsset(
                    id=entry["id"], path=entry["path"], source_hash=entry["source_hash"],
                    bucket_parameter=entry["bucket_parameter"],
                    key_parameter=entry["key_parameter"],
                    hash_parameter=entry["hash_parameter"],
                )
            )

    app_name = data.get("app_name")
    notes = data.get("architecture_notes")
    return Results(
        status=status,
        files=files,
        usage=usage,
        engine={"name": str(engine.get("name", "unknown"))[:64],
                "version": str(engine.get("version", ""))[:32]},
        app_name=str(app_name)[:60] if isinstance(app_name, str) and app_name else None,
        architecture_notes=(
            str(notes)[:2000] if isinstance(notes, str) and notes else None
        ),
        error=error,
        synth_assets=synth_assets,
    )
