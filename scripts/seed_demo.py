"""Showcase demo data (owner request, 30 Jul 2026).

WHY THIS EXISTS — and the policy it amends: the 25 Jul 2026 live-site content
policy was "catalog content only, no synthetic usage data". A showcase needs
populated dashboards, so the owner authorised an exception. Two guardrails keep
that honest:

  1. EVERY row is created with a DETERMINISTIC uuid5 id derived from
     DEMO_NAMESPACE. Re-running is idempotent (no duplicates), and `--purge`
     removes exactly what was seeded and nothing else.
  2. Seeded users are the existing @marshal.demo accounts only. No real user's
     data is touched, and nothing is written outside the ids this script owns.

Usage (local, against a reachable DB):
    cd backend && uv run python ../scripts/seed_demo.py [--purge] [--tidy]

Usage (cloud — the DB is private, so run it as a one-off ECS task):
    aws ecs run-task --cluster marshal --task-definition <backend-td> \
      --launch-type FARGATE --network-configuration '...' \
      --overrides '{"containerOverrides":[{"name":"backend",
        "command":["python","/opt/marshal/scripts/seed_demo.py"]}]}'

Flags:
    --purge  delete every demo row this script owns, then exit
    --tidy   ALSO hide drill residue so the showcase screens are clean: probe
             projects are SOFT-deleted (reversible — the same thing the app's
             own delete endpoint does) and probe chat sessions are removed via
             the app's delete path. Explicit name patterns only (TIDY_* below);
             a real project can never be matched by accident.
"""

import asyncio
import hashlib
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, "/app")

from sqlalchemy import delete, select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AiRiskAssessment,
    AuditLog,
    ChatSession,
    CodegenArtifact,
    CodegenBuild,
    Deployment,
    Lease,
    ModelInvocation,
    Notification,
    Project,
    ProjectMember,
    Spec,
    Team,
    TeamMember,
    UsageEvent,
    User,
)

# Namespace that makes every seeded id reproducible. Derived from a stable URL
# so the constant is self-documenting; bump the /v1 to re-key a future showcase.
DEMO_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://marshal.build/demo-seed/v1")


def did(kind: str, key: str) -> uuid.UUID:
    """Deterministic id for a demo row."""
    return uuid.uuid5(DEMO_NAMESPACE, f"{kind}:{key}")


NOW = datetime.now(UTC)


def ago(days: float = 0, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours)


# --------------------------------------------------------------- demo content

TEAMS = [
    ("retail-banking", "Retail Banking", "Customer-facing assistants and self-service journeys"),
    ("claims-ops", "Claims Operations", "Document-heavy claims intake and triage automation"),
    ("data-platform", "Data Platform", "Shared retrieval, governance and evaluation tooling"),
]

# (key, name, description, owner_email, team_key, status, origin, age_days, budget)
PROJECTS = [
    ("mortgage-advisor", "Mortgage Pre-Qualification Advisor",
     "Guides applicants through eligibility questions and explains the decision factors in plain language.",
     "power@marshal.demo", "retail-banking", "deployed", "template", 26, 400),
    ("branch-copilot", "Branch Staff Copilot",
     "Answers product and policy questions for branch staff with citations to the current handbook.",
     "power@marshal.demo", "retail-banking", "deployed", "marketplace_fork", 19, None),
    ("claims-intake", "Claims Intake Classifier",
     "Reads submitted claim documents, extracts the key fields and routes by claim type and severity.",
     "business@marshal.demo", "claims-ops", "building", "template", 12, 250),
    ("fnol-summariser", "First-Notice-of-Loss Summariser",
     "Turns call transcripts into structured loss reports for adjusters, flagging missing information.",
     "business@marshal.demo", "claims-ops", "spec_complete", "scratch", 8, None),
    ("kyc-review-assist", "KYC Review Assistant",
     "Drafts reviewer notes for periodic KYC refresh cases and cites the evidence it used.",
     "admin@marshal.demo", "data-platform", "spec_complete", "template", 5, 300),
    ("policy-qa", "Policy Q&A Service",
     "Shared retrieval service other teams call for grounded answers over internal policy documents.",
     "admin@marshal.demo", "data-platform", "deployed", "scratch", 33, 500),
    ("statement-insights", "Statement Insights Prototype",
     "Explores spending-pattern summaries for the mobile app. Parked pending data-residency review.",
     "power@marshal.demo", None, "inactive", "scratch", 41, None),
]

