"""CodegenProvider seam (codegen-handoff spec R1).

Providers turn a spec set into a deployable artifact set. They are pure
generators: the runner owns persistence, validation, and state — providers
never touch the DB (BuildCtx carries everything they need).
"""

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import get_settings


class CodegenUnavailable(Exception):
    """Selected provider is not available at this milestone (surfaces as 503)."""


@dataclass(frozen=True)
class PlannedFile:
    path: str
    brief: str
    language: str = "python"


@dataclass(frozen=True)
class BuildPlan:
    app_name: str
    architecture_notes: str
    files: list[PlannedFile] = field(default_factory=list)


@dataclass(frozen=True)
class BuildCtx:
    """Everything a provider may use — resolved by the runner, DB-free."""

    build_id: uuid.UUID
    project_id: uuid.UUID
    project_name: str
    user_id: uuid.UUID | None
    spec_docs: dict[str, str]  # {requirements|design|tasks: markdown}
    model_id: str
    max_tokens: int
    guardrail_rules: list[str] = field(default_factory=list)  # template §4.2 architecture rules
    # B21 normalized posture, derived from the frozen requirements. Default is
    # intentionally keyed for safe compatibility when an older caller omits it.
    endpoint_auth: dict = field(
        default_factory=lambda: {"mode": "key_required", "source": "default"}
    )
    # Compatibility property input for older provider tests; production code
    # reads endpoint_auth. Kept as a field until downstream providers migrate.
    require_api_key: bool = True
    # B19 console signal — external requests must preserve this too.
    require_web_console: bool = False
    # B23 parse-once, JSON-safe exact-spelling contract.
    literal_contract: dict = field(
        default_factory=lambda: {"version": 1, "entries": []}
    )
    # C1 connector-lite: declared connector slugs from the frozen requirements
    # (empty when the connectors_enabled flag is off — the phrase stays inert
    # prose, exactly the pre-C1 honesty contract). Includes MCP tool
    # connectors — custody is one list.
    declared_connectors: list[str] = field(default_factory=list)
    # C3 v1: the subset consumed as MCP tool sources (≤1 at v1).
    mcp_tool_connectors: list[str] = field(default_factory=list)
    # Composable agents R2/R3: declared agent dependencies (slugs, frozen
    # snapshot) and whether the spec declares an orchestrated pipeline.
    agent_dependencies: list[str] = field(default_factory=list)
    orchestrated: bool = False
    # Agent substance R2.1: conversation-memory declaration.
    require_memory: bool = False
    # Agent substance R1: packaged dependency profile declaration.
    require_packaged: bool = False
    # 13.5AA size escalation: the PLATFORM chose packaged because the inline
    # build outgrew CloudFormation's ZipFile ceiling — not the spec. The
    # generated code must stay stdlib + boto3 (empty manifest): escalation
    # lifts the SIZE limit, never the dependency rule, which remains
    # spec-declared only.
    size_escalated: bool = False
    # Agent substance R2.2: declared local tools ("SHALL use tools: a, b").
    declared_tools: list[str] = field(default_factory=list)
    # Agent substance R2.3: bounded planning loop declaration.
    require_planning: bool = False
    # Agent substance R3.2: template rail caps for the loop rungs
    # (tools/planning iteration ceilings resolved from guardrails).
    loop_iteration_cap: int = 5


class CodegenProvider(Protocol):
    name: str

    # `profile` is the RESOLVED artifact profile (inline-cfn | packaged-cfn |
    # cdk-app). It is part of the contract — omitting it here let the runner's
    # in-process call site drop it while conforming to the seam (13.5Z).
    async def plan(self, ctx: BuildCtx, *, profile: str = "inline-cfn") -> BuildPlan: ...

    async def generate_file(
        self, ctx: BuildCtx, plan: BuildPlan, file: PlannedFile, generated: dict[str, str]
    ) -> str: ...


PROVIDER_NAMES = ("internal", "runner")

# Legacy identifiers still found in platform_settings.codegen.provider JSON,
# historical codegen_builds.provider rows and older CODEGEN_PROVIDER env values.
_LEGACY_PROVIDER_ALIASES = {"kiro": "runner"}


def normalize_provider_name(value: str | None) -> str:
    """Canonical provider identifier for any stored/env/request value.

    Lower-cases and strips, then maps legacy aliases (`kiro` → `runner`).
    Unknown names are returned unchanged so `get_provider` still raises
    `CodegenUnavailable` and `resolve_provider_name` still fails safe.
    """
    selected = (value or "internal").strip().lower()
    return _LEGACY_PROVIDER_ALIASES.get(selected, selected)


def get_provider(name: str) -> "CodegenProvider":
    """Construct a provider by name.

    `internal` = in-process Bedrock synthesis; `runner` = the S3/CodeBuild
    workspace runner (S9); `kiro` is accepted as a legacy alias for `runner`.
    """
    selected = normalize_provider_name(name)
    if selected == "internal":
        from app.services.codegen.internal import InternalProvider

        return InternalProvider()
    if selected == "runner":
        from app.services.codegen.workspace_runner import WorkspaceRunnerProvider

        try:
            return WorkspaceRunnerProvider()
        except RuntimeError as exc:  # workspace bucket unconfigured
            raise CodegenUnavailable(str(exc)) from exc
    raise CodegenUnavailable(f"Unknown codegen provider '{selected}'")


async def resolve_provider_name() -> str:
    """Resolve the effective provider, honoring the cloud capability pin.

    CODEGEN_PROVIDER is authoritative when ENVIRONMENT=cloud, so a stored
    platform choice cannot enable the external runner for a restricted cohort.
    Local/dev retains the runtime platform setting with the environment value
    as its bootstrap default. Unknown values always fail safe to internal.
    """
    settings = get_settings()
    deployment_default = normalize_provider_name(settings.codegen_provider)
    if settings.environment.strip().lower() == "cloud":
        return (
            deployment_default
            if deployment_default in PROVIDER_NAMES
            else "internal"
        )
    try:
        from app.core.db import SessionLocal
        from app.models import PlatformSettings

        async with SessionLocal() as db:
            row = await db.get(PlatformSettings, 1)
        stored = (row.codegen or {}).get("provider") if row else None
        # Stored rows may still carry the legacy "kiro" identifier.
        configured = normalize_provider_name(stored) if stored else deployment_default
    except Exception:  # noqa: BLE001 — settings trouble must not brick builds
        configured = deployment_default
    return configured if configured in PROVIDER_NAMES else "internal"
