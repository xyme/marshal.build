"""Manifest-first reset of approved marshal demo-owned hot data.

The default is a read-only dry run. Applying a reset requires ``--apply``,
the confirmation token emitted by an unchanged dry run, and the matching
``--maintenance-token``. Apply/resume independently verify that the configured
ECS backend has desired/running/pending counts of zero and that its Application
Auto Scaling target is pinned to min/max zero; the token alone is never enough.
The script keeps the four primary Postgres/Cognito identities, never calls
Cognito, never hard-deletes marketplace rows, and refuses to use SQL to hide
potentially live infrastructure.

Safe lifecycle preparation is intentionally outside this command: active or
in-flight builds/deployments and every unterminated lease must be cancelled or
torn down through the product API first. Failed deployment histories carrying
stack or lease references require a same-project torn-down sibling that shares
the stack or lease and a terminated lease.

Relational deletion commits before idempotent external deletion. A private,
content-addressed cleanup descriptor and receipts allow exact post-commit retry
of DynamoDB, S3, and webhook-secret cleanup:
``--resume-external <DESCRIPTOR_SHA256> --maintenance-token <TOKEN>``.

Usage:
    cd backend && uv run python ../scripts/reset_demo.py
    cd backend && uv run python ../scripts/reset_demo.py --dry-run
    cd backend && uv run python ../scripts/reset_demo.py \
        --apply --confirm RESET-DEMO-<DIGEST> \
        --maintenance-token BACKEND-SCALED-ZERO-<DIGEST>
    cd backend && uv run python ../scripts/reset_demo.py \
        --resume-external <DESCRIPTOR_SHA256> \
        --maintenance-token BACKEND-SCALED-ZERO-<DIGEST>
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, "/app")

import boto3  # noqa: E402
from boto3.dynamodb.conditions import Key  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from seed_demo import PROJECTS, ROSTER, TEAMS, did  # noqa: E402
from sqlalchemy import delete, or_, select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402
from sqlalchemy.inspection import inspect as sa_inspect  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AiRiskAssessment,
    Alert,
    AuditLog,
    ChatSession,
    CodegenArtifact,
    CodegenBuild,
    Deployment,
    Lease,
    MarketplaceSample,
    ModelInvocation,
    Notification,
    PlatformSettings,
    Project,
    ProjectMember,
    ProjectPresence,
    ReviewerGroupMember,
    SandboxSpend,
    ServiceAccountToken,
    Spec,
    SpecComment,
    SpecDraft,
    SpecGeneration,
    Team,
    TeamMember,
    Template,
    UsageEvent,
    User,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.models.entities import PLATFORM_TENANT_ID  # noqa: E402
from app.services.codegen.storage import artifact_key  # noqa: E402
from app.services.deployment import (  # noqa: E402
    DEPLOYMENT_ADMISSION_LOCK_KEY,
    DEPLOYMENT_ADMISSION_LOCK_NAMESPACE,
)
from fixtures.marketplace.portfolio import PORTFOLIO_KEYS  # noqa: E402

MANIFEST_VERSION = 2
MANIFEST_KIND = "marshal-demo-reset"
CLEANUP_DESCRIPTOR_KIND = "marshal-demo-external-cleanup"
CLEANUP_POINTER_KIND = "marshal-demo-external-cleanup-pointer"
CLEANUP_RECEIPT_KIND = "marshal-demo-external-cleanup-receipt"
RESET_PREFIX = "artifacts/demo-reset/v2"
RESET_EVIDENCE_RETENTION_DAYS = 180
WEBHOOK_SECRET_PREFIX = "marshal/webhooks/"
WEBHOOK_SECRET_RECOVERY_DAYS = 7
RESET_LOCK_KEY = 5_726_883_033_699_327_011
PROJECT_MARKER_PREFIX = "[marshal-demo-portfolio:v1:"
PROJECT_MARKER_RE = re.compile(r"\[marshal-demo-portfolio:v1:([a-z0-9-]+)\]")
DRILL_TEAM_RE = re.compile(r"Drill Team [AB] \d{10,}")
BUILD_WORKSPACE_KEY_RE_TEMPLATE = r"^builds/{build_id}(?:-r\d+)?/"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PRIMARY_IDENTITIES = {
    "business@marshal.demo": "business",
    "power@marshal.demo": "power",
    "demo@marshal.demo": "power",
    "admin@marshal.demo": "admin",
}
ACTIVE_BUILD_STATES = {"queued", "dispatched", "generating", "validating"}
IN_FLIGHT_DEPLOYMENT_STATES = {
    "pending",
    "pre_flight",
    "leasing",
    "deploying",
    "updating",
    "tearing_down",
}
SAFE_DEPLOYMENT_STATES = {"torn_down", "superseded"}
DEFAULT_ECS_CLUSTER = os.environ.get("DEMO_RESET_ECS_CLUSTER", "marshal")
DEFAULT_ECS_SERVICE = os.environ.get("DEMO_RESET_ECS_SERVICE", "marshal-backend")


class ResetRefused(RuntimeError):
    """A fail-closed reset precondition was not met."""


MODEL_BY_NAME = {
    "ai_risk_assessments": AiRiskAssessment,
    "alerts": Alert,
    "audit_logs": AuditLog,
    "chat_sessions": ChatSession,
    "codegen_artifacts": CodegenArtifact,
    "codegen_builds": CodegenBuild,
    "deployments": Deployment,
    "leases": Lease,
    "model_invocations": ModelInvocation,
    "notifications": Notification,
    "project_members": ProjectMember,
    "reviewer_group_members": ReviewerGroupMember,
    "sandbox_spend": SandboxSpend,
    "service_account_tokens": ServiceAccountToken,
    "spec_comments": SpecComment,
    "spec_drafts": SpecDraft,
    "spec_generations": SpecGeneration,
    "specs": Spec,
    "team_members": TeamMember,
    "usage_events": UsageEvent,
    "webhook_deliveries": WebhookDelivery,
    "webhook_endpoints": WebhookEndpoint,
}
DETACH_MODEL_BY_TABLE = {
    model.__tablename__: model
    for model in (
        PlatformSettings,
        Template,
        MarketplaceSample,
        AiRiskAssessment,
        CodegenBuild,
        Alert,
        ProjectMember,
        TeamMember,
        ReviewerGroupMember,
        ServiceAccountToken,
        Spec,
        SpecComment,
    )
}


@dataclass
class ResetScope:
    primary: dict[str, dict[str, Any]]
    roster_user_ids: set[uuid.UUID]
    service_user_ids: set[uuid.UUID]
    user_ids: set[uuid.UUID]
    team_ids: set[uuid.UUID]
    project_ids: set[uuid.UUID]
    session_ids: set[uuid.UUID]
    generation_ids: set[uuid.UUID]
    build_ids: set[uuid.UUID]
    deployment_ids: set[uuid.UUID]
    lease_ids: set[uuid.UUID]
    marketplace_link_ids: set[uuid.UUID]
    marketplace_links: list[dict[str, Any]] = field(default_factory=list)
    retained_detaches: list[dict[str, Any]] = field(default_factory=list)
    selected_ids: dict[str, set[uuid.UUID]] = field(default_factory=dict)
    presence_keys: set[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=set)
    evidence_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    evidence_blobs: dict[str, bytes] = field(default_factory=dict)
    failed_history: list[dict[str, Any]] = field(default_factory=list)
    transcript_keys: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    artifact_s3_keys: set[str] = field(default_factory=set)
    s3_cleanup_keys: list[str] = field(default_factory=list)
    webhook_secret_names: list[str] = field(default_factory=list)
    artifact_bucket: str = ""
    evidence_bucket: str = ""
    dynamo_table: str = ""
    before_counts: dict[str, int] = field(default_factory=dict)
    scope_sha256: str = ""

    @property
    def confirmation_token(self) -> str:
        if not self.scope_sha256:
            raise RuntimeError("scope digest has not been computed")
        return f"RESET-DEMO-{self.scope_sha256[:16].upper()}"

    @property
    def maintenance_token(self) -> str:
        if not self.scope_sha256:
            raise RuntimeError("scope digest has not been computed")
        return f"BACKEND-SCALED-ZERO-{self.scope_sha256[:16].upper()}"

    @property
    def has_mutations(self) -> bool:
        relational = any(self.before_counts.get(name, 0) for name in self.selected_ids)
        relational = relational or bool(
            self.project_ids
            or self.team_ids
            or self.roster_user_ids
            or self.service_user_ids
        )
        return bool(
            relational
            or self.marketplace_link_ids
            or self.retained_detaches
            or self.presence_keys
            or self.s3_cleanup_keys
            or self.webhook_secret_names
            or any(self.transcript_keys.values())
        )


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, uuid.UUID, Decimal, Path)):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, set):
        return sorted(str(item) for item in value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode()


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        column.key: getattr(row, column.key)
        for column in sa_inspect(row.__class__).columns
    }


def _jsonl(rows: Iterable[dict[str, Any]]) -> bytes:
    ordered = sorted(
        rows, key=lambda row: (str(row.get("kind", "")), str(row.get("id", "")))
    )
    if not ordered:
        return b""
    return b"\n".join(_canonical_bytes(row) for row in ordered) + b"\n"


def _ids(rows: Iterable[Any]) -> set[uuid.UUID]:
    return {row.id for row in rows}


def _expected_primary_env(email: str) -> str:
    prefix = email.split("@", 1)[0].upper()
    return f"DEMO_{prefix}_USER_ID"


async def _resolve_identities(
    db: AsyncSession,
) -> tuple[dict[str, dict[str, Any]], list[User]]:
    emails = set(PRIMARY_IDENTITIES) | {entry[1] for entry in ROSTER}
    rows = list(
        (
            await db.execute(
                select(User).where(
                    User.tenant_id == PLATFORM_TENANT_ID,
                    User.email.in_(emails),
                )
            )
        ).scalars()
    )
    by_email: dict[str, list[User]] = {}
    for row in rows:
        by_email.setdefault(row.email, []).append(row)

    primary: dict[str, dict[str, Any]] = {}
    for email, expected_role in PRIMARY_IDENTITIES.items():
        matches = by_email.get(email, [])
        if len(matches) != 1:
            raise ResetRefused(
                f"expected exactly one primary identity {email}; found {[str(row.id) for row in matches]}"
            )
        row = matches[0]
        if (
            row.kind != "human"
            or row.role != expected_role
            or row.cognito_sub.startswith("demo-seed-")
        ):
            raise ResetRefused(
                f"primary identity {email} has unexpected kind/role/Cognito mapping"
            )
        expected_id = os.environ.get(_expected_primary_env(email))
        if expected_id and expected_id != str(row.id):
            raise ResetRefused(
                f"{_expected_primary_env(email)}={expected_id} does not match database id {row.id}"
            )
        primary[email] = {
            "id": str(row.id),
            "cognito_sub": row.cognito_sub,
            "role": row.role,
            "status": row.status,
            "preserved": True,
        }

    roster_rows: list[User] = []
    for key, email, _name, role, _persona, _status, _joined in ROSTER:
        expected_id = did("user", key)
        matches = by_email.get(email, [])
        by_id = await db.get(User, expected_id)
        if not matches and by_id is None:
            continue
        if len(matches) != 1 or by_id is None or matches[0].id != expected_id:
            raise ResetRefused(
                f"roster identity {email} is ambiguous or not at deterministic id {expected_id}"
            )
        row = matches[0]
        if (
            row.cognito_sub != f"demo-seed-{key}"
            or row.role != role
            or row.kind != "human"
        ):
            raise ResetRefused(
                f"roster identity {email} no longer matches the demo-seed contract"
            )
        roster_rows.append(row)
    return primary, roster_rows


async def _rows(db: AsyncSession, model: Any, *conditions: Any) -> list[Any]:
    query = select(model)
    if conditions:
        query = query.where(*conditions)
    return list((await db.execute(query)).scalars())


def _assert_lifecycle_safe(
    builds: list[CodegenBuild],
    deployments: list[Deployment],
    leases: list[Lease],
) -> list[dict[str, Any]]:
    active_builds = [
        {"id": str(row.id), "project_id": str(row.project_id), "status": row.status}
        for row in builds
        if row.status in ACTIVE_BUILD_STATES
    ]
    unsafe_deployments = [
        {
            "id": str(row.id),
            "project_id": str(row.project_id),
            "status": row.status,
            "stack_name": row.stack_name,
            "lease_id": str(row.lease_id) if row.lease_id else None,
        }
        for row in deployments
        if row.status == "active" or row.status in IN_FLIGHT_DEPLOYMENT_STATES
    ]
    unterminated = [
        {
            "id": str(row.id),
            "project_id": str(row.project_id),
            "status": row.status,
            "external_lease_id": row.external_lease_id,
        }
        for row in leases
        if row.status != "terminated"
    ]
    failed_history: list[dict[str, Any]] = []
    unsafe_failed: list[dict[str, Any]] = []
    lease_by_id = {row.id: row for row in leases}
    torn_down = [row for row in deployments if row.status == "torn_down"]
    for row in deployments:
        if row.status != "failed":
            continue
        lease = lease_by_id.get(row.lease_id) if row.lease_id else None
        references_resource = bool(row.stack_name or row.stack_id or row.lease_id)
        proving_siblings = [
            sibling
            for sibling in torn_down
            if sibling.project_id == row.project_id
            and sibling.id != row.id
            and sibling.torn_down_at is not None
            and (
                bool(row.stack_name and sibling.stack_name == row.stack_name)
                or bool(row.stack_id and sibling.stack_id == row.stack_id)
                or bool(row.lease_id and sibling.lease_id == row.lease_id)
            )
        ]
        lease_terminated = row.lease_id is None or (
            lease is not None
            and lease.status == "terminated"
            and lease.terminated_at is not None
        )
        entry = {
            "id": str(row.id),
            "project_id": str(row.project_id),
            "stack_name": row.stack_name,
            "stack_id": row.stack_id,
            "lease_id": str(row.lease_id) if row.lease_id else None,
            "lease_status": lease.status if lease else None,
            "lease_terminated_at": lease.terminated_at if lease else None,
        }
        if references_resource and (not proving_siblings or not lease_terminated):
            entry["reconciliation"] = "required_through_product_teardown"
            unsafe_failed.append(entry)
        elif references_resource:
            entry["reconciliation"] = "proved_by_torn_down_sibling"
            entry["proving_sibling_ids"] = sorted(
                str(sibling.id) for sibling in proving_siblings
            )
        else:
            entry["reconciliation"] = "safe_history_without_resource_references"
        failed_history.append(entry)

    if active_builds or unsafe_deployments or unterminated or unsafe_failed:
        raise ResetRefused(
            "live-resource safety check failed; cancel builds and use the product teardown "
            "path before resetting: "
            + json.dumps(
                {
                    "active_builds": active_builds,
                    "active_or_inflight_deployments": unsafe_deployments,
                    "unterminated_leases": unterminated,
                    "failed_histories_requiring_teardown": unsafe_failed,
                },
                sort_keys=True,
                default=_json_default,
            )
        )
    unexpected = [
        row
        for row in deployments
        if row.status not in SAFE_DEPLOYMENT_STATES | {"failed"}
    ]
    if unexpected:
        raise ResetRefused(
            f"unknown deployment states refuse reset: {[(str(row.id), row.status) for row in unexpected]}"
        )
    return failed_history


async def _discover_database_scope(db: AsyncSession) -> ResetScope:
    primary, roster_rows = await _resolve_identities(db)
    primary_ids = {uuid.UUID(value["id"]) for value in primary.values()}
    roster_ids = _ids(roster_rows)
    identity_user_ids = primary_ids | roster_ids

    # A service token authenticates as its service User; the selected human is
    # normally only recorded in created_by. Discover by either side, then bind
    # the complete token set for every discovered service identity so deleting
    # the User cannot cascade an unrecorded credential.
    matching_tokens = await _rows(
        db,
        ServiceAccountToken,
        ServiceAccountToken.tenant_id == PLATFORM_TENANT_ID,
        or_(
            ServiceAccountToken.user_id.in_(identity_user_ids),
            ServiceAccountToken.created_by.in_(identity_user_ids),
        ),
    )
    service_owner_ids = {row.user_id for row in matching_tokens}
    service_users = (
        await _rows(
            db,
            User,
            User.tenant_id == PLATFORM_TENANT_ID,
            User.id.in_(service_owner_ids),
        )
        if service_owner_ids
        else []
    )
    service_users_by_id = {row.id: row for row in service_users}
    invalid_service_owners = [
        {
            "token_id": str(token.id),
            "user_id": str(token.user_id),
            "kind": (
                service_users_by_id[token.user_id].kind
                if token.user_id in service_users_by_id
                else None
            ),
        }
        for token in matching_tokens
        if token.user_id not in service_users_by_id
        or service_users_by_id[token.user_id].kind != "service"
    ]
    if invalid_service_owners:
        raise ResetRefused(
            "selected-identity service tokens do not resolve to platform-tenant service "
            "User rows: " + json.dumps(invalid_service_owners, sort_keys=True)
        )
    service_user_ids = _ids(service_users)
    tokens = (
        await _rows(
            db,
            ServiceAccountToken,
            ServiceAccountToken.tenant_id == PLATFORM_TENANT_ID,
            ServiceAccountToken.user_id.in_(service_user_ids),
        )
        if service_user_ids
        else []
    )
    user_ids = identity_user_ids | service_user_ids

    deterministic_team_contract = {
        did("team", key): name for key, name, _description in TEAMS
    }
    admin_id = uuid.UUID(primary["admin@marshal.demo"]["id"])
    tenant_teams = await _rows(
        db,
        Team,
        Team.tenant_id == PLATFORM_TENANT_ID,
    )
    teams: list[Team] = []
    unexpected_owned_teams: list[dict[str, Any]] = []
    for team in tenant_teams:
        expected_name = deterministic_team_contract.get(team.id)
        is_drill = DRILL_TEAM_RE.fullmatch(team.name) is not None
        if expected_name is not None:
            if team.name != expected_name or team.created_by != admin_id:
                raise ResetRefused(
                    "deterministic demo team no longer matches its exact name/admin provenance: "
                    f"{team.id} name={team.name!r} created_by={team.created_by}"
                )
            teams.append(team)
            continue
        if is_drill:
            if team.created_by != admin_id:
                raise ResetRefused(
                    "explicit Drill Team A/B <epoch> team was not created by the known "
                    f"demo admin: {team.id} created_by={team.created_by}"
                )
            teams.append(team)
            continue
        if team.created_by in user_ids:
            unexpected_owned_teams.append(
                {
                    "id": str(team.id),
                    "name": team.name,
                    "created_by": str(team.created_by),
                }
            )
    if unexpected_owned_teams:
        raise ResetRefused(
            "selected demo/roster identities created non-seed, non-drill teams; refusing "
            "rather than absorbing them: "
            + json.dumps(unexpected_owned_teams, sort_keys=True)
        )
    team_ids = _ids(teams)

    deterministic_project_ids = {did("project", item[0]) for item in PROJECTS}
    tenant_projects = await _rows(
        db,
        Project,
        Project.tenant_id == PLATFORM_TENANT_ID,
    )
    service_owned_projects = [
        {
            "id": str(project.id),
            "name": project.name,
            "owner_id": str(project.user_id),
        }
        for project in tenant_projects
        if project.user_id in service_user_ids
    ]
    if service_owned_projects:
        raise ResetRefused(
            "selected service identities own projects despite the act-only contract; "
            "transfer ownership to a reviewed human before reset: "
            + json.dumps(service_owned_projects, sort_keys=True)
        )
    projects: list[Project] = []
    malformed_markers: list[dict[str, Any]] = []
    for project in tenant_projects:
        description = project.description or ""
        marker_keys = PROJECT_MARKER_RE.findall(description)
        if PROJECT_MARKER_PREFIX in description and (
            len(marker_keys) != 1 or marker_keys[0] not in PORTFOLIO_KEYS
        ):
            malformed_markers.append(
                {
                    "id": str(project.id),
                    "name": project.name,
                    "marker_keys": marker_keys,
                }
            )
            continue
        has_portfolio_marker = len(marker_keys) == 1
        if (
            project.id in deterministic_project_ids
            or project.user_id in identity_user_ids
            or has_portfolio_marker
        ):
            if project.user_id not in identity_user_ids:
                raise ResetRefused(
                    "selected deterministic/portfolio project has an owner outside the "
                    "selected demo/roster identities: "
                    f"project={project.id} owner={project.user_id}"
                )
            projects.append(project)
    if malformed_markers:
        raise ResetRefused(
            "malformed or noncanonical portfolio markers require manual review: "
            + json.dumps(malformed_markers, sort_keys=True)
        )
    outsider_team_projects = [
        {
            "id": str(project.id),
            "team_id": str(project.team_id),
            "owner_id": str(project.user_id),
            "name": project.name,
        }
        for project in tenant_projects
        if project.team_id in team_ids and project.user_id not in identity_user_ids
    ]
    if outsider_team_projects:
        raise ResetRefused(
            "selected teams contain projects owned outside the selected demo/roster "
            "identities; refusing rather than absorbing them: "
            + json.dumps(outsider_team_projects, sort_keys=True)
        )
    project_ids = _ids(projects)

    marketplace_links = await _rows(
        db,
        MarketplaceSample,
        MarketplaceSample.tenant_id == PLATFORM_TENANT_ID,
        MarketplaceSample.source_project_id.in_(project_ids),
    )
    published_links = [row for row in marketplace_links if row.status == "published"]
    if published_links:
        raise ResetRefused(
            "published catalog rows still reference reset projects; curate them before reset: "
            f"{[(str(row.id), row.title) for row in published_links]}"
        )

    sessions = await _rows(
        db,
        ChatSession,
        ChatSession.tenant_id == PLATFORM_TENANT_ID,
        or_(ChatSession.user_id.in_(user_ids), ChatSession.project_id.in_(project_ids)),
    )
    session_ids = _ids(sessions)

    generations = await _rows(
        db,
        SpecGeneration,
        SpecGeneration.tenant_id == PLATFORM_TENANT_ID,
        or_(
            SpecGeneration.project_id.in_(project_ids),
            SpecGeneration.session_id.in_(session_ids),
        ),
    )
    generation_ids = _ids(generations)

    deployments = await _rows(
        db,
        Deployment,
        Deployment.tenant_id == PLATFORM_TENANT_ID,
        or_(Deployment.project_id.in_(project_ids), Deployment.user_id.in_(user_ids)),
    )
    deployment_ids = _ids(deployments)
    related_build_ids = {row.build_id for row in deployments if row.build_id}
    related_lease_ids = {row.lease_id for row in deployments if row.lease_id}

    builds = await _rows(
        db,
        CodegenBuild,
        CodegenBuild.tenant_id == PLATFORM_TENANT_ID,
        or_(
            CodegenBuild.project_id.in_(project_ids),
            CodegenBuild.id.in_(related_build_ids),
        ),
    )
    build_ids = _ids(builds)
    leases = await _rows(
        db,
        Lease,
        Lease.tenant_id == PLATFORM_TENANT_ID,
        or_(
            Lease.project_id.in_(project_ids),
            Lease.user_id.in_(user_ids),
            Lease.id.in_(related_lease_ids),
        ),
    )
    lease_ids = _ids(leases)
    failed_history = _assert_lifecycle_safe(builds, deployments, leases)

    artifacts = await _rows(
        db, CodegenArtifact, CodegenArtifact.build_id.in_(build_ids)
    )
    artifact_s3_keys: set[str] = set()
    for artifact in artifacts:
        if artifact.s3_key is None:
            continue
        expected_key = artifact_key(artifact.build_id, artifact.path)
        expected_prefix = f"artifacts/{artifact.build_id}/"
        if (
            artifact.build_id not in build_ids
            or not artifact.s3_key.startswith(expected_prefix)
            or artifact.s3_key != expected_key
        ):
            raise ResetRefused(
                "artifact S3 key is outside its exact selected-build key contract: "
                f"artifact={artifact.id} observed={artifact.s3_key!r} expected={expected_key!r}"
            )
        artifact_s3_keys.add(artifact.s3_key)
    specs = await _rows(
        db,
        Spec,
        Spec.tenant_id == PLATFORM_TENANT_ID,
        or_(Spec.project_id.in_(project_ids), Spec.session_id.in_(session_ids)),
    )
    drafts = await _rows(
        db,
        SpecDraft,
        SpecDraft.tenant_id == PLATFORM_TENANT_ID,
        or_(SpecDraft.project_id.in_(project_ids), SpecDraft.user_id.in_(user_ids)),
    )
    comments = await _rows(
        db,
        SpecComment,
        SpecComment.tenant_id == PLATFORM_TENANT_ID,
        SpecComment.project_id.in_(project_ids),
    )
    roster_comments_outside_scope = await _rows(
        db,
        SpecComment,
        SpecComment.tenant_id == PLATFORM_TENANT_ID,
        SpecComment.author_id.in_(roster_ids),
        SpecComment.project_id.notin_(project_ids),
    )
    if roster_comments_outside_scope:
        raise ResetRefused(
            "demo-seed roster comments exist on retained projects; review before deleting "
            f"their authors: {[str(row.id) for row in roster_comments_outside_scope]}"
        )
    external_replies = await _rows(
        db,
        SpecComment,
        SpecComment.parent_id.in_(_ids(comments)),
        SpecComment.project_id.notin_(project_ids),
    )
    if external_replies:
        raise ResetRefused(
            "retained-project replies reference demo-project comments; refusing cascade: "
            f"{[str(row.id) for row in external_replies]}"
        )
    presence = await _rows(
        db,
        ProjectPresence,
        or_(
            ProjectPresence.project_id.in_(project_ids),
            ProjectPresence.user_id.in_(user_ids),
        ),
    )
    project_members = await _rows(
        db,
        ProjectMember,
        ProjectMember.tenant_id == PLATFORM_TENANT_ID,
        or_(
            ProjectMember.project_id.in_(project_ids),
            ProjectMember.user_id.in_(user_ids),
        ),
    )
    team_members = await _rows(
        db,
        TeamMember,
        TeamMember.tenant_id == PLATFORM_TENANT_ID,
        or_(TeamMember.team_id.in_(team_ids), TeamMember.user_id.in_(user_ids)),
    )
    reviewers = await _rows(
        db,
        ReviewerGroupMember,
        ReviewerGroupMember.tenant_id == PLATFORM_TENANT_ID,
        ReviewerGroupMember.user_id.in_(user_ids),
    )
    notifications = await _rows(
        db,
        Notification,
        Notification.tenant_id == PLATFORM_TENANT_ID,
        Notification.user_id.in_(user_ids),
    )
    usage = await _rows(
        db,
        UsageEvent,
        UsageEvent.tenant_id == PLATFORM_TENANT_ID,
        or_(
            UsageEvent.user_id.in_(user_ids),
            UsageEvent.project_id.in_(project_ids),
            UsageEvent.team_id.in_(team_ids),
        ),
    )
    risks = await _rows(
        db,
        AiRiskAssessment,
        AiRiskAssessment.tenant_id == PLATFORM_TENANT_ID,
        AiRiskAssessment.project_id.in_(project_ids),
    )
    audit_rows = await _rows(
        db,
        AuditLog,
        AuditLog.tenant_id == PLATFORM_TENANT_ID,
        or_(AuditLog.actor_id.in_(user_ids), AuditLog.project_id.in_(project_ids)),
    )
    model_rows = await _rows(
        db,
        ModelInvocation,
        ModelInvocation.tenant_id == PLATFORM_TENANT_ID,
        or_(
            ModelInvocation.user_id.in_(user_ids),
            ModelInvocation.project_id.in_(project_ids),
            ModelInvocation.session_id.in_(session_ids),
            ModelInvocation.generation_id.in_(generation_ids),
        ),
    )
    alerts = await _rows(
        db,
        Alert,
        Alert.tenant_id == PLATFORM_TENANT_ID,
        or_(
            Alert.scope_user_id.in_(user_ids),
            Alert.scope_project_id.in_(project_ids),
        ),
    )
    spend = await _rows(
        db,
        SandboxSpend,
        or_(
            SandboxSpend.lease_id.in_(lease_ids),
            SandboxSpend.project_id.in_(project_ids),
        ),
    )
    endpoints = await _rows(
        db,
        WebhookEndpoint,
        WebhookEndpoint.tenant_id == PLATFORM_TENANT_ID,
        WebhookEndpoint.created_by.in_(user_ids),
    )
    endpoint_ids = _ids(endpoints)
    webhook_secret_names = sorted(
        f"{WEBHOOK_SECRET_PREFIX}{endpoint_id}" for endpoint_id in endpoint_ids
    )
    deliveries = await _rows(
        db,
        WebhookDelivery,
        WebhookDelivery.tenant_id == PLATFORM_TENANT_ID,
        WebhookDelivery.endpoint_id.in_(endpoint_ids),
    )

    selected_rows = {
        "ai_risk_assessments": risks,
        "alerts": alerts,
        "audit_logs": audit_rows,
        "chat_sessions": sessions,
        "codegen_artifacts": artifacts,
        "codegen_builds": builds,
        "deployments": deployments,
        "leases": leases,
        "model_invocations": model_rows,
        "notifications": notifications,
        "project_members": project_members,
        "reviewer_group_members": reviewers,
        "sandbox_spend": spend,
        "service_account_tokens": tokens,
        "spec_comments": comments,
        "spec_drafts": drafts,
        "spec_generations": generations,
        "specs": specs,
        "team_members": team_members,
        "usage_events": usage,
        "webhook_deliveries": deliveries,
        "webhook_endpoints": endpoints,
    }
    selected_ids = {name: _ids(items) for name, items in selected_rows.items()}

    # Service users are selected only through credentials tied to the approved
    # human identities. Their act-only rows above are deleted with the scope;
    # ownership or attribution on any retained row is unexpected and refuses
    # deletion instead of being silently detached.
    service_reference_specs = (
        (Team, "created_by", team_ids),
        (PlatformSettings, "updated_by", set()),
        (Template, "created_by", set()),
        (MarketplaceSample, "author_id", set()),
        (MarketplaceSample, "reviewed_by", set()),
        (AiRiskAssessment, "decided_by", selected_ids["ai_risk_assessments"]),
        (CodegenBuild, "created_by", selected_ids["codegen_builds"]),
        (Alert, "acknowledged_by", selected_ids["alerts"]),
        (ProjectMember, "added_by", selected_ids["project_members"]),
        (TeamMember, "added_by", selected_ids["team_members"]),
        (
            ReviewerGroupMember,
            "added_by",
            selected_ids["reviewer_group_members"],
        ),
        (
            ServiceAccountToken,
            "created_by",
            selected_ids["service_account_tokens"],
        ),
        (Spec, "created_by", selected_ids["specs"]),
        (SpecComment, "author_id", selected_ids["spec_comments"]),
        (SpecComment, "resolved_by", selected_ids["spec_comments"]),
        (WebhookEndpoint, "created_by", selected_ids["webhook_endpoints"]),
    )
    unexpected_service_references: list[dict[str, str]] = []
    if service_user_ids:
        for model, field_name, deleting_ids in service_reference_specs:
            column = getattr(model, field_name)
            referenced = await _rows(db, model, column.in_(service_user_ids))
            for row in referenced:
                if row.id in deleting_ids:
                    continue
                unexpected_service_references.append(
                    {
                        "table": model.__tablename__,
                        "id": str(row.id),
                        "field": field_name,
                        "service_user_id": str(getattr(row, field_name)),
                    }
                )
    if unexpected_service_references:
        raise ResetRefused(
            "selected service identities have unexpected references on retained rows; "
            "review them before reset: "
            + json.dumps(unexpected_service_references, sort_keys=True)
        )

    # Every nullable reference changed on apply is discovered now, included in
    # the confirmation digest, and later updated by exact row/field/value only.
    detach_specs = (
        (PlatformSettings, "updated_by", set()),
        (Template, "created_by", set()),
        (MarketplaceSample, "author_id", set()),
        (MarketplaceSample, "reviewed_by", set()),
        (AiRiskAssessment, "decided_by", selected_ids["ai_risk_assessments"]),
        (CodegenBuild, "created_by", selected_ids["codegen_builds"]),
        (Alert, "acknowledged_by", selected_ids["alerts"]),
        (ProjectMember, "added_by", selected_ids["project_members"]),
        (TeamMember, "added_by", selected_ids["team_members"]),
        (
            ReviewerGroupMember,
            "added_by",
            selected_ids["reviewer_group_members"],
        ),
        (
            ServiceAccountToken,
            "created_by",
            selected_ids["service_account_tokens"],
        ),
        (Spec, "created_by", selected_ids["specs"]),
        (SpecComment, "resolved_by", selected_ids["spec_comments"]),
    )
    retained_detaches: list[dict[str, Any]] = []
    for model, field_name, deleting_ids in detach_specs:
        column = getattr(model, field_name)
        referenced = await _rows(db, model, column.in_(roster_ids))
        for row in referenced:
            if row.id in deleting_ids:
                continue
            retained_detaches.append(
                {
                    "table": model.__tablename__,
                    "id": row.id,
                    "field": field_name,
                    "before": getattr(row, field_name),
                }
            )
    incoming_risk_links = await _rows(
        db,
        AiRiskAssessment,
        AiRiskAssessment.resubmission_of.in_(selected_ids["ai_risk_assessments"]),
    )
    for row in incoming_risk_links:
        if row.id not in selected_ids["ai_risk_assessments"]:
            retained_detaches.append(
                {
                    "table": AiRiskAssessment.__tablename__,
                    "id": row.id,
                    "field": "resubmission_of",
                    "before": row.resubmission_of,
                }
            )
    retained_detaches.sort(
        key=lambda item: (item["table"], str(item["id"]), item["field"])
    )
    marketplace_link_records = [
        {
            "id": row.id,
            "source_project_id": row.source_project_id,
            "status": row.status,
        }
        for row in sorted(marketplace_links, key=lambda item: str(item.id))
    ]

    deployment_evidence: list[dict[str, Any]] = []
    for kind, items in (
        ("deployment", deployments),
        ("lease", leases),
        ("build", builds),
    ):
        deployment_evidence.extend({"kind": kind, **_row_dict(row)} for row in items)
    evidence_rows = {
        "audit": [_row_dict(row) for row in audit_rows],
        "model-invocations": [_row_dict(row) for row in model_rows],
        "deployment": deployment_evidence,
        "service-accounts": [_row_dict(row) for row in service_users],
        "service-account-tokens": [_row_dict(row) for row in tokens],
        "risk-assessments": [_row_dict(row) for row in risks],
        "alerts": [_row_dict(row) for row in alerts],
        "sandbox-spend": [_row_dict(row) for row in spend],
        "usage-events": [_row_dict(row) for row in usage],
        "notifications": [_row_dict(row) for row in notifications],
    }
    evidence_blobs = {
        f"{name}.jsonl": _jsonl(items) for name, items in evidence_rows.items()
    }

    settings = get_settings()
    artifact_bucket = settings.codegen_workspace_bucket
    evidence_bucket = os.environ.get("DEMO_RESET_EVIDENCE_BUCKET", artifact_bucket)
    if not artifact_bucket:
        raise ResetRefused(
            "CODEGEN_WORKSPACE_BUCKET is required for scoped artifact cleanup"
        )
    if not evidence_bucket:
        raise ResetRefused(
            "DEMO_RESET_EVIDENCE_BUCKET or CODEGEN_WORKSPACE_BUCKET is required for private evidence"
        )
    dynamo_table = settings.dynamo_table_chat
    if not dynamo_table:
        raise ResetRefused("DYNAMO_TABLE_CHAT is required for transcript cleanup")

    before_counts = {name: len(ids) for name, ids in selected_ids.items()}
    before_counts.update(
        {
            "marketplace_links_to_detach": len(marketplace_links),
            "retained_references_to_detach": len(retained_detaches),
            "project_presence": len(presence),
            "projects": len(project_ids),
            "teams": len(team_ids),
            "service_users": len(service_user_ids),
            "roster_users": len(roster_ids),
            "webhook_secrets": len(webhook_secret_names),
            "primary_users_preserved": len(primary),
        }
    )

    return ResetScope(
        primary=primary,
        roster_user_ids=roster_ids,
        service_user_ids=service_user_ids,
        user_ids=user_ids,
        team_ids=team_ids,
        project_ids=project_ids,
        session_ids=session_ids,
        generation_ids=generation_ids,
        build_ids=build_ids,
        deployment_ids=deployment_ids,
        lease_ids=lease_ids,
        marketplace_link_ids=_ids(marketplace_links),
        marketplace_links=marketplace_link_records,
        retained_detaches=retained_detaches,
        selected_ids=selected_ids,
        presence_keys={(row.project_id, row.user_id) for row in presence},
        evidence_rows=evidence_rows,
        evidence_blobs=evidence_blobs,
        failed_history=failed_history,
        artifact_s3_keys=artifact_s3_keys,
        webhook_secret_names=webhook_secret_names,
        artifact_bucket=artifact_bucket,
        evidence_bucket=evidence_bucket,
        dynamo_table=dynamo_table,
        before_counts=before_counts,
    )


def _verify_backend_maintenance_state(
    cluster: str = DEFAULT_ECS_CLUSTER,
    service: str = DEFAULT_ECS_SERVICE,
) -> dict[str, Any]:
    """Independently prove ECS is quiescent and autoscaling is pinned at zero."""
    region = get_settings().aws_region
    ecs = boto3.client("ecs", region_name=region)
    scaling = boto3.client("application-autoscaling", region_name=region)
    try:
        described = ecs.describe_services(cluster=cluster, services=[service])
        failures = described.get("failures", [])
        services = described.get("services", [])
        if failures or len(services) != 1:
            raise ResetRefused(
                f"backend maintenance verification could not resolve {cluster}/{service}"
            )
        observed = services[0]
        counts = {
            "desired": int(observed.get("desiredCount", -1)),
            "running": int(observed.get("runningCount", -1)),
            "pending": int(observed.get("pendingCount", -1)),
        }
        if any(value != 0 for value in counts.values()):
            raise ResetRefused(
                "backend maintenance verification failed: ECS desired/running/pending "
                f"must all be zero, observed {counts}"
            )
        running_tasks = ecs.list_tasks(
            cluster=cluster,
            serviceName=service,
            desiredStatus="RUNNING",
        ).get("taskArns", [])
        if running_tasks:
            raise ResetRefused(
                "backend maintenance verification failed: ECS still lists running tasks"
            )

        resource_id = f"service/{cluster}/{service}"
        targets = scaling.describe_scalable_targets(
            ServiceNamespace="ecs",
            ResourceIds=[resource_id],
            ScalableDimension="ecs:service:DesiredCount",
        ).get("ScalableTargets", [])
        if len(targets) != 1:
            raise ResetRefused(
                "backend maintenance verification requires exactly one ECS scalable target"
            )
        target = targets[0]
        minimum = int(target.get("MinCapacity", -1))
        maximum = int(target.get("MaxCapacity", -1))
        if minimum != 0 or maximum != 0:
            raise ResetRefused(
                "backend maintenance verification failed: autoscaling min/max must both "
                f"be zero, observed min={minimum} max={maximum}"
            )
    except ResetRefused:
        raise
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", type(exc).__name__)
        raise ResetRefused(
            "backend maintenance control-plane verification failed "
            f"({code}); read-only ecs:DescribeServices, ecs:ListTasks and "
            "application-autoscaling:DescribeScalableTargets are required"
        ) from exc
    except Exception as exc:
        raise ResetRefused(
            "backend maintenance control-plane verification failed "
            f"({type(exc).__name__}); verify AWS credentials and read-only ECS/"
            "Application Auto Scaling permissions"
        ) from exc

    return {
        "control_plane_verified": True,
        "observed_at": datetime.now(UTC).isoformat(),
        "region": region,
        "cluster": cluster,
        "service": service,
        "ecs": {**counts, "running_task_count": 0},
        "application_autoscaling": {
            "resource_id": resource_id,
            "min_capacity": minimum,
            "max_capacity": maximum,
        },
    }


def _s3_client():
    return boto3.client("s3", region_name=get_settings().aws_region)


def _dynamo_table(name: str):
    return boto3.resource("dynamodb", region_name=get_settings().aws_region).Table(name)


def _discover_transcript_keys(
    table_name: str, session_ids: set[uuid.UUID]
) -> dict[str, list[dict[str, str]]]:
    table = _dynamo_table(table_name)
    result: dict[str, list[dict[str, str]]] = {}
    for session_id in sorted(str(value) for value in session_ids):
        keys: list[dict[str, str]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("session_id").eq(session_id),
            "ProjectionExpression": "session_id, sk",
            "ConsistentRead": True,
        }
        while True:
            response = table.query(**kwargs)
            keys.extend(
                {"session_id": str(item["session_id"]), "sk": str(item["sk"])}
                for item in response.get("Items", [])
            )
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
            kwargs["ExclusiveStartKey"] = cursor
        result[session_id] = sorted(keys, key=lambda item: item["sk"])
    return result


def _discover_s3_keys(
    bucket: str, build_ids: set[uuid.UUID], exact_keys: set[str]
) -> list[str]:
    client = _s3_client()
    keys = set(exact_keys)
    paginator = client.get_paginator("list_objects_v2")
    for build_id in sorted(str(value) for value in build_ids):
        artifact_prefix = f"artifacts/{build_id}/"
        workspace_prefix = f"builds/{build_id}"
        workspace_key_re = re.compile(
            BUILD_WORKSPACE_KEY_RE_TEMPLATE.format(build_id=re.escape(build_id))
        )
        for page in paginator.paginate(Bucket=bucket, Prefix=artifact_prefix):
            for entry in page.get("Contents", []):
                key = str(entry["Key"])
                if key.startswith(artifact_prefix):
                    keys.add(key)
        for page in paginator.paginate(Bucket=bucket, Prefix=workspace_prefix):
            for entry in page.get("Contents", []):
                key = str(entry["Key"])
                if workspace_key_re.match(key):
                    keys.add(key)
    return sorted(keys)


async def _discover_complete_scope(db: AsyncSession) -> ResetScope:
    scope = await _discover_database_scope(db)
    scope.transcript_keys, scope.s3_cleanup_keys = await asyncio.gather(
        asyncio.to_thread(
            _discover_transcript_keys, scope.dynamo_table, scope.session_ids
        ),
        asyncio.to_thread(
            _discover_s3_keys,
            scope.artifact_bucket,
            scope.build_ids,
            scope.artifact_s3_keys,
        ),
    )
    scope.before_counts["dynamo_chat_messages"] = sum(
        len(items) for items in scope.transcript_keys.values()
    )
    scope.before_counts["s3_artifact_objects"] = len(scope.s3_cleanup_keys)
    basis = _scope_basis(scope)
    scope.scope_sha256 = _digest(_canonical_bytes(basis))
    return scope


async def discover_scope() -> ResetScope:
    async with SessionLocal() as db:
        return await _discover_complete_scope(db)


def _scope_basis(scope: ResetScope) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "tenant_id": str(PLATFORM_TENANT_ID),
        "primary": scope.primary,
        "roster_user_ids": sorted(str(value) for value in scope.roster_user_ids),
        "service_user_ids": sorted(str(value) for value in scope.service_user_ids),
        "team_ids": sorted(str(value) for value in scope.team_ids),
        "project_ids": sorted(str(value) for value in scope.project_ids),
        "session_ids": sorted(str(value) for value in scope.session_ids),
        "build_ids": sorted(str(value) for value in scope.build_ids),
        "deployment_ids": sorted(str(value) for value in scope.deployment_ids),
        "lease_ids": sorted(str(value) for value in scope.lease_ids),
        "marketplace_link_ids": sorted(
            str(value) for value in scope.marketplace_link_ids
        ),
        "marketplace_links": scope.marketplace_links,
        "retained_detaches": scope.retained_detaches,
        "selected_ids": {
            name: sorted(str(value) for value in values)
            for name, values in sorted(scope.selected_ids.items())
        },
        "presence_keys": sorted(
            f"{project_id}:{user_id}" for project_id, user_id in scope.presence_keys
        ),
        "failed_history": scope.failed_history,
        "transcript_key_sha256": _digest(_canonical_bytes(scope.transcript_keys)),
        "artifact_s3_key_sha256": _digest(
            _canonical_bytes(sorted(scope.artifact_s3_keys))
        ),
        "s3_cleanup_key_sha256": _digest(_canonical_bytes(scope.s3_cleanup_keys)),
        "webhook_secret_names": scope.webhook_secret_names,
        "webhook_secret_name_sha256": _digest(
            _canonical_bytes(scope.webhook_secret_names)
        ),
        "evidence_sha256": {
            name: _digest(blob) for name, blob in sorted(scope.evidence_blobs.items())
        },
        "stores": {
            "artifact_bucket": scope.artifact_bucket,
            "evidence_bucket": scope.evidence_bucket,
            "dynamo_table": scope.dynamo_table,
        },
        "before_counts": scope.before_counts,
    }


def _summary(scope: ResetScope, *, mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "manifest_version": MANIFEST_VERSION,
        "scope_sha256": scope.scope_sha256,
        "confirmation_token": scope.confirmation_token,
        "maintenance_token_required_for_apply": scope.maintenance_token,
        "primary_identities_preserved": scope.primary,
        "service_user_ids_selected": sorted(
            str(value) for value in scope.service_user_ids
        ),
        "webhook_secret_names": scope.webhook_secret_names,
        "before_counts": scope.before_counts,
        "failed_history": scope.failed_history,
        "retained_detaches": scope.retained_detaches,
        "marketplace_links": scope.marketplace_links,
        "stores": {
            "artifact_bucket": scope.artifact_bucket,
            "evidence_bucket": scope.evidence_bucket,
            "dynamo_table": scope.dynamo_table,
        },
        "has_mutations": scope.has_mutations,
        "safety": {
            "cognito_calls": 0,
            "catalog_hard_deletes": 0,
            "live_resource_sql_teardown": False,
            "maintenance_control_plane_required": {
                "ecs_counts": {"desired": 0, "running": 0, "pending": 0},
                "application_autoscaling": {"min_capacity": 0, "max_capacity": 0},
            },
        },
    }


def _get_object_bytes(
    client: Any, bucket: str, key: str, *, required: bool = False
) -> bytes | None:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            if required:
                raise ResetRefused(
                    f"required evidence object s3://{bucket}/{key} does not exist"
                ) from exc
            return None
        raise
    stream = response["Body"]
    try:
        return stream.read()
    finally:
        stream.close()


def _assert_private_bucket(client: Any, bucket: str) -> None:
    try:
        response = client.get_public_access_block(Bucket=bucket)
    except ClientError as exc:
        raise ResetRefused(
            f"cannot verify private public-access block for evidence bucket {bucket}"
        ) from exc
    configuration = response.get("PublicAccessBlockConfiguration") or {}
    required = (
        "BlockPublicAcls",
        "IgnorePublicAcls",
        "BlockPublicPolicy",
        "RestrictPublicBuckets",
    )
    if not all(configuration.get(name) is True for name in required):
        raise ResetRefused(
            f"evidence bucket {bucket} is not protected by the complete S3 public-access block"
        )


def _put_verified_object(
    client: Any, bucket: str, key: str, body: bytes, content_type: str
) -> str:
    digest = _digest(body)
    existing_body = _get_object_bytes(client, bucket, key)
    if existing_body is not None:
        existing_digest = _digest(existing_body)
        if existing_digest != digest:
            raise ResetRefused(
                f"existing evidence object s3://{bucket}/{key} has SHA-256 "
                f"{existing_digest}, expected {digest}"
            )
        return digest
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        ServerSideEncryption="AES256",
        Metadata={"sha256": digest, "manifest-version": str(MANIFEST_VERSION)},
        ChecksumSHA256=base64.b64encode(sha256(body).digest()).decode(),
    )
    observed_body = _get_object_bytes(client, bucket, key, required=True)
    if observed_body is None or _digest(observed_body) != digest:
        raise ResetRefused(
            f"evidence GET/hash verification failed for s3://{bucket}/{key}"
        )
    return digest


def _verify_evidence_manifest(
    client: Any,
    bucket: str,
    manifest_body: bytes,
    *,
    scope_sha256: str,
    expected_scope_basis: dict[str, Any],
) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_body)
    except (TypeError, ValueError) as exc:
        raise ResetRefused("reset evidence manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ResetRefused("reset evidence manifest root must be an object")
    normalized_scope_basis = json.loads(_canonical_bytes(expected_scope_basis))
    if (
        manifest.get("manifest_kind") != MANIFEST_KIND
        or manifest.get("manifest_version") != MANIFEST_VERSION
        or manifest.get("scope_sha256") != scope_sha256
        or manifest.get("confirmation_basis") != normalized_scope_basis
        or _digest(_canonical_bytes(normalized_scope_basis)) != scope_sha256
    ):
        raise ResetRefused("reset evidence manifest scope contract is invalid")
    if manifest.get("retention") != {
        "disposition": "expire",
        "owner_approved": True,
        "policy": "existing-artifacts-lifecycle",
        "prefix": "artifacts/demo-reset/",
        "retention_days": RESET_EVIDENCE_RETENTION_DAYS,
    }:
        raise ResetRefused("reset evidence manifest retention declaration is invalid")

    descriptors = manifest.get("evidence")
    expected_digests = normalized_scope_basis.get("evidence_sha256")
    if not isinstance(descriptors, list) or not isinstance(expected_digests, dict):
        raise ResetRefused("reset evidence manifest object inventory is invalid")
    by_name: dict[str, dict[str, Any]] = {}
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or not isinstance(
            descriptor.get("name"), str
        ):
            raise ResetRefused("reset evidence manifest contains a malformed object")
        name = descriptor["name"]
        if name in by_name:
            raise ResetRefused(
                "reset evidence manifest contains duplicate object names"
            )
        by_name[name] = descriptor
    if set(by_name) != set(expected_digests):
        raise ResetRefused(
            "reset evidence manifest objects do not match the confirmed evidence set"
        )

    expected_prefix = f"{RESET_PREFIX}/{scope_sha256[:24]}/evidence/"
    for name, expected_digest in sorted(expected_digests.items()):
        descriptor = by_name[name]
        key = descriptor.get("key")
        if (
            descriptor.get("bucket") != bucket
            or key != f"{expected_prefix}{name}"
            or descriptor.get("sha256") != expected_digest
            or not isinstance(descriptor.get("bytes"), int)
            or descriptor["bytes"] < 0
            or not isinstance(descriptor.get("records"), int)
            or descriptor["records"] < 0
        ):
            raise ResetRefused(
                f"reset evidence manifest descriptor is invalid for {name!r}"
            )
        object_body = _get_object_bytes(client, bucket, key, required=True)
        if (
            object_body is None
            or len(object_body) != descriptor["bytes"]
            or _digest(object_body) != expected_digest
        ):
            raise ResetRefused(
                f"reset evidence object GET/hash mismatch for s3://{bucket}/{key}"
            )
    return manifest


def _export_evidence(
    scope: ResetScope,
    maintenance_token: str,
    maintenance_state: dict[str, Any],
) -> dict[str, Any]:
    client = _s3_client()
    _assert_private_bucket(client, scope.evidence_bucket)
    prefix = f"{RESET_PREFIX}/{scope.scope_sha256[:24]}"
    descriptors: list[dict[str, Any]] = []
    for filename, blob in sorted(scope.evidence_blobs.items()):
        key = f"{prefix}/evidence/{filename}"
        digest = _put_verified_object(
            client, scope.evidence_bucket, key, blob, "application/x-ndjson"
        )
        descriptors.append(
            {
                "name": filename,
                "bucket": scope.evidence_bucket,
                "key": key,
                "sha256": digest,
                "bytes": len(blob),
                "records": len(scope.evidence_rows[filename.removesuffix(".jsonl")]),
            }
        )

    manifest = {
        "manifest_kind": MANIFEST_KIND,
        "manifest_version": MANIFEST_VERSION,
        "scope_sha256": scope.scope_sha256,
        "confirmation_basis": _scope_basis(scope),
        "evidence": descriptors,
        "retention": {
            "disposition": "expire",
            "owner_approved": True,
            "policy": "existing-artifacts-lifecycle",
            "prefix": "artifacts/demo-reset/",
            "retention_days": RESET_EVIDENCE_RETENTION_DAYS,
        },
        "maintenance": {
            "backend_scaled_to_zero_attested": True,
            "token_sha256": _digest(maintenance_token.encode()),
            "control_plane": maintenance_state,
        },
        "deletion_contract": {
            "primary_postgres_identities": "preserve",
            "primary_cognito_identities": "preserve_no_api_calls",
            "roster_users": "delete_after_dependents",
            "marketplace_rows": "preserve_and_detach_nonpublished_source_links",
            "catalog_hard_delete": False,
            "live_resources": "must_be_torn_down_before_reset",
        },
    }
    manifest_body = (
        json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            default=_json_default,
        ).encode()
        + b"\n"
    )
    manifest_key = f"{prefix}/manifest.json"
    manifest_sha = _put_verified_object(
        client,
        scope.evidence_bucket,
        manifest_key,
        manifest_body,
        "application/json",
    )
    _verify_evidence_manifest(
        client,
        scope.evidence_bucket,
        manifest_body,
        scope_sha256=scope.scope_sha256,
        expected_scope_basis=_scope_basis(scope),
    )
    return {
        "bucket": scope.evidence_bucket,
        "key": manifest_key,
        "sha256": manifest_sha,
        "evidence": descriptors,
    }


def _json_document(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            default=_json_default,
        ).encode()
        + b"\n"
    )


def _cleanup_descriptor_payload(
    scope: ResetScope,
    manifest: dict[str, Any],
    maintenance_token: str,
    maintenance_state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "descriptor_kind": CLEANUP_DESCRIPTOR_KIND,
        "descriptor_version": MANIFEST_VERSION,
        "tenant_id": str(PLATFORM_TENANT_ID),
        "scope_sha256": scope.scope_sha256,
        "scope_basis": _scope_basis(scope),
        "manifest": {
            "bucket": manifest["bucket"],
            "key": manifest["key"],
            "sha256": manifest["sha256"],
        },
        "maintenance": {
            "backend_scaled_to_zero_attested": True,
            "token_sha256": _digest(maintenance_token.encode()),
            "control_plane": maintenance_state,
        },
        "stores": {
            "artifact_bucket": scope.artifact_bucket,
            "evidence_bucket": scope.evidence_bucket,
            "dynamo_table": scope.dynamo_table,
        },
        "transcript_keys": scope.transcript_keys,
        "artifact_s3_keys": sorted(scope.artifact_s3_keys),
        "s3_cleanup_keys": scope.s3_cleanup_keys,
        "webhook_secret_names": scope.webhook_secret_names,
    }


def _persist_cleanup_descriptor(
    scope: ResetScope,
    manifest: dict[str, Any],
    maintenance_token: str,
    maintenance_state: dict[str, Any],
) -> dict[str, Any]:
    payload = _cleanup_descriptor_payload(
        scope, manifest, maintenance_token, maintenance_state
    )
    body = _json_document(payload)
    digest = _digest(body)
    key = f"{RESET_PREFIX}/cleanup/descriptors/{digest}.json"
    client = _s3_client()
    observed = _put_verified_object(
        client,
        scope.evidence_bucket,
        key,
        body,
        "application/json",
    )
    if observed != digest:
        raise ResetRefused("cleanup descriptor content-address verification failed")
    descriptor = {
        "bucket": scope.evidence_bucket,
        "key": key,
        "sha256": digest,
    }
    pointer_payload = {
        "pointer_kind": CLEANUP_POINTER_KIND,
        "pointer_version": MANIFEST_VERSION,
        "tenant_id": str(PLATFORM_TENANT_ID),
        "scope_sha256": scope.scope_sha256,
        "descriptor": descriptor,
    }
    pointer_body = _json_document(pointer_payload)
    pointer_sha = _digest(pointer_body)
    pointer_key = f"{RESET_PREFIX}/cleanup/scopes/{scope.scope_sha256}.json"
    pointer_observed = _put_verified_object(
        client,
        scope.evidence_bucket,
        pointer_key,
        pointer_body,
        "application/json",
    )
    if pointer_observed != pointer_sha:
        raise ResetRefused("cleanup scope pointer GET/hash verification failed")
    return {
        **descriptor,
        "scope_pointer": {
            "bucket": scope.evidence_bucket,
            "key": pointer_key,
            "sha256": pointer_sha,
        },
    }


def _validate_cleanup_descriptor(
    payload: dict[str, Any],
    descriptor_sha256: str,
    evidence_bucket: str,
) -> None:
    if (
        payload.get("descriptor_kind") != CLEANUP_DESCRIPTOR_KIND
        or payload.get("descriptor_version") != MANIFEST_VERSION
        or payload.get("tenant_id") != str(PLATFORM_TENANT_ID)
    ):
        raise ResetRefused("cleanup descriptor kind/version/tenant is not supported")
    scope_sha = payload.get("scope_sha256")
    basis = payload.get("scope_basis")
    if (
        not isinstance(scope_sha, str)
        or not SHA256_RE.fullmatch(scope_sha)
        or not isinstance(basis, dict)
        or _digest(_canonical_bytes(basis)) != scope_sha
    ):
        raise ResetRefused("cleanup descriptor scope basis digest is invalid")
    stores = payload.get("stores") or {}
    settings = get_settings()
    expected_artifact_bucket = settings.codegen_workspace_bucket
    expected_evidence_bucket = os.environ.get(
        "DEMO_RESET_EVIDENCE_BUCKET", expected_artifact_bucket
    )
    if (
        stores
        != {
            "artifact_bucket": expected_artifact_bucket,
            "evidence_bucket": expected_evidence_bucket,
            "dynamo_table": settings.dynamo_table_chat,
        }
        or evidence_bucket != expected_evidence_bucket
    ):
        raise ResetRefused(
            "cleanup descriptor stores do not match the current environment"
        )
    manifest = payload.get("manifest") or {}
    if (
        manifest.get("bucket") != evidence_bucket
        or not isinstance(manifest.get("key"), str)
        or not manifest["key"].startswith(f"{RESET_PREFIX}/")
        or not isinstance(manifest.get("sha256"), str)
        or not SHA256_RE.fullmatch(manifest["sha256"])
    ):
        raise ResetRefused("cleanup descriptor manifest reference is invalid")
    session_ids = set(basis.get("session_ids") or [])
    transcript_keys = payload.get("transcript_keys")
    if not isinstance(transcript_keys, dict):
        raise ResetRefused("cleanup descriptor transcript key map is invalid")
    for session_id, keys in transcript_keys.items():
        if session_id not in session_ids or not isinstance(keys, list):
            raise ResetRefused(
                "cleanup descriptor contains an unselected Dynamo partition"
            )
        for key in keys:
            if (
                not isinstance(key, dict)
                or key.get("session_id") != session_id
                or not isinstance(key.get("sk"), str)
            ):
                raise ResetRefused("cleanup descriptor contains a malformed Dynamo key")
    if _digest(_canonical_bytes(transcript_keys)) != basis.get("transcript_key_sha256"):
        raise ResetRefused("cleanup descriptor Dynamo inventory digest is invalid")
    build_ids = set(basis.get("build_ids") or [])
    s3_keys = payload.get("s3_cleanup_keys")
    artifact_keys = payload.get("artifact_s3_keys")
    if not isinstance(s3_keys, list) or not isinstance(artifact_keys, list):
        raise ResetRefused("cleanup descriptor S3 key inventory is invalid")
    if _digest(_canonical_bytes(artifact_keys)) != basis.get(
        "artifact_s3_key_sha256"
    ) or _digest(_canonical_bytes(s3_keys)) != basis.get("s3_cleanup_key_sha256"):
        raise ResetRefused("cleanup descriptor S3 inventory digest is invalid")
    if len(s3_keys) != len(set(s3_keys)) or s3_keys != sorted(s3_keys):
        raise ResetRefused("cleanup descriptor S3 keys are duplicated or unsorted")
    for key in s3_keys:
        if not isinstance(key, str):
            raise ResetRefused("cleanup descriptor contains a non-string S3 key")
        allowed = any(
            key.startswith(f"artifacts/{build_id}/")
            or re.match(
                BUILD_WORKSPACE_KEY_RE_TEMPLATE.format(build_id=re.escape(build_id)),
                key,
            )
            for build_id in build_ids
        )
        if not allowed:
            raise ResetRefused(
                f"cleanup descriptor contains an off-prefix S3 key: {key!r}"
            )
    if not set(artifact_keys).issubset(set(s3_keys)):
        raise ResetRefused(
            "cleanup descriptor artifact keys are not in its S3 inventory"
        )
    webhook_secret_names = payload.get("webhook_secret_names")
    selected_ids = basis.get("selected_ids")
    raw_endpoint_ids = (
        selected_ids.get("webhook_endpoints")
        if isinstance(selected_ids, dict)
        else None
    )
    if (
        not isinstance(raw_endpoint_ids, list)
        or any(not isinstance(value, str) for value in raw_endpoint_ids)
        or raw_endpoint_ids != sorted(set(raw_endpoint_ids))
    ):
        raise ResetRefused("cleanup descriptor webhook endpoint inventory is invalid")
    endpoint_ids: list[str] = []
    for endpoint_id in raw_endpoint_ids:
        try:
            parsed_endpoint_id = uuid.UUID(endpoint_id)
        except ValueError as exc:
            raise ResetRefused(
                "cleanup descriptor contains an invalid webhook endpoint id"
            ) from exc
        if str(parsed_endpoint_id) != endpoint_id:
            raise ResetRefused(
                "cleanup descriptor webhook endpoint IDs are not canonical"
            )
        endpoint_ids.append(endpoint_id)
    expected_secret_names = [
        f"{WEBHOOK_SECRET_PREFIX}{endpoint_id}" for endpoint_id in endpoint_ids
    ]
    if (
        not isinstance(webhook_secret_names, list)
        or webhook_secret_names != expected_secret_names
        or basis.get("webhook_secret_names") != expected_secret_names
        or _digest(_canonical_bytes(webhook_secret_names))
        != basis.get("webhook_secret_name_sha256")
    ):
        raise ResetRefused(
            "cleanup descriptor webhook secret inventory is not the exact selected "
            "endpoint set"
        )
    maintenance = payload.get("maintenance") or {}
    expected_token = f"BACKEND-SCALED-ZERO-{scope_sha[:16].upper()}"
    if maintenance.get(
        "backend_scaled_to_zero_attested"
    ) is not True or maintenance.get("token_sha256") != _digest(
        expected_token.encode()
    ):
        raise ResetRefused("cleanup descriptor maintenance attestation is invalid")
    control_plane = maintenance.get("control_plane")
    if control_plane is not None:
        ecs_state = control_plane.get("ecs") if isinstance(control_plane, dict) else None
        scaling_state = (
            control_plane.get("application_autoscaling")
            if isinstance(control_plane, dict)
            else None
        )
        if (
            not isinstance(control_plane, dict)
            or control_plane.get("control_plane_verified") is not True
            or not isinstance(ecs_state, dict)
            or any(ecs_state.get(key) != 0 for key in ("desired", "running", "pending"))
            or not isinstance(scaling_state, dict)
            or scaling_state.get("min_capacity") != 0
            or scaling_state.get("max_capacity") != 0
        ):
            raise ResetRefused(
                "cleanup descriptor control-plane maintenance evidence is invalid"
            )
    if not SHA256_RE.fullmatch(descriptor_sha256):
        raise ResetRefused("cleanup descriptor SHA-256 is invalid")


def _load_cleanup_descriptor(
    descriptor_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not SHA256_RE.fullmatch(descriptor_sha256):
        raise ResetRefused("--resume-external requires a full lowercase SHA-256")
    settings = get_settings()
    evidence_bucket = os.environ.get(
        "DEMO_RESET_EVIDENCE_BUCKET", settings.codegen_workspace_bucket
    )
    if not evidence_bucket:
        raise ResetRefused("DEMO_RESET_EVIDENCE_BUCKET is required to resume cleanup")
    key = f"{RESET_PREFIX}/cleanup/descriptors/{descriptor_sha256}.json"
    client = _s3_client()
    _assert_private_bucket(client, evidence_bucket)
    body = _get_object_bytes(client, evidence_bucket, key, required=True)
    if body is None or _digest(body) != descriptor_sha256:
        raise ResetRefused("cleanup descriptor body does not match its content address")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ResetRefused("cleanup descriptor is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ResetRefused("cleanup descriptor root must be an object")
    _validate_cleanup_descriptor(payload, descriptor_sha256, evidence_bucket)
    manifest = payload["manifest"]
    manifest_body = _get_object_bytes(
        client, evidence_bucket, manifest["key"], required=True
    )
    if manifest_body is None or _digest(manifest_body) != manifest["sha256"]:
        raise ResetRefused("cleanup descriptor manifest content digest is invalid")
    _verify_evidence_manifest(
        client,
        evidence_bucket,
        manifest_body,
        scope_sha256=payload["scope_sha256"],
        expected_scope_basis=payload["scope_basis"],
    )

    descriptor = {
        "bucket": evidence_bucket,
        "key": key,
        "sha256": descriptor_sha256,
    }
    pointer_payload = {
        "pointer_kind": CLEANUP_POINTER_KIND,
        "pointer_version": MANIFEST_VERSION,
        "tenant_id": str(PLATFORM_TENANT_ID),
        "scope_sha256": payload["scope_sha256"],
        "descriptor": descriptor,
    }
    pointer_body = _json_document(pointer_payload)
    pointer_key = f"{RESET_PREFIX}/cleanup/scopes/{payload['scope_sha256']}.json"
    observed_pointer = _get_object_bytes(
        client, evidence_bucket, pointer_key, required=True
    )
    if observed_pointer != pointer_body:
        raise ResetRefused(
            "cleanup scope pointer does not resolve to the requested descriptor"
        )
    descriptor["scope_pointer"] = {
        "bucket": evidence_bucket,
        "key": pointer_key,
        "sha256": _digest(pointer_body),
    }
    return payload, descriptor


def _as_uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _scope_from_cleanup_descriptor(payload: dict[str, Any]) -> ResetScope:
    basis = payload["scope_basis"]
    primary = basis["primary"]
    roster_ids = {_as_uuid(value) for value in basis["roster_user_ids"]}
    service_ids = {_as_uuid(value) for value in basis["service_user_ids"]}
    primary_ids = {_as_uuid(value["id"]) for value in primary.values()}
    retained_detaches = []
    for item in basis.get("retained_detaches") or []:
        converted = dict(item)
        converted["id"] = _as_uuid(converted["id"])
        before = converted.get("before")
        if isinstance(before, str):
            try:
                converted["before"] = uuid.UUID(before)
            except ValueError:
                pass
        retained_detaches.append(converted)
    scope = ResetScope(
        primary=primary,
        roster_user_ids=roster_ids,
        service_user_ids=service_ids,
        user_ids=primary_ids | roster_ids | service_ids,
        team_ids={_as_uuid(value) for value in basis["team_ids"]},
        project_ids={_as_uuid(value) for value in basis["project_ids"]},
        session_ids={_as_uuid(value) for value in basis["session_ids"]},
        generation_ids=set(),
        build_ids={_as_uuid(value) for value in basis["build_ids"]},
        deployment_ids={_as_uuid(value) for value in basis["deployment_ids"]},
        lease_ids={_as_uuid(value) for value in basis["lease_ids"]},
        marketplace_link_ids={
            _as_uuid(value) for value in basis["marketplace_link_ids"]
        },
        marketplace_links=list(basis.get("marketplace_links") or []),
        retained_detaches=retained_detaches,
        selected_ids={
            name: {_as_uuid(value) for value in values}
            for name, values in basis["selected_ids"].items()
        },
        presence_keys={
            tuple(_as_uuid(value) for value in item.split(":", 1))
            for item in basis.get("presence_keys") or []
        },
        failed_history=list(basis.get("failed_history") or []),
        transcript_keys=payload["transcript_keys"],
        artifact_s3_keys=set(payload["artifact_s3_keys"]),
        s3_cleanup_keys=payload["s3_cleanup_keys"],
        webhook_secret_names=payload["webhook_secret_names"],
        artifact_bucket=payload["stores"]["artifact_bucket"],
        evidence_bucket=payload["stores"]["evidence_bucket"],
        dynamo_table=payload["stores"]["dynamo_table"],
        before_counts=dict(basis.get("before_counts") or {}),
        scope_sha256=payload["scope_sha256"],
    )
    return scope


def _persist_cleanup_receipt(
    scope: ResetScope,
    descriptor: dict[str, Any],
    *,
    phase: str,
    result: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "receipt_kind": CLEANUP_RECEIPT_KIND,
        "receipt_version": MANIFEST_VERSION,
        "recorded_at": datetime.now(UTC).isoformat(),
        "scope_sha256": scope.scope_sha256,
        "descriptor": descriptor,
        "phase": phase,
        "result": result,
        **details,
    }
    body = _json_document(payload)
    digest = _digest(body)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    key = (
        f"{RESET_PREFIX}/cleanup/receipts/{descriptor['sha256']}/"
        f"{timestamp}-{phase}-{result}-{digest}.json"
    )
    observed = _put_verified_object(
        _s3_client(),
        scope.evidence_bucket,
        key,
        body,
        "application/json",
    )
    if observed != digest:
        raise ResetRefused("cleanup receipt content-address verification failed")
    return {
        "bucket": scope.evidence_bucket,
        "key": key,
        "sha256": digest,
        "phase": phase,
        "result": result,
    }


def _delete_transcripts(scope: ResetScope) -> int:
    table = _dynamo_table(scope.dynamo_table)
    expected = sum(len(items) for items in scope.transcript_keys.values())
    if expected:
        with table.batch_writer() as batch:
            for session_id in sorted(scope.transcript_keys):
                for key in scope.transcript_keys[session_id]:
                    batch.delete_item(Key=key)
    remaining = _discover_transcript_keys(scope.dynamo_table, scope.session_ids)
    residual = sum(len(items) for items in remaining.values())
    if residual:
        raise ResetRefused(
            f"Dynamo transcript cleanup left {residual} selected messages"
        )
    return expected


def _delete_s3_objects(scope: ResetScope) -> int:
    if not scope.s3_cleanup_keys:
        return 0
    client = _s3_client()
    for start in range(0, len(scope.s3_cleanup_keys), 1000):
        batch = scope.s3_cleanup_keys[start : start + 1000]
        response = client.delete_objects(
            Bucket=scope.artifact_bucket,
            Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
        )
        errors = response.get("Errors") or []
        if errors:
            raise ResetRefused(f"S3 artifact cleanup failed: {errors[:5]}")
    residual = _discover_s3_keys(scope.artifact_bucket, scope.build_ids, set())
    selected_residual = sorted(set(residual) & set(scope.s3_cleanup_keys))
    if selected_residual:
        raise ResetRefused(
            f"S3 artifact cleanup left {len(selected_residual)} selected objects"
        )
    return len(scope.s3_cleanup_keys)


def _secrets_client():
    return boto3.client("secretsmanager", region_name=get_settings().aws_region)


def _active_webhook_secret_count(scope: ResetScope) -> int:
    client = _secrets_client()
    active = 0
    for name in scope.webhook_secret_names:
        try:
            response = client.describe_secret(SecretId=name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                continue
            raise
        if response.get("DeletedDate") is None:
            active += 1
    return active


def _delete_webhook_secrets(scope: ResetScope) -> int:
    client = _secrets_client()
    for name in scope.webhook_secret_names:
        try:
            current = client.describe_secret(SecretId=name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                continue
            raise
        if current.get("DeletedDate") is not None:
            continue
        try:
            client.delete_secret(
                SecretId=name,
                RecoveryWindowInDays=WEBHOOK_SECRET_RECOVERY_DAYS,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ResourceNotFoundException":
                continue
            if code == "InvalidRequestException":
                try:
                    rechecked = client.describe_secret(SecretId=name)
                except ClientError as recheck_exc:
                    if (
                        recheck_exc.response.get("Error", {}).get("Code")
                        == "ResourceNotFoundException"
                    ):
                        continue
                    raise
                if rechecked.get("DeletedDate") is not None:
                    continue
            raise
    residual = _active_webhook_secret_count(scope)
    if residual:
        raise ResetRefused(
            f"Secrets Manager cleanup left {residual} selected webhook secrets active"
        )
    return len(scope.webhook_secret_names)


async def _delete_ids(
    db: AsyncSession,
    model: Any,
    ids: set[uuid.UUID],
) -> int:
    if not ids:
        return 0
    result = await db.execute(
        delete(model)
        .where(model.id.in_(ids))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


async def _recheck_lifecycle(db: AsyncSession, scope: ResetScope) -> None:
    builds = await _rows(db, CodegenBuild, CodegenBuild.id.in_(scope.build_ids))
    deployments = await _rows(db, Deployment, Deployment.id.in_(scope.deployment_ids))
    leases = await _rows(db, Lease, Lease.id.in_(scope.lease_ids))
    _assert_lifecycle_safe(builds, deployments, leases)
    new_builds = await _rows(
        db,
        CodegenBuild,
        CodegenBuild.project_id.in_(scope.project_ids),
        CodegenBuild.status.in_(ACTIVE_BUILD_STATES),
    )
    new_deployments = await _rows(
        db,
        Deployment,
        Deployment.project_id.in_(scope.project_ids),
        or_(
            Deployment.status == "active",
            Deployment.status.in_(IN_FLIGHT_DEPLOYMENT_STATES),
        ),
    )
    new_leases = await _rows(
        db,
        Lease,
        Lease.project_id.in_(scope.project_ids),
        Lease.status != "terminated",
    )
    if new_builds or new_deployments or new_leases:
        raise ResetRefused(
            "lifecycle changed after manifest export; no relational rows were deleted"
        )


async def _detach_retained_references(db: AsyncSession, scope: ResetScope) -> None:
    for item in scope.retained_detaches:
        model = DETACH_MODEL_BY_TABLE[item["table"]]
        row = await db.get(model, item["id"])
        if row is None or getattr(row, item["field"]) != item["before"]:
            raise ResetRefused(
                "confirmed retained reference changed before apply: "
                f"{item['table']}:{item['id']}:{item['field']}"
            )
        setattr(row, item["field"], None)

    for item in scope.marketplace_links:
        row = await db.get(MarketplaceSample, item["id"])
        if (
            row is None
            or row.source_project_id != item["source_project_id"]
            or row.status != item["status"]
        ):
            raise ResetRefused(
                f"confirmed marketplace link changed before apply: {item['id']}"
            )
        if row.status == "published":
            raise ResetRefused(
                f"published marketplace row {row.id} appeared during reset"
            )
        row.source_project_id = None
        if row.status in {"draft", "submitted"}:
            row.status = "archived"


async def _delete_relational(
    confirmed_scope: ResetScope,
) -> tuple[dict[str, int], ResetScope]:
    deleted: dict[str, int] = {}
    async with SessionLocal() as db:
        try:
            bind = db.get_bind()
            if bind.dialect.name == "postgresql":
                # Match deployment admission's lock first, then take the
                # reset-specific lock. Both are transaction-scoped and remain
                # held through the complete scope recheck and commit.
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
                    {
                        "namespace": DEPLOYMENT_ADMISSION_LOCK_NAMESPACE,
                        "key": DEPLOYMENT_ADMISSION_LOCK_KEY,
                    },
                )
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": RESET_LOCK_KEY},
                )
            scope = await _discover_complete_scope(db)
            if scope.scope_sha256 != confirmed_scope.scope_sha256:
                raise ResetRefused(
                    "complete reset scope changed under maintenance/admission lock; "
                    "no relational rows were deleted. Run dry-run again."
                )
            await _detach_retained_references(db, scope)

            risk_ids = scope.selected_ids["ai_risk_assessments"]
            if risk_ids:
                # Retained incoming links were confirmed and detached above;
                # break links among the selected rows before bulk deletion.
                await db.execute(
                    update(AiRiskAssessment)
                    .where(
                        AiRiskAssessment.id.in_(risk_ids),
                        AiRiskAssessment.resubmission_of.in_(risk_ids),
                    )
                    .values(resubmission_of=None)
                    .execution_options(synchronize_session=False)
                )

            order = [
                "webhook_deliveries",
                "codegen_artifacts",
                "sandbox_spend",
                "spec_generations",
                "spec_drafts",
                "spec_comments",
                "project_members",
                "team_members",
                "service_account_tokens",
                "reviewer_group_members",
                "notifications",
                "usage_events",
                "ai_risk_assessments",
                "audit_logs",
                "model_invocations",
                "alerts",
                "specs",
                "chat_sessions",
                "deployments",
                "leases",
                "codegen_builds",
                "webhook_endpoints",
            ]
            for name in order:
                deleted[name] = await _delete_ids(
                    db, MODEL_BY_NAME[name], scope.selected_ids[name]
                )

            if scope.presence_keys:
                presence_condition = or_(
                    *(
                        (ProjectPresence.project_id == project_id)
                        & (ProjectPresence.user_id == user_id)
                        for project_id, user_id in scope.presence_keys
                    )
                )
                result = await db.execute(
                    delete(ProjectPresence)
                    .where(presence_condition)
                    .execution_options(synchronize_session=False)
                )
                deleted["project_presence"] = result.rowcount or 0
            else:
                deleted["project_presence"] = 0

            deleted["projects"] = await _delete_ids(db, Project, scope.project_ids)
            deleted["teams"] = await _delete_ids(db, Team, scope.team_ids)
            deleted["service_users"] = await _delete_ids(
                db, User, scope.service_user_ids
            )
            deleted["roster_users"] = await _delete_ids(db, User, scope.roster_user_ids)

            await db.flush()
            remaining_primary = await _rows(
                db,
                User,
                User.id.in_({uuid.UUID(item["id"]) for item in scope.primary.values()}),
            )
            if len(remaining_primary) != len(scope.primary):
                raise ResetRefused(
                    "primary identity postcondition failed; rolling back"
                )
            remaining_projects = await _rows(
                db, Project, Project.id.in_(scope.project_ids)
            )
            remaining_services = await _rows(
                db, User, User.id.in_(scope.service_user_ids)
            )
            remaining_roster = await _rows(db, User, User.id.in_(scope.roster_user_ids))
            if remaining_projects or remaining_services or remaining_roster:
                raise ResetRefused(
                    "project/service/roster deletion postcondition failed; rolling back"
                )
            await db.commit()
            return deleted, scope
        except Exception:
            await db.rollback()
            raise


async def _after_counts(
    scope: ResetScope, *, include_external: bool = True
) -> dict[str, int]:
    result: dict[str, int] = {}
    async with SessionLocal() as db:
        for name, ids in scope.selected_ids.items():
            model = MODEL_BY_NAME[name]
            rows = await _rows(db, model, model.id.in_(ids))
            result[name] = len(rows)
        result["projects"] = len(
            await _rows(db, Project, Project.id.in_(scope.project_ids))
        )
        result["teams"] = len(await _rows(db, Team, Team.id.in_(scope.team_ids)))
        result["service_users"] = len(
            await _rows(db, User, User.id.in_(scope.service_user_ids))
        )
        result["roster_users"] = len(
            await _rows(db, User, User.id.in_(scope.roster_user_ids))
        )
        if scope.presence_keys:
            presence_condition = or_(
                *(
                    (ProjectPresence.project_id == project_id)
                    & (ProjectPresence.user_id == user_id)
                    for project_id, user_id in scope.presence_keys
                )
            )
            result["project_presence"] = len(
                await _rows(db, ProjectPresence, presence_condition)
            )
        else:
            result["project_presence"] = 0
        result["primary_users_preserved"] = len(
            await _rows(
                db,
                User,
                User.id.in_({uuid.UUID(item["id"]) for item in scope.primary.values()}),
            )
        )
        result["marketplace_links_to_detach"] = len(
            await _rows(
                db,
                MarketplaceSample,
                MarketplaceSample.source_project_id.in_(scope.project_ids),
            )
        )
        remaining_detaches = 0
        for item in scope.retained_detaches:
            model = DETACH_MODEL_BY_TABLE[item["table"]]
            row = await db.get(model, item["id"])
            if row is not None and getattr(row, item["field"]) is not None:
                remaining_detaches += 1
        result["retained_references_to_detach"] = remaining_detaches
    if include_external:
        remaining_transcripts = await asyncio.to_thread(
            _discover_transcript_keys, scope.dynamo_table, scope.session_ids
        )
        result["dynamo_chat_messages"] = sum(
            len(items) for items in remaining_transcripts.values()
        )
        discovered_s3 = await asyncio.to_thread(
            _discover_s3_keys, scope.artifact_bucket, scope.build_ids, set()
        )
        result["s3_artifact_objects"] = len(discovered_s3)
        result["webhook_secrets"] = await asyncio.to_thread(
            _active_webhook_secret_count, scope
        )
    return result


async def _primary_identity_snapshot(scope: ResetScope) -> dict[str, dict[str, Any]]:
    expected_ids = {_as_uuid(item["id"]) for item in scope.primary.values()}
    async with SessionLocal() as db:
        rows = await _rows(db, User, User.id.in_(expected_ids))
    by_email = {row.email: row for row in rows}
    observed: dict[str, dict[str, Any]] = {}
    for email, expected in scope.primary.items():
        row = by_email.get(email)
        if (
            row is None
            or str(row.id) != str(expected["id"])
            or row.kind != "human"
            or row.role != expected["role"]
            or row.status != expected["status"]
            or row.cognito_sub != expected["cognito_sub"]
        ):
            raise ResetRefused(
                f"preserved primary identity changed or disappeared after reset: {email}"
            )
        observed[email] = {
            "id": str(row.id),
            "cognito_sub": row.cognito_sub,
            "role": row.role,
            "status": row.status,
            "preserved": True,
        }
    if len(rows) != len(scope.primary):
        raise ResetRefused("primary identity after-state is ambiguous")
    return observed


def _non_primary_residue(
    after: dict[str, int], *, include_external: bool
) -> dict[str, int]:
    ignored = {"primary_users_preserved"}
    if not include_external:
        ignored |= {
            "dynamo_chat_messages",
            "s3_artifact_objects",
            "webhook_secrets",
        }
    return {
        name: count for name, count in after.items() if name not in ignored and count
    }


async def _assert_relational_commit_visible(
    scope: ResetScope,
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    after = await _after_counts(scope, include_external=False)
    primary = await _primary_identity_snapshot(scope)
    residue = _non_primary_residue(after, include_external=False)
    if residue or after.get("primary_users_preserved") != len(scope.primary):
        raise ResetRefused(
            "external cleanup cannot start until the descriptor's exact relational "
            f"scope is committed clean: {after}"
        )
    return after, primary


async def _run_external_cleanup(
    scope: ResetScope,
    descriptor: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    try:
        relational_after, primary = await _assert_relational_commit_visible(scope)
        committed_receipt = await asyncio.to_thread(
            _persist_cleanup_receipt,
            scope,
            descriptor,
            phase="relational",
            result="committed",
            details={
                "mode": mode,
                "after_counts": relational_after,
                "primary_identities": primary,
            },
        )
        (
            deleted_messages,
            deleted_objects,
            deleted_webhook_secrets,
        ) = await asyncio.gather(
            asyncio.to_thread(_delete_transcripts, scope),
            asyncio.to_thread(_delete_s3_objects, scope),
            asyncio.to_thread(_delete_webhook_secrets, scope),
        )
        after = await _after_counts(scope)
        primary = await _primary_identity_snapshot(scope)
        residue = _non_primary_residue(after, include_external=True)
        if residue or after.get("primary_users_preserved") != len(scope.primary):
            raise ResetRefused(
                f"reset postcondition failed after external cleanup: {after}"
            )
        completion = await asyncio.to_thread(
            _persist_cleanup_receipt,
            scope,
            descriptor,
            phase="external",
            result="completed",
            details={
                "mode": mode,
                "after_counts": after,
                "primary_identities": primary,
                "cleanup": {
                    "dynamo_chat_messages": deleted_messages,
                    "s3_artifact_objects": deleted_objects,
                    "webhook_secrets_scheduled_for_deletion": deleted_webhook_secrets,
                    "webhook_secret_recovery_window_days": WEBHOOK_SECRET_RECOVERY_DAYS,
                },
            },
        )
        return {
            "deleted_messages": deleted_messages,
            "deleted_objects": deleted_objects,
            "deleted_webhook_secrets": deleted_webhook_secrets,
            "after_counts": after,
            "primary_identities": primary,
            "relational_receipt": committed_receipt,
            "completion_receipt": completion,
        }
    except Exception as exc:
        try:
            failure = await asyncio.to_thread(
                _persist_cleanup_receipt,
                scope,
                descriptor,
                phase="external",
                result="failed",
                details={
                    "mode": mode,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                    "resume_external": descriptor["sha256"],
                },
            )
        except Exception as receipt_exc:
            raise ResetRefused(
                "external cleanup failed after relational commit and its failure receipt "
                f"could not be persisted; resume with --resume-external {descriptor['sha256']}: "
                f"cleanup={exc}; receipt={receipt_exc}"
            ) from exc
        raise ResetRefused(
            "external cleanup failed after relational commit; retry idempotently with "
            f"--resume-external {descriptor['sha256']}; failure_receipt={failure['key']}"
        ) from exc


def _emit_json(payload: dict[str, Any], *, indent: int | None = 2) -> None:
    print(
        json.dumps(
            payload,
            indent=indent,
            sort_keys=True,
            default=_json_default,
        ),
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only plan (default)")
    mode.add_argument("--apply", action="store_true", help="apply the confirmed reset")
    mode.add_argument(
        "--resume-external",
        metavar="DESCRIPTOR_SHA256",
        help="resume exact idempotent Dynamo/S3/webhook-secret cleanup after relational commit",
    )
    parser.add_argument(
        "--confirm",
        help="exact RESET-DEMO-... token emitted by an unchanged dry run",
    )
    parser.add_argument(
        "--maintenance-token",
        help=(
            "exact BACKEND-SCALED-ZERO-... token emitted by dry-run; apply/resume "
            "also verify ECS and Application Auto Scaling read-only"
        ),
    )
    parser.add_argument(
        "--ecs-cluster",
        default=DEFAULT_ECS_CLUSTER,
        help=f"backend ECS cluster (default: {DEFAULT_ECS_CLUSTER})",
    )
    parser.add_argument(
        "--ecs-service",
        default=DEFAULT_ECS_SERVICE,
        help=f"backend ECS service (default: {DEFAULT_ECS_SERVICE})",
    )
    return parser.parse_args()


async def _resume_external(args: argparse.Namespace) -> None:
    payload, descriptor = await asyncio.to_thread(
        _load_cleanup_descriptor, args.resume_external
    )
    scope = _scope_from_cleanup_descriptor(payload)
    if args.maintenance_token != scope.maintenance_token:
        raise ResetRefused(
            "maintenance attestation missing or stale; confirm the backend is still scaled "
            f"to zero and pass --maintenance-token {scope.maintenance_token}"
        )
    maintenance_state = await asyncio.to_thread(
        _verify_backend_maintenance_state,
        getattr(args, "ecs_cluster", DEFAULT_ECS_CLUSTER),
        getattr(args, "ecs_service", DEFAULT_ECS_SERVICE),
    )
    result = await _run_external_cleanup(scope, descriptor, mode="resume-external")
    _emit_json(
        {
            "result": "external_cleanup_resumed_and_completed",
            "scope_sha256": scope.scope_sha256,
            "cleanup_descriptor": descriptor,
            "maintenance": maintenance_state,
            "after_counts": result["after_counts"],
            "primary_identities_preserved": result["primary_identities"],
            "receipts": {
                "relational": result["relational_receipt"],
                "completion": result["completion_receipt"],
            },
            "cognito_api_calls": 0,
            "catalog_hard_deletes": 0,
        }
    )


async def run(args: argparse.Namespace) -> None:
    if args.resume_external:
        await _resume_external(args)
        return

    scope = await discover_scope()
    _emit_json(_summary(scope, mode="apply" if args.apply else "dry-run"))
    if not args.apply:
        print(
            "demo reset dry-run complete; no writes or deletes performed",
            flush=True,
        )
        return
    if args.confirm != scope.confirmation_token:
        raise ResetRefused(
            "confirmation token missing or stale; rerun dry-run and pass "
            f"--confirm {scope.confirmation_token}"
        )
    if args.maintenance_token != scope.maintenance_token:
        raise ResetRefused(
            "apply requires the exact maintenance token and live control-plane proof; "
            "after pinning backend autoscaling and service counts to zero pass "
            f"--maintenance-token {scope.maintenance_token}"
        )
    maintenance_state = await asyncio.to_thread(
        _verify_backend_maintenance_state,
        getattr(args, "ecs_cluster", DEFAULT_ECS_CLUSTER),
        getattr(args, "ecs_service", DEFAULT_ECS_SERVICE),
    )
    if not scope.has_mutations:
        primary = await _primary_identity_snapshot(scope)
        _emit_json(
            {
                "result": "already_clean",
                "before_counts": scope.before_counts,
                "after_counts": scope.before_counts,
                "maintenance": maintenance_state,
                "primary_identities_preserved": primary,
            },
            indent=None,
        )
        return

    manifest = await asyncio.to_thread(
        _export_evidence, scope, args.maintenance_token, maintenance_state
    )
    descriptor = await asyncio.to_thread(
        _persist_cleanup_descriptor,
        scope,
        manifest,
        args.maintenance_token,
        maintenance_state,
    )
    pre_delete_maintenance_state = await asyncio.to_thread(
        _verify_backend_maintenance_state,
        getattr(args, "ecs_cluster", DEFAULT_ECS_CLUSTER),
        getattr(args, "ecs_service", DEFAULT_ECS_SERVICE),
    )
    _emit_json(
        {
            "result": "prepared_for_relational_deletion",
            "scope_sha256": scope.scope_sha256,
            "cleanup_descriptor_sha256": descriptor["sha256"],
            "cleanup_descriptor_key": descriptor["key"],
            "scope_pointer": descriptor["scope_pointer"],
            "maintenance": pre_delete_maintenance_state,
            "resume_external": descriptor["sha256"],
        },
        indent=None,
    )
    try:
        deleted_rows, rechecked = await _delete_relational(scope)
    except Exception as exc:
        try:
            failure = await asyncio.to_thread(
                _persist_cleanup_receipt,
                scope,
                descriptor,
                phase="relational",
                result="failed",
                details={
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                    "external_cleanup_started": False,
                },
            )
        except Exception as receipt_exc:
            raise ResetRefused(
                "relational reset failed before external cleanup and its failure receipt "
                f"could not be persisted: reset={exc}; receipt={receipt_exc}"
            ) from exc
        raise ResetRefused(
            "relational reset failed before external cleanup; "
            f"failure_receipt={failure['key']}: {exc}"
        ) from exc

    external = await _run_external_cleanup(rechecked, descriptor, mode="apply")
    _emit_json(
        {
            "result": "applied",
            "scope_sha256": rechecked.scope_sha256,
            "manifest": manifest,
            "cleanup_descriptor": descriptor,
            "maintenance": pre_delete_maintenance_state,
            "before_counts": rechecked.before_counts,
            "deleted": {
                "dynamo_chat_messages": external["deleted_messages"],
                "s3_artifact_objects": external["deleted_objects"],
                "webhook_secrets_scheduled_for_deletion": external[
                    "deleted_webhook_secrets"
                ],
                "webhook_secret_recovery_window_days": (WEBHOOK_SECRET_RECOVERY_DAYS),
                "relational_rows": deleted_rows,
            },
            "after_counts": external["after_counts"],
            "primary_identities_preserved": external["primary_identities"],
            "receipts": {
                "relational": external["relational_receipt"],
                "completion": external["completion_receipt"],
            },
            "cognito_api_calls": 0,
            "catalog_hard_deletes": 0,
        }
    )


async def main() -> None:
    args = parse_args()
    if args.confirm and not args.apply:
        raise SystemExit("--confirm is valid only with --apply")
    if args.maintenance_token and not (args.apply or args.resume_external):
        raise SystemExit(
            "--maintenance-token is valid only with --apply or --resume-external"
        )
    try:
        await run(args)
    except ResetRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    asyncio.run(main())