REQUIREMENTS_MD = """# Requirements — {name}

## Introduction
{description}

## Requirement 1: Grounded answers
**User story:** As a {persona}, I want answers that cite their source, so that I
can verify them before acting.

#### Acceptance criteria
1. WHEN the assistant answers THEN it SHALL cite the source document and section.
2. IF confidence is below the configured threshold THEN it SHALL say so and offer
   a hand-off instead of guessing.
3. WHEN no supporting document is found THEN it SHALL refuse rather than answer
   from general knowledge.

## Requirement 2: Auditability
**User story:** As a compliance reviewer, I want every interaction recorded, so
that I can evidence how a decision was reached.

#### Acceptance criteria
1. WHEN any answer is produced THEN the prompt, response and sources SHALL be
   retained for the audit window.
2. WHEN a reviewer exports a case THEN the export SHALL include the full trail.

## Requirement 3: Cost control
**User story:** As a platform owner, I want per-team spend caps, so that a runaway
workload cannot consume the budget.

#### Acceptance criteria
1. WHEN a team reaches 90% of its monthly cap THEN the owner SHALL be alerted.
2. WHEN the cap is reached THEN further model calls SHALL be refused with a clear
   message.

## Non-functional requirements
- p95 response under 3 seconds for retrieval-backed answers.
- No customer identifiers in prompts sent to the model.
- Region pinned to the approved deployment region.
"""

DESIGN_MD = """# Design — {name}

## Overview
{description}

The design keeps the retrieval and generation seams separate so either can be
replaced without touching the other, and so evaluation can target them
independently.

## Architecture

```mermaid
graph TD
    U[User] --> A[API Gateway]
    A --> H[Handler / Lambda]
    H --> R[Retriever]
    R --> V[(Vector index)]
    H --> M[Bedrock model]
    H --> L[(Audit log)]
    H --> S[(Session store)]
```

## Components

| Component | Responsibility | Notes |
|-----------|----------------|-------|
| Handler | Request validation, orchestration, refusal rules | Stateless |
| Retriever | Top-k passages + score threshold | Threshold is configuration |
| Model seam | Prompt assembly and generation | Model id is configuration |
| Audit log | Append-only record of prompt/response/sources | Retention per policy |

## Data model
- `documents(id, source_uri, section, checksum, ingested_at)`
- `interactions(id, user_id, question, answer, sources[], confidence, created_at)`

## Error handling
Retrieval failure degrades to an explicit "cannot answer right now" rather than
an ungrounded answer. Model throttling retries with backoff, then surfaces a
retry-after to the caller.

## Testing strategy
Golden-question set with expected citations; refusal cases asserted explicitly;
latency budget checked under representative concurrency.
"""

TASKS_MD = """# Implementation Plan — {name}

- [ ] 1. Project scaffolding and configuration
  - Create the service skeleton, configuration loading and health endpoint
  - _Requirements: 1, 3_

- [ ] 2. Document ingestion
- [ ] 2.1 Ingest and chunk source documents
  - Checksum-based skip for unchanged documents
  - _Requirements: 1_
- [ ] 2.2 Build the vector index and score threshold
  - _Requirements: 1_

- [ ] 3. Retrieval-backed answering
- [ ] 3.1 Implement the retriever seam with top-k and threshold
  - _Requirements: 1_
- [ ] 3.2 Implement the refusal path for low confidence and no-match
  - _Requirements: 1_

- [ ] 4. Audit trail
  - Persist prompt, response, sources and confidence for every interaction
  - _Requirements: 2_

- [ ] 5. Cost controls
  - Per-team monthly cap with alerting at 90% and refusal at 100%
  - _Requirements: 3_

- [ ] 6. Evaluation harness
  - Golden-question regression run with citation assertions
  - _Requirements: 1, 2_
"""

# Colleagues so the org looks like an org: these are DATABASE rows only — they
# have no Cognito identity and cannot sign in. They exist so the funnel, team
# rollups, chargeback and access review show realistic spread instead of three
# accounts doing everything. Purged with everything else.
# (key, email, name, role, persona, status, joined_days_ago)
ROSTER = [
    ("nadia", "nadia.okafor@marshal.demo", "Nadia Okafor", "power", "power", "active", 58),
    ("tom", "tom.hillier@marshal.demo", "Tom Hillier", "business", "business", "active", 55),
    ("mei", "mei.zhang@marshal.demo", "Mei Zhang", "power", "power", "active", 51),
    ("raj", "raj.patel@marshal.demo", "Raj Patel", "business", "business", "active", 47),
    ("sofia", "sofia.marino@marshal.demo", "Sofia Marino", "power", "power", "active", 44),
    ("dan", "dan.mercer@marshal.demo", "Dan Mercer", "business", "business", "active", 39),
    ("aisha", "aisha.bello@marshal.demo", "Aisha Bello", "power", "power", "active", 33),
    ("henrik", "henrik.dahl@marshal.demo", "Henrik Dahl", "business", "business", "active", 28),
    ("lucia", "lucia.ferrer@marshal.demo", "Lucia Ferrer", "business", "business", "active", 24),
    ("omar", "omar.haddad@marshal.demo", "Omar Haddad", "power", "power", "active", 17),
    ("grace", "grace.lin@marshal.demo", "Grace Lin", "business", "business", "active", 11),
    # Left the company — shows the S15-03 offboarding/access-review story
    ("peter", "peter.novak@marshal.demo", "Peter Novak", "business", "business", "suspended", 62),
]

