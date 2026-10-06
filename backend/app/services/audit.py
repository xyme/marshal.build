"""Action audit trail (audit-logging spec R1) — Postgres + stdout JSON mirror.

Single capture seam: the middleware in main.py looks every mutating request up
in AUDIT_ROUTE_REGISTRY after the response is produced. Writes are
fire-and-forget on a fresh session — an audit failure must never fail (or slow)
the business operation. Endpoints enrich entries via `set_audit_detail`.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass

from fastapi import Request

from app.core.db import SessionLocal
from app.models import AuditLog

logger = logging.getLogger("marshal.audit")

# Categories per FSD §4.6.1 (model rows live in model_invocations; the admin
# viewer projects them into the same timeline as category "model").
USER, ADMIN, DEPLOYMENT, SECURITY = "user", "admin", "deployment", "security"


@dataclass(frozen=True)
class AuditSpec:
    category: str
    action: str
    resource_type: str | None = None
    id_param: str | None = None  # path param carrying the resource id


# (METHOD, route path template WITHOUT the /api/v1 include-prefix — that is
# what scope['route'].path yields at request time) → AuditSpec.
# Completeness is enforced by tests/test_audit.py::test_registry_covers_mutating_routes.
AUDIT_ROUTE_REGISTRY: dict[tuple[str, str], AuditSpec] = {
    # chat / sessions
    ("POST", "/chat/sessions"): AuditSpec(USER, "session_created", "session"),
    ("PATCH", "/chat/sessions/{session_id}"): AuditSpec(USER, "session_updated", "session", "session_id"),
    ("DELETE", "/chat/sessions/{session_id}"): AuditSpec(USER, "session_deleted", "session", "session_id"),
    ("POST", "/chat/sessions/{session_id}/messages"): AuditSpec(USER, "message_sent", "session", "session_id"),
    ("PUT", "/chat/sessions/{session_id}/substrate"): AuditSpec(USER, "substrate_attached", "session", "session_id"),
    ("DELETE", "/chat/sessions/{session_id}/substrate"): AuditSpec(USER, "substrate_removed", "session", "session_id"),
    ("POST", "/chat/sessions/{session_id}/generate-spec"): AuditSpec(USER, "generation_triggered", "session", "session_id"),
    ("POST", "/chat/sessions/{session_id}/generate-spec/{doc_type}"): AuditSpec(USER, "doc_regenerate_triggered", "session", "session_id"),
    ("POST", "/chat/sessions/{session_id}/spec/save"): AuditSpec(USER, "session_spec_saved", "session", "session_id"),
    # projects
    ("POST", "/projects"): AuditSpec(USER, "project_created", "project"),
    ("POST", "/projects/import"): AuditSpec(USER, "project_imported", "project"),
    ("PUT", "/projects/{project_id}"): AuditSpec(USER, "project_updated", "project", "project_id"),
    ("POST", "/projects/{project_id}/archive"): AuditSpec(USER, "project_archived", "project", "project_id"),
    ("POST", "/projects/{project_id}/restore"): AuditSpec(USER, "project_restored", "project", "project_id"),
    ("DELETE", "/projects/{project_id}"): AuditSpec(USER, "project_deleted", "project", "project_id"),
    # specs
    ("PUT", "/projects/{project_id}/specs/{doc_type}"): AuditSpec(USER, "spec_saved", "spec", "project_id"),
    ("POST", "/projects/{project_id}/specs/{doc_type}/rollback"): AuditSpec(USER, "spec_rolled_back", "spec", "project_id"),
    ("PUT", "/projects/{project_id}/specs/{doc_type}/draft"): AuditSpec(USER, "spec_draft_saved", "spec", "project_id"),
    ("DELETE", "/projects/{project_id}/specs/{doc_type}/draft"): AuditSpec(USER, "spec_draft_discarded", "spec", "project_id"),
    # deployments
    ("POST", "/projects/{project_id}/deploy"): AuditSpec(DEPLOYMENT, "deploy_triggered", "project", "project_id"),
    ("POST", "/projects/{project_id}/deployment/teardown"): AuditSpec(DEPLOYMENT, "teardown_triggered", "project", "project_id"),
    # profile
    ("PUT", "/users/me"): AuditSpec(USER, "profile_updated", "user"),
    ("PUT", "/users/me/demo-experience"): AuditSpec(
        USER, "demo_experience_changed", "user"
    ),
    # admin: templates
    ("POST", "/admin/templates"): AuditSpec(ADMIN, "template_created", "template"),
    ("PUT", "/admin/templates/{template_id}"): AuditSpec(ADMIN, "template_updated", "template", "template_id"),
    ("POST", "/admin/templates/{template_id}/publish"): AuditSpec(ADMIN, "template_published", "template", "template_id"),
    ("POST", "/admin/templates/{template_id}/deprecate"): AuditSpec(ADMIN, "template_deprecated", "template", "template_id"),
    ("DELETE", "/admin/templates/{template_id}"): AuditSpec(ADMIN, "template_deleted", "template", "template_id"),
    # marketplace (public fork + admin curation)
    ("POST", "/marketplace/samples/{sample_id}/fork"): AuditSpec(USER, "sample_forked", "sample", "sample_id"),
    ("POST", "/admin/marketplace/samples"): AuditSpec(ADMIN, "sample_created", "sample"),
    ("PUT", "/admin/marketplace/samples/{sample_id}"): AuditSpec(ADMIN, "sample_updated", "sample", "sample_id"),
    ("POST", "/admin/marketplace/samples/{sample_id}/import-spec"): AuditSpec(ADMIN, "sample_spec_imported", "sample", "sample_id"),
    ("POST", "/admin/marketplace/samples/{sample_id}/publish"): AuditSpec(ADMIN, "sample_published", "sample", "sample_id"),
    ("POST", "/admin/marketplace/samples/{sample_id}/archive"): AuditSpec(ADMIN, "sample_archived", "sample", "sample_id"),
    ("DELETE", "/admin/marketplace/samples/{sample_id}"): AuditSpec(ADMIN, "sample_deleted", "sample", "sample_id"),
    # admin: users
    ("PUT", "/admin/users/{user_id}"): AuditSpec(ADMIN, "user_updated", "user", "user_id"),
    ("POST", "/admin/users/{user_id}/persona-request/decide"): AuditSpec(ADMIN, "persona_request_decided", "user", "user_id"),
    # admin: audit export (an admin action worth its own trail)
    ("POST", "/admin/audit-logs/export"): AuditSpec(ADMIN, "audit_exported", "audit"),
    # admin: governance (S4)
    ("PUT", "/admin/model-controls"): AuditSpec(ADMIN, "model_controls_updated", "settings"),
    ("POST", "/admin/risk-assessments/{assessment_id}/decide"): AuditSpec(ADMIN, "risk_decided", "assessment", "assessment_id"),
    # B17: tuning the rubric is governance-of-the-governance — the audit row
    # carries before/after and whether the policy version bumped.
    ("PUT", "/admin/governance/risk-policy"): AuditSpec(ADMIN, "risk_policy_updated", "risk_policy"),
    # S5: budgets, prefs, alerts
    ("PUT", "/projects/{project_id}/budget"): AuditSpec(USER, "project_budget_updated", "project", "project_id"),
    ("PUT", "/users/me/notifications"): AuditSpec(USER, "notification_prefs_updated", "user"),
    # S15-02 teams: creation/rename/archive and membership are access-control
    # changes, so they belong in the admin trail alongside role changes.
    ("POST", "/admin/teams"): AuditSpec(ADMIN, "team_created", "team"),
    ("PUT", "/admin/teams/{team_id}"): AuditSpec(ADMIN, "team_updated", "team"),
    ("PUT", "/admin/teams/{team_id}/members"): AuditSpec(ADMIN, "team_members_set", "team"),
    ("PUT", "/projects/{project_id}/team"): AuditSpec(USER, "project_team_changed", "project"),
    # S15-03: offboarding is a SECURITY event — permanent access removal, and
    # the row is the evidence that it happened on the day it was claimed.
    ("POST", "/admin/users/{user_id}/offboard"): AuditSpec(SECURITY, "user_offboarded", "user"),
    # S17 custom model endpoints: where user data may EGRESS is an admin
    # decision with compliance surface — every lifecycle step leaves evidence.
    # Disable audits as model_endpoint_updated with enabled:false in the
    # before/after detail (route registry maps routes, not payloads).
    ("POST", "/admin/model-endpoints"): AuditSpec(ADMIN, "model_endpoint_created", "model_endpoint"),
    ("PUT", "/admin/model-endpoints/{slug}"): AuditSpec(ADMIN, "model_endpoint_updated", "model_endpoint", "slug"),
    ("POST", "/admin/model-endpoints/{slug}/probe"): AuditSpec(ADMIN, "model_endpoint_probed", "model_endpoint", "slug"),
    # C0 connector registry (external-import-connectors spec) — probes are
    # audited too: each one is an admin-triggered egress action.
    ("POST", "/admin/integrations/connectors"): AuditSpec(ADMIN, "connector_registered", "connector"),
    ("PUT", "/admin/integrations/connectors/{slug}"): AuditSpec(ADMIN, "connector_updated", "connector", "slug"),
    ("POST", "/admin/integrations/connectors/{slug}/probe"): AuditSpec(ADMIN, "connector_probed", "connector", "slug"),
    ("PUT", "/admin/integrations/connectors/{slug}/status"): AuditSpec(ADMIN, "connector_status_changed", "connector", "slug"),
    ("DELETE", "/admin/integrations/connectors/{slug}"): AuditSpec(ADMIN, "connector_deleted", "connector", "slug"),
    # S14-02 MFA enrollment. Security category: these are authentication-factor
    # changes, and they must be reviewable independently of profile edits.
    ("POST", "/users/me/mfa/start"): AuditSpec(SECURITY, "mfa_enrollment_started", "user"),
    ("POST", "/users/me/mfa/confirm"): AuditSpec(SECURITY, "mfa_enabled", "user"),
    ("DELETE", "/users/me/mfa"): AuditSpec(SECURITY, "mfa_disabled", "user"),
    ("PUT", "/admin/alerts/{alert_id}/acknowledge"): AuditSpec(ADMIN, "alert_acknowledged", "alert", "alert_id"),
    # S6: review workflow
    ("POST", "/risk-assessments/{assessment_id}/decide"): AuditSpec(ADMIN, "risk_decided", "assessment", "assessment_id"),
    ("POST", "/risk-assessments/{assessment_id}/comment"): AuditSpec(USER, "risk_owner_commented", "assessment", "assessment_id"),
    ("PUT", "/admin/reviewer-groups/{group_name}"): AuditSpec(ADMIN, "reviewer_group_updated", "reviewer_group"),
    # guided mode (S4-05) — wizard progress is user activity
    ("POST", "/chat/sessions/{session_id}/guided/answer"): AuditSpec(USER, "guided_step_answered", "session", "session_id"),
    ("POST", "/chat/sessions/{session_id}/guided/clarify"): AuditSpec(USER, "guided_clarify_answered", "session", "session_id"),
    ("POST", "/chat/sessions/{session_id}/guided/proceed"): AuditSpec(USER, "guided_proceeded", "session", "session_id"),
    ("POST", "/chat/sessions/{session_id}/guided/switch-freeform"): AuditSpec(USER, "guided_switched_freeform", "session", "session_id"),
    # S7: collaboration (membership, transfer, comments)
    ("POST", "/projects/{project_id}/members"): AuditSpec(USER, "member_added", "project", "project_id"),
    ("PATCH", "/projects/{project_id}/members/{user_id}"): AuditSpec(USER, "member_role_changed", "project", "project_id"),
    ("DELETE", "/projects/{project_id}/members/{user_id}"): AuditSpec(USER, "member_removed", "project", "project_id"),
    ("POST", "/projects/{project_id}/transfer-ownership"): AuditSpec(USER, "ownership_transferred", "project", "project_id"),
    ("POST", "/projects/{project_id}/comments"): AuditSpec(USER, "comment_added", "comment", "project_id"),
    ("POST", "/comments/{comment_id}/reply"): AuditSpec(USER, "comment_replied", "comment", "comment_id"),
    ("POST", "/comments/{comment_id}/resolve"): AuditSpec(USER, "comment_resolved", "comment", "comment_id"),
    ("POST", "/comments/{comment_id}/unresolve"): AuditSpec(USER, "comment_unresolved", "comment", "comment_id"),
    ("DELETE", "/comments/{comment_id}"): AuditSpec(USER, "comment_deleted", "comment", "comment_id"),
    # S7: marketplace submissions
    ("POST", "/projects/{project_id}/submit-to-marketplace"): AuditSpec(USER, "submission_created", "sample", "project_id"),
    ("POST", "/marketplace/submissions/{sample_id}/withdraw"): AuditSpec(USER, "submission_withdrawn", "sample", "sample_id"),
    ("POST", "/admin/marketplace/submissions/{sample_id}/approve"): AuditSpec(ADMIN, "submission_approved", "sample", "sample_id"),
    ("POST", "/admin/marketplace/submissions/{sample_id}/reject"): AuditSpec(ADMIN, "submission_rejected", "sample", "sample_id"),
    # S8: codegen builds
    ("POST", "/projects/{project_id}/builds"): AuditSpec(USER, "build_started", "build", "project_id"),
    ("POST", "/builds/{build_id}/cancel"): AuditSpec(USER, "build_cancelled", "build", "build_id"),
    # S11: deployment lifecycle
    ("POST", "/projects/{project_id}/deployment/extend"): AuditSpec(USER, "deployment_extended", "deployment", "project_id"),
    ("POST", "/projects/{project_id}/deployment/preview"): AuditSpec(DEPLOYMENT, "deployment_preview", "project", "project_id"),
    # B13: reading a live credential is a SECURITY event (integration-wave R1.4)
    ("POST", "/projects/{project_id}/deployment/api-key/reveal"): AuditSpec(SECURITY, "deployment_api_key_revealed", "deployment", "project_id"),
    # B20 R2: minting Testbed credentials is a SECURITY event — a person gains
    # direct (limited) access to a leased account. Detail carries session name
    # + expiry; the secret itself is never stored or logged.
    ("POST", "/projects/{project_id}/deployment/testbed-credentials"): AuditSpec(SECURITY, "testbed_credentials_issued", "deployment", "project_id"),
    # B9: credential lifecycle is SECURITY (integration-wave R2)
    ("POST", "/admin/service-accounts"): AuditSpec(SECURITY, "service_account_created", "user"),
    ("PUT", "/admin/service-accounts/{account_id}/status"): AuditSpec(SECURITY, "service_account_status_changed", "user", "account_id"),
    ("POST", "/admin/service-accounts/{account_id}/tokens"): AuditSpec(SECURITY, "service_token_minted", "user", "account_id"),
    ("DELETE", "/admin/service-accounts/{account_id}/tokens/{token_id}"): AuditSpec(SECURITY, "service_token_revoked", "user", "account_id"),
    # B10/B11: integration egress config is ADMIN surface (integration-wave R3.4/R4.3)
    ("POST", "/admin/integrations/webhooks"): AuditSpec(ADMIN, "webhook_created", "webhook"),
    ("PUT", "/admin/integrations/webhooks/{endpoint_id}"): AuditSpec(ADMIN, "webhook_updated", "webhook", "endpoint_id"),
    ("DELETE", "/admin/integrations/webhooks/{endpoint_id}"): AuditSpec(ADMIN, "webhook_deleted", "webhook", "endpoint_id"),
    ("POST", "/admin/integrations/webhooks/{endpoint_id}/test"): AuditSpec(ADMIN, "webhook_tested", "webhook", "endpoint_id"),
    ("PUT", "/admin/integrations/chat-ops"): AuditSpec(ADMIN, "chat_ops_updated", "settings"),
    ("POST", "/admin/integrations/chat-ops/test"): AuditSpec(ADMIN, "chat_ops_tested", "settings"),
}

# Mutating routes deliberately NOT audited (must be justified here):
AUDIT_EXEMPT: set[tuple[str, str]] = {
    # Pure per-user UI state; auditing every read-marker would be noise
    ("POST", "/notifications/{notification_id}/read"),
    ("POST", "/notifications/read-all"),
    # Presence heartbeats are pure telemetry at 30s cadence — auditing them
    # would flood the trail with zero governance value (collaboration R6.4)
    ("POST", "/projects/{project_id}/presence"),
}


def set_audit_detail(request: Request, **detail) -> None:
    """Endpoint-side enrichment (before/after values, doc_type, version…)."""
    existing = getattr(request.state, "audit_detail", None) or {}
    existing.update(detail)
    request.state.audit_detail = existing


async def write_entry(
    *,
    actor_id: uuid.UUID | None,
    category: str,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    project_id: uuid.UUID | None = None,
    detail: dict | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    http_status: int | None = None,
) -> None:
    """Persist one entry + mirror to stdout. Never raises."""
    entry = dict(
        actor_id=actor_id,
        category=category,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        project_id=project_id,
        detail=detail or {},
        source_ip=source_ip,
        user_agent=(user_agent or "")[:256] or None,
        request_id=request_id,
        http_status=http_status,
    )
    # Independent copy to CloudWatch via the awslogs driver (R1.5).
    logger.info(
        "audit %s",
        json.dumps({**entry, "actor_id": str(actor_id) if actor_id else None,
                    "project_id": str(project_id) if project_id else None},
                   default=str, separators=(",", ":")),
    )
    try:
        async with SessionLocal() as session:
            session.add(AuditLog(**entry))
            await session.commit()
    except Exception:  # noqa: BLE001 — availability over completeness (R1.3)
        logger.exception("audit write failed for action=%s", action)


def emit(coro) -> None:
    """Fire-and-forget scheduling that survives missing loops (tests/scripts)."""
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # no running loop — degrade to synchronous best effort
        asyncio.run(coro)


def _extract_uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


async def capture_request(request: Request, status_code: int) -> None:
    """Middleware hook: log registered mutations + security events (R1.1, R1.4)."""
    route = request.scope.get("route")
    path_template = getattr(route, "path", request.url.path)
    method = request.method
    if method in ("GET", "HEAD", "OPTIONS") and status_code not in (401, 403):
        return
    if not request.url.path.startswith("/api/"):
        return

    user = getattr(request.state, "user", None)
    detail = getattr(request.state, "audit_detail", None) or {}
    common = dict(
        actor_id=user.id if user else None,
        source_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=request.headers.get("x-amzn-trace-id") or request.headers.get("x-request-id"),
        http_status=status_code,
    )

    # Security events first (FSD §4.6.1 security category).
    if status_code in (401, 403):
        action = "auth_failed" if status_code == 401 else "permission_denied"
        await write_entry(
            category=SECURITY,
            action=action,
            detail={"method": method, "path": request.url.path, **detail},
            **common,
        )
        return

    spec = AUDIT_ROUTE_REGISTRY.get((method, path_template))
    if spec is None or status_code >= 500:
        return
    path_params = request.path_params or {}
    resource_id = str(path_params.get(spec.id_param)) if spec.id_param else detail.pop("resource_id", None)
    project_id = _extract_uuid(str(path_params.get("project_id"))) if "project_id" in path_params else None
    project_id = project_id or _extract_uuid(detail.pop("project_id", None))

    if status_code == 422:
        # Guardrail/validation rejection of a governed action (spec R1.1 security row).
        await write_entry(
            category=SECURITY,
            action=f"{spec.action}_rejected",
            resource_type=spec.resource_type,
            resource_id=resource_id,
            project_id=project_id,
            detail={"method": method, "path": request.url.path, **detail},
            **common,
        )
        return

    await write_entry(
        category=spec.category,
        action=spec.action,
        resource_type=spec.resource_type,
        resource_id=resource_id,
        project_id=project_id,
        detail=detail,
        **common,
    )