# Funnel shape: (event, distinct users reaching this step, events per user).
# Users are taken in roster order so the SAME people progress deeper, which is
# what a real adoption funnel looks like.
FUNNEL_SHAPE = [
    ("sign_in", 14, 6),
    ("chat_message", 11, 7),
    ("spec_saved", 8, 3),
    ("build_started", 5, 3),
    ("deploy_started", 3, 2),
    ("teardown", 2, 2),
]

# Drill residue patterns, measured against the live database on 30 Jul 2026:
# 46 of 64 projects and 558 of 562 chat sessions were probe artefacts from the
# S6-S14 live drills and repeated ui-smoke runs. Prefixes are explicit (never
# a blanket wildcard) so a real project can never be caught by accident.
TIDY_PROJECT_PREFIXES = (
    "S6 E2E", "S7 Probe", "S8 Probe", "S11 Drill", "S12 Beta E2E",
    "Risk E2E", "Audit E2E",
)
TIDY_SESSION_TITLES = (
    "Reply with one word: ping",
    "Guided session",
    "rate drill 1",
)
# Sessions whose titles start with these are drill probes too
TIDY_SESSION_PREFIXES = ("ping 1 —", "ping 1 -")


# ------------------------------------------------------------------ helpers


async def _users(db) -> dict[str, User]:
    rows = (await db.execute(select(User).where(User.email.like("%@marshal.demo")))).scalars().all()
    return {u.email: u for u in rows}


async def _upsert(db, model, row_id, **fields):
    """Create the row if the deterministic id is absent; otherwise update it."""
    existing = await db.get(model, row_id)
    if existing is None:
        obj = model(id=row_id, **fields)
        db.add(obj)
        return obj, True
    for key, value in fields.items():
        setattr(existing, key, value)
    return existing, False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------- seed


async def seed() -> None:
    async with SessionLocal() as db:
        users = await _users(db)
        missing = {"power@marshal.demo", "business@marshal.demo", "admin@marshal.demo"} - set(users)
        if missing:
            sys.exit(f"Demo users missing: {sorted(missing)} — run scripts/seed_users.py first.")
        admin = users["admin@marshal.demo"]
        created = updated = 0

        # ---- colleague roster (DB rows only — no Cognito identity, cannot sign in)
        for key, email, name, role, persona, status, joined in ROSTER:
            obj, is_new = await _upsert(
                db, User, did("user", key),
                cognito_sub=f"demo-seed-{key}",  # no such Cognito user exists
                email=email, name=name, role=role, role_source="admin", persona=persona,
                status=status, onboarding_completed=True,
                created_at=ago(days=joined), updated_at=ago(days=joined),
            )
            created += is_new
            updated += not is_new
            users[email] = obj
        await db.commit()

        # ---- teams + membership
        team_ids: dict[str, uuid.UUID] = {}
        for key, name, description in TEAMS:
            tid = did("team", key)
            team_ids[key] = tid
            _, is_new = await _upsert(
                db, Team, tid, name=name, description=description, status="active",
                created_by=admin.id, created_at=ago(days=45), updated_at=ago(days=45),
            )
            created += is_new
            updated += not is_new
        await db.commit()

        memberships = [
            ("retail-banking", "power@marshal.demo", "lead"),
            ("retail-banking", "business@marshal.demo", "member"),
            ("claims-ops", "business@marshal.demo", "lead"),
            ("claims-ops", "power@marshal.demo", "member"),
            ("data-platform", "admin@marshal.demo", "lead"),
            ("data-platform", "power@marshal.demo", "member"),
        ]
        # Spread the roster across the three teams so membership counts look real
        roster_teams = [
            ("retail-banking", ["nadia", "tom", "mei", "grace"]),
            ("claims-ops", ["raj", "sofia", "dan", "lucia"]),
            ("data-platform", ["aisha", "henrik", "omar"]),
        ]
        for team_key, member_keys in roster_teams:
            for position, member_key in enumerate(member_keys):
                email = next(e for k, e, *_ in ROSTER if k == member_key)
                memberships.append((team_key, email, "lead" if position == 0 else "member"))

        for team_key, email, role in memberships:
            if email not in users:
                continue
            await _upsert(
                db, TeamMember, did("member", f"{team_key}:{email}"),
                team_id=team_ids[team_key], user_id=users[email].id, role=role,
                added_by=admin.id, created_at=ago(days=44),
            )
        await db.commit()

        # ---- projects + specs
        project_ids: dict[str, uuid.UUID] = {}
        for key, name, description, owner_email, team_key, status, origin, age, budget in PROJECTS:
            pid = did("project", key)
            project_ids[key] = pid
            owner = users[owner_email]
            _, is_new = await _upsert(
                db, Project, pid, user_id=owner.id, name=name, description=description,
                status=status, origin=origin, team_id=team_ids.get(team_key) if team_key else None,
                budget_override_usd=budget, created_at=ago(days=age),
                updated_at=ago(days=max(0, age - 4)),
                archived_at=None, deleted_at=None,
            )
            created += is_new
            updated += not is_new

            persona = "business analyst" if owner_email.startswith("business") else "platform engineer"
            docs = {
                "requirements": REQUIREMENTS_MD.format(name=name, description=description, persona=persona),
                "design": DESIGN_MD.format(name=name, description=description),
                "tasks": TASKS_MD.format(name=name),
            }
            # Spec-complete and beyond carry all three documents; earlier
            # projects carry only what they would plausibly have.
            include = ["requirements", "design", "tasks"]
            if status == "draft":
                include = ["requirements"]
            for doc_type in include:
                await _upsert(
                    db, Spec, did("spec", f"{key}:{doc_type}"),
                    project_id=pid, session_id=None, version=1, type=doc_type,
                    content=docs[doc_type], model_id="us.anthropic.claude-sonnet-5",
                    origin="generated", created_by=owner.id, created_at=ago(days=max(0, age - 1)),
                )
        await db.commit()

        # ---- a shared project (collaboration surface)
        await _upsert(
            db, ProjectMember, did("share", "policy-qa:power"),
            project_id=project_ids["policy-qa"], user_id=users["power@marshal.demo"].id,
            role="editor", added_by=admin.id, created_at=ago(days=20), updated_at=ago(days=20),
        )

        # ---- risk assessments: one approved, one awaiting review (queue looks alive)
        approved_content = _sha("mortgage-advisor-v1")
        await _upsert(
            db, AiRiskAssessment, did("risk", "mortgage-advisor"),
            project_id=project_ids["mortgage-advisor"], content_hash=approved_content,
            rubric_version=1, score=38, level="medium",
            factors={
                "data_sensitivity": {"score": 3, "weight": 0.3, "rationale": "Applicant data stays out of prompts; only eligibility attributes are sent."},
                "autonomy": {"score": 2, "weight": 0.2, "rationale": "Advisory only — no automated decisioning."},
                "model_capability": {"score": 4, "weight": 0.2, "rationale": "General-purpose frontier model."},
                "deployment_scope": {"score": 3, "weight": 0.2, "rationale": "Internal pilot with a named user group."},
                "regulatory_exposure": {"score": 4, "weight": 0.1, "rationale": "Consumer-credit adjacent; requires explainability."},
            },
            status="scored", decision="approved", decided_by=admin.id, decided_at=ago(days=21),
            notes="Approved for the internal pilot. Re-review before any customer-facing rollout.",
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0", created_at=ago(days=22),
            assigned_group="managers", routed_at=ago(days=22),
        )
        await _upsert(
            db, AiRiskAssessment, did("risk", "kyc-review-assist"),
            project_id=project_ids["kyc-review-assist"], content_hash=_sha("kyc-review-assist-v1"),
            rubric_version=1, score=71, level="high",
            factors={
                "data_sensitivity": {"score": 5, "weight": 0.3, "rationale": "Handles identity documents and sanctions context."},
                "autonomy": {"score": 4, "weight": 0.2, "rationale": "Drafts reviewer conclusions that a human signs off."},
                "model_capability": {"score": 4, "weight": 0.2, "rationale": "General-purpose frontier model."},
                "deployment_scope": {"score": 3, "weight": 0.2, "rationale": "Compliance team only."},
                "regulatory_exposure": {"score": 5, "weight": 0.1, "rationale": "Directly in scope for AML/KYC obligations."},
            },
            status="scored", decision="pending", model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            created_at=ago(days=2), assigned_group="governance_board", routed_at=ago(days=2),
        )
        await db.commit()

        # ---- builds + artifacts + deployments for the deployed projects
        for key, age in (("mortgage-advisor", 6), ("branch-copilot", 4), ("policy-qa", 9)):
            pid = project_ids[key]
            owner_email = next(p[3] for p in PROJECTS if p[0] == key)
            bid = did("build", key)
            content_hash = _sha(f"{key}-artifacts")
            await _upsert(
                db, CodegenBuild, bid, project_id=pid, status="ready", provider="internal",
                phase_detail="Validation passed", artifact_profile="inline-cfn",
                spec_snapshot={}, spec_hash=_sha(f"{key}-spec"), content_hash=content_hash,
                manifest={
                    "app_name": key,
                    "engine": {"name": "internal", "version": "1"},
                    "smoke": {
                        "at": ago(days=age).isoformat(),
                        "passed": True,
                        "probed": [
                            {"path": "/", "status": 200, "ok": True},
                            {"path": "/ask", "status": 200, "ok": True},
                        ],
                        "skipped_routes": ["POST /ingest"],
                    },
                },
                created_by=users[owner_email].id, created_at=ago(days=age),
                started_at=ago(days=age), finished_at=ago(days=age, hours=-0.2),
            )
            await _upsert(
                db, CodegenArtifact, did("artifact", f"{key}:template"),
                build_id=bid, path="template.json",
                content='{"AWSTemplateFormatVersion":"2010-09-09","Resources":{}}',
                s3_key=None, content_hash=_sha(f"{key}-template"), size_bytes=64, language="json",
            )
            await _upsert(
                db, CodegenArtifact, did("artifact", f"{key}:readme"),
                build_id=bid, path="README.md", content=f"# {key}\n\nGenerated by marshal.\n",
                s3_key=None, content_hash=_sha(f"{key}-readme"), size_bytes=48, language="markdown",
            )
        await db.commit()

        # One torn-down Enclave so deployment history tells a story. The seeder
        # deliberately no longer fakes an ACTIVE deployment (5 Aug 2026): the
        # platform treats seeded rows as real — the health prober marked the
        # fake degraded and the expiry sweeper tried to reap it, so demos met a
        # FAILED deployment with a dead URL. The live showcase deployment is
        # created for real through the product instead: scripts/seed-live-demo.mjs
        # builds and deploys mortgage-advisor into a genuine Enclave.
        stale_fake_ids = [did("deployment", "mortgage-advisor"), did("lease", "mortgage-advisor")]
        await db.execute(delete(Deployment).where(Deployment.id == stale_fake_ids[0]))
        await db.execute(delete(Lease).where(Lease.id == stale_fake_ids[1]))
        for key, status, age, health in (
            ("policy-qa", "torn_down", 9, "unknown"),
        ):
            pid = project_ids[key]
            owner_email = next(p[3] for p in PROJECTS if p[0] == key)
            lease_id = did("lease", key)
            await _upsert(
                db, Lease, lease_id, provider="isb", external_lease_id=f"demo-lease-{key}",
                aws_account_id="123456789012", status="active" if status == "active" else "terminated",
                project_id=pid, user_id=users[owner_email].id, budget_usd=200,
                requested_at=ago(days=age), activated_at=ago(days=age),
                terminated_at=None if status == "active" else ago(days=age - 2),
            )
            await _upsert(
                db, Deployment, did("deployment", key), project_id=pid,
                user_id=users[owner_email].id, lease_id=lease_id, build_id=did("build", key),
                stack_name=f"marshal-{key[:12]}", stack_id=None, status=status,
                health=health, last_health_at=ago(hours=1) if status == "active" else None,
                app_url="https://demo-api.execute-api.us-east-1.amazonaws.com" if status == "active" else None,
                timeline=[
                    {"phase": "leasing", "at": ago(days=age).isoformat(), "detail": "Enclave lease activated"},
                    {"phase": "deploying", "at": ago(days=age).isoformat(), "detail": "Creating stack"},
                    {"phase": "active", "at": ago(days=age).isoformat(), "detail": "Healthy"},
                ] + ([{"phase": "torn_down", "at": ago(days=age - 2).isoformat(), "detail": "Torn down after review"}] if status == "torn_down" else []),
                resources=[], deployed_at=ago(days=age),
                expires_at=ago(days=-2) if status == "active" else None,
                torn_down_at=None if status == "active" else ago(days=age - 2),
                created_at=ago(days=age), updated_at=ago(days=age),
            )
        await db.commit()

        # ---- model spend over 30 days (cost dashboard + chargeback)
        spend_plan = [
            ("mortgage-advisor", "power@marshal.demo", "requirements", 0.42),
            ("mortgage-advisor", "power@marshal.demo", "design", 0.61),
            ("branch-copilot", "power@marshal.demo", "chat", 0.18),
            ("claims-intake", "business@marshal.demo", "requirements", 0.35),
            ("claims-intake", "business@marshal.demo", "codegen", 1.24),
            ("fnol-summariser", "business@marshal.demo", "chat", 0.22),
            ("kyc-review-assist", "admin@marshal.demo", "design", 0.58),
            ("policy-qa", "admin@marshal.demo", "codegen", 1.05),
            ("policy-qa", "power@marshal.demo", "chat", 0.31),
            ("statement-insights", "power@marshal.demo", "requirements", 0.27),
            # Colleagues working on their teams' projects — makes the chargeback
            # table read as a real cost-allocation report rather than 3 rows.
            ("mortgage-advisor", "nadia.okafor@marshal.demo", "chat", 0.24),
            ("mortgage-advisor", "tom.hillier@marshal.demo", "requirements", 0.33),
            ("branch-copilot", "mei.zhang@marshal.demo", "design", 0.47),
            ("claims-intake", "raj.patel@marshal.demo", "chat", 0.19),
            ("claims-intake", "sofia.marino@marshal.demo", "codegen", 0.88),
            ("fnol-summariser", "dan.mercer@marshal.demo", "requirements", 0.29),
            ("kyc-review-assist", "aisha.bello@marshal.demo", "design", 0.52),
            ("policy-qa", "henrik.dahl@marshal.demo", "chat", 0.21),
            ("policy-qa", "omar.haddad@marshal.demo", "codegen", 0.74),
            ("branch-copilot", "grace.lin@marshal.demo", "chat", 0.16),
        ]
        for index, (project_key, email, purpose, cost) in enumerate(spend_plan):
            for repeat in range(3):
                # Spread every entry INSIDE the 30-day window. An earlier linear
                # formula ran past day 29 and silently dropped the tail of the
                # plan, which showed up as a thin chargeback table.
                day = 1 + ((index * 3 + repeat * 9) % 28)
                prompt = f"[demo] {purpose} generation for {project_key} #{repeat}"
                await _upsert(
                    db, ModelInvocation, did("invocation", f"{project_key}:{email}:{purpose}:{repeat}"),
                    created_at=ago(days=day),
                    user_id=users[email].id, project_id=project_ids[project_key],
                    session_id=None, generation_id=None, purpose=purpose, source="platform",
                    model_id="us.anthropic.claude-sonnet-5" if purpose != "classification" else "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    prompt_text=prompt, prompt_sha256=_sha(prompt),
                    response_text="[demo] generated content", response_sha256=_sha("demo-response"),
                    input_tokens=1800 + repeat * 250, output_tokens=900 + repeat * 180,
                    stop_reason="end_turn", latency_ms=2400 + repeat * 300,
                    cost_usd=round(cost * (1 + repeat * 0.15), 6), success=True, error_class=None,
                )
        await db.commit()

        # ---- usage events over 30 days (funnel, DAU, per-team rollups)
        # Funnel participants: the three sign-in accounts first (so a live demo
        # of those users shows up in the numbers), then the roster.
        participants = [
            users["power@marshal.demo"], users["business@marshal.demo"], users["admin@marshal.demo"],
            *[users[email] for _, email, *_ in ROSTER if email in users],
        ]
        # People work mostly inside their OWN team, so per-team rollups differ
        # instead of every team showing the same headcount.
        team_projects: dict[str, list[str]] = {}
        for project_key, *_rest in ((p[0], p) for p in PROJECTS):
            tkey = next(p[4] for p in PROJECTS if p[0] == project_key)
            if tkey:
                team_projects.setdefault(tkey, []).append(project_key)
        user_projects: dict[str, list[str]] = {}
        for team_key, email, _role in memberships:
            if team_key in team_projects:
                user_projects.setdefault(email, []).extend(team_projects[team_key])
        all_project_keys = [p[0] for p in PROJECTS]

        for event, user_count, per_user in FUNNEL_SHAPE:
            for index, user in enumerate(participants[:user_count]):
                choices = user_projects.get(user.email) or all_project_keys
                for repeat in range(per_user):
                    if event == "sign_in":
                        # one row per user per UTC day, per the analytics contract
                        day = (index + repeat * 4) % 30
                        key = f"{event}:{user.email}:{day}"
                        project_key = None
                    else:
                        day = (index * 2 + repeat * 5) % 30
                        project_key = choices[(index + repeat) % len(choices)]
                        key = f"{event}:{user.email}:{repeat}"
                    pid = project_ids[project_key] if project_key else None
                    team_id = None
                    if project_key:
                        team_key = next(p[4] for p in PROJECTS if p[0] == project_key)
                        team_id = team_ids.get(team_key) if team_key else None
                    await _upsert(
                        db, UsageEvent, did("event", key), user_id=user.id, event=event,
                        project_id=pid, team_id=team_id, detail={"demo": True},
                        dedupe_key=f"demo:{key}", created_at=ago(days=day, hours=(index % 9) + 1),
                    )
        await db.commit()

        # ---- notifications (bell has something real to show)
        notes = [
            ("n1", "power@marshal.demo", "deploy_succeeded", "Deployment live",
             "Mortgage Pre-Qualification Advisor is live in its Enclave.", 6),
            ("n2", "admin@marshal.demo", "risk_review_requested", "Review requested",
             "KYC Review Assistant scored HIGH and is awaiting a governance decision.", 2),
            ("n3", "business@marshal.demo", "build_ready", "Build ready",
             "Claims Intake Classifier finished building and passed validation.", 3),
            ("n4", "power@marshal.demo", "cost_threshold", "75% of monthly cap",
             "Retail Banking has used 75% of its monthly model budget.", 4),
        ]
        for key, email, ntype, title, body, age in notes:
            await _upsert(
                db, Notification, did("notification", key), user_id=users[email].id,
                type=ntype, title=title, body=body, link="/projects",
                read_at=None if age < 4 else ago(days=age - 1),
                created_at=ago(days=age), email_status="skipped", dedupe_key=f"demo:{key}",
            )
        await db.commit()

        # ---- audit rows so the trail is not empty on the admin page
        audit_rows = [
            ("a1", admin.id, "admin", "team_created", "team", 45),
            ("a2", admin.id, "admin", "team_members_set", "team", 44),
            ("a3", users["power@marshal.demo"].id, "user", "project_created", "project", 26),
            ("a4", users["power@marshal.demo"].id, "user", "spec_saved", "spec", 25),
            ("a5", admin.id, "admin", "risk_decided", "assessment", 21),
            ("a6", users["power@marshal.demo"].id, "deployment", "deploy_triggered", "project", 6),
            ("a7", users["business@marshal.demo"].id, "user", "build_started", "build", 3),
        ]
        for key, actor_id, category, action, resource_type, age in audit_rows:
            await _upsert(
                db, AuditLog, did("audit", key), created_at=ago(days=age), actor_id=actor_id,
                category=category, action=action, resource_type=resource_type,
                resource_id=None, project_id=None, detail={"demo": True},
                source_ip=None, user_agent="marshal demo seed", request_id=None,
                http_status=200, archived_at=None,
            )
        await db.commit()

        print(f"demo seed complete: {created} created, {updated} updated")
        print(
            f"  users={len(ROSTER)} teams={len(TEAMS)} projects={len(PROJECTS)} "
            f"usage_events≈{sum(users_at_step * per_user for _, users_at_step, per_user in FUNNEL_SHAPE)}"
        )


# -------------------------------------------------------------------- purge

PURGE_ORDER = [
    (AuditLog, [did("audit", k) for k, *_ in [("a1",), ("a2",), ("a3",), ("a4",), ("a5",), ("a6",), ("a7",)]]),
]


async def purge() -> None:
    """Delete exactly the rows this script owns (deterministic ids)."""
    async with SessionLocal() as db:
        ids = {
            Notification: [did("notification", k) for k, *_ in [("n1",), ("n2",), ("n3",), ("n4",)]],
            UsageEvent: None,  # matched by dedupe prefix below
            ModelInvocation: None,
            Deployment: [did("deployment", k) for k in ("mortgage-advisor", "policy-qa")],
            Lease: [did("lease", k) for k in ("mortgage-advisor", "policy-qa")],
            CodegenArtifact: None,
            CodegenBuild: [did("build", k) for k in ("mortgage-advisor", "branch-copilot", "policy-qa")],
            AiRiskAssessment: [did("risk", k) for k in ("mortgage-advisor", "kyc-review-assist")],
            ProjectMember: [did("share", "policy-qa:power")],
            Spec: None,
            Project: [did("project", p[0]) for p in PROJECTS],
            TeamMember: [did("member", f"{t}:{e}") for t, e, _ in [
                ("retail-banking", "power@marshal.demo", "lead"),
                ("retail-banking", "business@marshal.demo", "member"),
                ("claims-ops", "business@marshal.demo", "lead"),
                ("claims-ops", "power@marshal.demo", "member"),
                ("data-platform", "admin@marshal.demo", "lead"),
                ("data-platform", "power@marshal.demo", "member"),
            ]],
            Team: [did("team", t[0]) for t in TEAMS],
            # Roster users last: their memberships/events/invocations are gone by now
            User: [did("user", r[0]) for r in ROSTER],
        }
        removed = 0
        # dedupe-prefixed rows first (events), then artifacts/specs via parents
        result = await db.execute(
            delete(UsageEvent).where(UsageEvent.dedupe_key.like("demo:%"))
        )
        removed += result.rowcount or 0
        # Roster users' team memberships (ids are deterministic but keyed by
        # email, so sweep by user id to be certain nothing dangles).
        roster_user_ids = [did("user", r[0]) for r in ROSTER]
        result = await db.execute(
            delete(TeamMember).where(TeamMember.user_id.in_(roster_user_ids))
        )
        removed += result.rowcount or 0
        result = await db.execute(
            delete(ModelInvocation).where(ModelInvocation.prompt_text.like("[demo]%"))
        )
        removed += result.rowcount or 0
        project_ids = [did("project", p[0]) for p in PROJECTS]
        build_ids = [did("build", k) for k in ("mortgage-advisor", "branch-copilot", "policy-qa")]
        result = await db.execute(
            delete(CodegenArtifact).where(CodegenArtifact.build_id.in_(build_ids))
        )
        removed += result.rowcount or 0
        result = await db.execute(delete(Spec).where(Spec.project_id.in_(project_ids)))
        removed += result.rowcount or 0
        for model, row_ids in ids.items():
            if not row_ids:
                continue
            result = await db.execute(delete(model).where(model.id.in_(row_ids)))
            removed += result.rowcount or 0
        for model, row_ids in PURGE_ORDER:
            result = await db.execute(delete(model).where(model.id.in_(row_ids)))
            removed += result.rowcount or 0
        await db.commit()
        print(f"demo purge complete: {removed} rows removed")


# --------------------------------------------------------------------- tidy


async def tidy() -> None:
    """Remove drill residue that makes the showcase look untidy.

    Narrow by construction: only projects whose names start with a known probe
    prefix, and only chat sessions with the exact probe titles. Nothing else.
    """
    async with SessionLocal() as db:
        # Projects are SOFT-deleted — exactly what the app's own DELETE endpoint
        # does (status='deleted' + deleted_at). They vanish from every list
        # surface, their audit trail and deployment history stay intact, and the
        # step is reversible by clearing the two columns. Hard deletes were
        # rejected: probe projects own deployments and leases whose foreign keys
        # have no cascade, so the delete would either fail or need a hand-rolled
        # teardown of half the schema.
        # Prefix alone is not enough: a genuine project could legitimately be
        # called "S7 Probe Retrospective Notes". Every real probe name ends in
        # the epoch-millis the drill stamped on it, so require that too.
        import re

        stamped = re.compile(r"\d{10,}\s*$")
        hidden_projects = 0
        skipped_names: list[str] = []
        for prefix in TIDY_PROJECT_PREFIXES:
            rows = (
                await db.execute(
                    select(Project).where(
                        Project.name.like(f"{prefix}%"), Project.status != "deleted"
                    )
                )
            ).scalars().all()
            for project in rows:
                if not stamped.search(project.name):
                    skipped_names.append(project.name)
                    continue
                project.status = "deleted"
                project.deleted_at = NOW
                hidden_projects += 1
        await db.commit()
        for name in skipped_names:
            print(f"  kept (prefix matched but no drill timestamp): {name}")

        # Chat sessions have no soft-delete, and they are pure transcript noise,
        # so mirror the app's delete path: drop the DynamoDB messages, detach any
        # spec that references the session, then remove the row.
        titles = list(TIDY_SESSION_TITLES)
        stmt = select(ChatSession).where(ChatSession.title.in_(titles))
        sessions = (await db.execute(stmt)).scalars().all()
        for prefix in TIDY_SESSION_PREFIXES:
            sessions += (
                await db.execute(select(ChatSession).where(ChatSession.title.like(f"{prefix}%")))
            ).scalars().all()

        from app.services import chat as chat_service

        removed_sessions = 0
        for session in sessions:
            try:
                await chat_service.delete_messages(session.id)
            except Exception as exc:  # noqa: BLE001 — transcript store is best-effort
                print(f"  warn: message cleanup failed for {session.id}: {exc}")
            await db.execute(
                Spec.__table__.update()
                .where(Spec.session_id == session.id)
                .values(session_id=None)
            )
            await db.delete(session)
            removed_sessions += 1
        await db.commit()
        print(
            f"tidy complete: {hidden_projects} probe projects hidden (soft-deleted), "
            f"{removed_sessions} probe sessions removed"
        )


async def main() -> None:
    args = set(sys.argv[1:])
    if "--purge" in args:
        await purge()
        return
    if "--tidy" in args:
        await tidy()
    await seed()


if __name__ == "__main__":
    asyncio.run(main())
