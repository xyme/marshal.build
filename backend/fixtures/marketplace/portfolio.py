"""Canonical three-journey marketplace portfolio fixtures.

These fixtures are the complete, implementation-ready source of truth consumed by
``scripts/seed_marketplace.py``. They deliberately describe synthetic sample
workloads only. Verification evidence, URLs, screenshots, and ``last_verified``
are populated only after a verification run succeeds.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

PORTFOLIO_SCHEMA_VERSION = 1
PORTFOLIO_NAMESPACE = "marshal.demo.portfolio/v1"
PORTFOLIO_KEYS = (
    "sentiment-notes-agent",
    "employee-handbook-qa",
    "approval-workflow-copilot",
)

SONNET = "us.anthropic.claude-sonnet-5"

_COMMON_POSTURE = {
    "deployment_mode": "full_governance",
    "codegen_provider": "internal",
    "artifact_profile": "inline-cfn",
    "endpoint_auth": "api_key",
    "synthetic_only": True,
}

_SENTIMENT_REQUIREMENTS = """# Sentiment Notes Agent — Requirements

## Purpose and boundaries
Build a synthetic note-classification demo. It is deterministic application code:
there is no model call at request time and no claim that sentiment is inferred
outside the exact vocabulary below.

## Requirement 1: Deterministic note classification
**User story:** As a demo operator, I want one note classified predictably so that
the same input always produces the same evidence.

### Acceptance criteria
1. `POST /notes` SHALL accept exactly `{note}` and reject unknown fields with HTTP 400.
2. The sentiment vocabulary SHALL be exactly `{positive, neutral, negative}`.
3. Matching SHALL lowercase the note and compare whole words against these exact sets:
   - positive: `{clear, easy, great, helpful, resolved}`
   - negative: `{blocked, confusing, error, failed, slow}`
4. The score SHALL equal positive matches minus negative matches. Positive scores map to
   `positive`, negative scores map to `negative`, and zero (including ties) maps to
   `neutral`. Duplicate words count once.
5. The response SHALL contain exactly `{note_id, sentiment, score, matched_terms}`;
   `matched_terms` SHALL be sorted lexicographically.
6. Repeating the same normalized note SHALL return the same `note_id`, derived from its
   SHA-256 digest, and the same response.

## Requirement 2: Keyed API and B19 web console
**User story:** As a presenter, I want a browser form without exposing credentials so
that the demo can be exercised safely.

### Acceptance criteria
1. Every business API endpoint SHALL require an API key.
2. The application SHALL provide a web test page at `GET /app` using the B19 web-console
   contract. The page itself is key-exempt; `/notes` is not.
3. The page SHALL provide a paste-key field, keep the key in memory only, send it as
   `x-api-key`, use a stage-aware relative API base, and contain no external scripts,
   fonts, stylesheets, analytics, or stored credentials.
4. The page SHALL show the exact sentiment, score, and matched terms returned by the API.

## Requirement 3: Synthetic evidence and reset
1. The canonical evidence inputs SHALL be the three notes embedded in the fixture assets.
2. `GET /notes` SHALL list stored synthetic notes and `DELETE /notes` SHALL remove all
   note rows for a reset rehearsal. Both endpoints require the API key.
3. DynamoDB SHALL store only the synthetic note, digest-derived ID, deterministic result,
   and creation timestamp. No personal data is accepted or needed.

## Non-functional requirements
- Endpoint authentication: API key required.
- Deployment mode is Full Governance.
- Code generation provider is internal and artifact profile is inline-cfn.
- All classification logic is local deterministic Python; no Bedrock permission or
  runtime model invocation is allowed.
- The generated Lambda source must remain under the inline packaging ceiling.
"""

_SENTIMENT_DESIGN = """# Sentiment Notes Agent — Design

## Baseline architecture
```mermaid
graph LR
    Browser["B19 /app paste-key console"] --> Api["API Gateway REST API"]
    Api --> Fn["Python Lambda: exact vocabulary classifier"]
    Fn --> Notes["DynamoDB synthetic notes"]
```

The Lambda normalizes whitespace and case, tokenizes words, intersects two frozen term
sets, computes `positive_count - negative_count`, and derives `note_id` from SHA-256 of
the normalized note. There is no probabilistic branch and no model client.

## Routes
- `GET /app`: self-contained B19 page, key-exempt.
- `POST /notes`: classify and idempotently store one synthetic note, API-key protected.
- `GET /notes`: list demo rows, API-key protected.
- `DELETE /notes`: reset demo rows, API-key protected.

## Security and evidence
The API key is provisioned by the platform and pasted by the operator; it never appears
in source, CloudFormation, browser storage, logs, screenshots, or catalog metadata.
Screenshots are accepted only after the browser assertion sees an exact fixture result.
The deployment can be left active only when the rehearsal is explicitly run with
`KEEP_SENTIMENT_ACTIVE=1`; otherwise the standard teardown path is mandatory.
"""

_SENTIMENT_TASKS = """# Sentiment Notes Agent — Implementation Plan

- [ ] 1. Define the exact positive/negative term sets and SHA-256 note identity.
- [ ] 2. Generate an inline-cfn REST API with API-key protection on every notes route.
- [ ] 3. Implement deterministic POST/GET/DELETE note handlers and DynamoDB storage.
- [ ] 4. Add the B19 self-contained `/app` paste-key console with stage-aware fetch paths.
- [ ] 5. Prove the three canonical notes return positive, neutral, and negative exactly.
- [ ] 6. Capture a browser screenshot with the key absent from DOM, logs, and evidence.
- [ ] 7. Teardown unless `KEEP_SENTIMENT_ACTIVE=1`, then record verified evidence hashes.
"""

_HANDBOOK_STOP_WORDS = [
    "a",
    "an",
    "and",
    "are",
    "can",
    "do",
    "does",
    "for",
    "how",
    "i",
    "in",
    "is",
    "it",
    "many",
    "must",
    "my",
    "of",
    "on",
    "the",
    "to",
    "what",
    "when",
    "within",
]

_HANDBOOK_PASSAGES = [
    {
        "passage_id": "NS-HB-001",
        "section": "Flexible Work — Core Hours",
        "retrieval_terms": [
            "core",
            "days",
            "hours",
            "reachable",
            "remote",
            "remotely",
            "work",
        ],
        "text": (
            "Northstar employees may work remotely up to three days each week. "
            "Teams publish two shared core-hour blocks, 10:00–12:00 and 14:00–16:00 "
            "local team time, when members should be reachable unless on approved leave."
        ),
    },
    {
        "passage_id": "NS-HB-002",
        "section": "Learning — Annual Allowance",
        "retrieval_terms": ["allowance", "course", "learning", "roll", "training"],
        "text": (
            "Each Northstar employee has a fictional annual learning allowance of "
            "$1,200. A manager must approve a course before purchase; unused allowance "
            "does not roll into the next calendar year."
        ),
    },
    {
        "passage_id": "NS-HB-003",
        "section": "Expenses — Submission Window",
        "retrieval_terms": [
            "expense",
            "expenses",
            "purchase",
            "receipt",
            "receipts",
            "reimbursement",
            "submit",
            "submitted",
        ],
        "text": (
            "Synthetic business expenses must be submitted within 30 calendar days of "
            "purchase. Receipts are required for items of $25 or more, and the employee's "
            "manager reviews the report before reimbursement."
        ),
    },
    {
        "passage_id": "NS-HB-004",
        "section": "Information Security — Lost Devices",
        "retrieval_terms": [
            "desk",
            "device",
            "incident",
            "lost",
            "recovery",
            "report",
            "service",
        ],
        "text": (
            "A lost Northstar device must be reported to the fictional Service Desk "
            "within one hour of discovery. The employee should not attempt remote recovery "
            "and should follow the Service Desk incident instructions."
        ),
    },
]

_HANDBOOK_CORPUS_MD = "\n".join(
    f"- `{passage['passage_id']}` | section={json.dumps(passage['section'], ensure_ascii=False)} "
    f"| terms={','.join(passage['retrieval_terms'])} "
    f"| text={json.dumps(passage['text'], ensure_ascii=False)}"
    for passage in _HANDBOOK_PASSAGES
)

_HANDBOOK_REQUIREMENTS = """# Employee Handbook Q&A — Requirements

## Purpose and boundaries
Answer questions only from four fictional Northstar handbook passages bundled with this
snapshot. The application has no upload, crawler, external corpus, vector database,
embedding call, or runtime model.

## Requirement 1: Deterministic retrieval
**User story:** As a Northstar employee in a fictional demo, I want a cited handbook
answer so that I can verify it against the supplied passage.

### Acceptance criteria
1. API Gateway SHALL expose exactly `POST /questions` with resource `PathPart: questions`; aliases such as `/answer` are forbidden. The route SHALL accept exactly `{question}` and reject unknown fields.
2. The corpus SHALL contain exactly `NS-HB-001` through `NS-HB-004` as copied in the
   fixture asset manifest; no other source is permitted.
3. Retrieval SHALL normalize the question with Unicode NFKC, lowercase it, extract
   tokens using `[a-z0-9]+`, de-duplicate tokens, and remove exactly this stop-word
   set: `{a, an, and, are, can, do, does, for, how, i, in, is, it, many, must, my,
   of, on, the, to, what, when, within}`.
4. Each passage SHALL use only its explicit `retrieval_terms` array in the fixture
   asset. Its score SHALL be the number of distinct normalized question tokens in that
   array. A passage is eligible only at score >= 1; ties SHALL resolve by ascending
   `passage_id`. Terms SHALL NOT be inferred from passage prose at runtime.
5. A known answer SHALL equal the selected passage's complete `text` value byte-for-byte,
   never newly generated or summarized prose.
6. A grounded response SHALL contain exactly `{answer, citations, abstained}` with
   `abstained=false` and exactly one citation `{passage_id, section}`.

## Requirement 2: Abstention
1. When no fixed retrieval term matches, the response SHALL be exactly
   `{"answer":"I don't have enough evidence in the Northstar handbook to answer that.","citations":[],"abstained":true}`.
2. The application SHALL NOT answer from general knowledge, infer policy, merge passages,
   or invoke a model as a fallback.
3. The canonical unknown question `What is Northstar's parental leave duration?` SHALL
   produce the exact abstention response.

## Requirement 3: Deployment and privacy
1. Every API route SHALL require an API key.
2. The corpus and known-answer set are fictional synthetic data committed with the
   catalog fixture. No external data dependency is allowed.
3. Deployment SHALL use Full Governance, internal code generation, and inline-cfn.
4. The deployed application SHALL be stateless apart from platform/deployment logs; no
   user question store is required.
5. The rehearsal SHALL deploy only for the scheduled drill and SHALL teardown afterward.

## Non-functional requirements
- Deterministic byte-for-byte responses for the canonical known-answer set.
- No Bedrock, embeddings, OpenSearch, network fetch, upload, or runtime model permission.
- Four compact passages must fit safely in the inline Lambda package.
"""

_HANDBOOK_DESIGN = f"""# Employee Handbook Q&A — Design

## Architecture
```mermaid
graph LR
    Caller["Keyed API caller"] --> Api["API Gateway REST API"]
    Api --> Fn["Python Lambda: fixed-term retrieval + extractive answer"]
    Corpus["Four inline Northstar passages"] --> Fn
```

The source embeds exactly four versioned passages, the exact stop-word array, and each
passage's frozen `retrieval_terms`. The handler applies Unicode NFKC, lowercase
`[a-z0-9]+` tokenization, distinct-token intersection scoring, eligibility at score >= 1,
and an ascending-passage-ID tie-break. It copies the selected passage's complete `text`
byte-for-byte into the answer. If no term matches, it returns the exact abstention object.
There is no mutable corpus and no runtime model client.

## Frozen corpus (embed these values exactly)
{_HANDBOOK_CORPUS_MD}

## Canonical known-answer vectors
- `How many remote days can I work?` -> `NS-HB-001`.
- `What is the annual learning allowance?` -> `NS-HB-002`.
- `When do I submit an expense and when is a receipt required?` -> `NS-HB-003`.
- `How quickly must I report a lost device?` -> `NS-HB-004`.
- `What is Northstar's parental leave duration?` -> exact abstention.

## Lifecycle
The rehearsal builds, deploys, checks every vector against the live keyed endpoint,
records response hashes, and tears down. No persistent search service or idle-cost floor
is part of the baseline.
"""

_HANDBOOK_TASKS = """# Employee Handbook Q&A — Implementation Plan

- [ ] 0. Treat the complete requirements and design snapshot as implementation source of truth (generation contract v2).
- [ ] 1. Embed exactly the four versioned Northstar passages from the fixture assets.
- [ ] 2. Implement fixed retrieval terms, deterministic scoring, and passage-ID tie-break.
- [ ] 3. Implement extractive cited answers and the exact no-evidence abstention object.
- [ ] 4. Generate a keyed inline-cfn API with no Bedrock/OpenSearch/network permissions.
- [ ] 5. Exercise all four known answers and the unknown question against the live API.
- [ ] 6. Hash the live response vectors into private evidence.
- [ ] 7. Teardown the deployment and verify the terminal lifecycle state.
"""

_APPROVAL_REQUIREMENTS = """# Approval Workflow Copilot — Requirements

## 1. Synthetic request and deterministic policy
POST /requests SHALL accept exactly {requester_ref, department, category, amount_usd, vendor_status, budget_remaining_usd, justification}. Reject any missing/extra field; only synthetic values are permitted.

Evaluate policy version northstar-procurement-v1 in local deterministic code. Add reason vendor_not_approved when vendor_status is not approved; add reason insufficient_budget when amount_usd exceeds budget_remaining_usd; eligible is true only when reasons is empty. Route amounts up to 5000 to manager_optional, amounts through 25000 to manager_required, and larger amounts to director_required.
The policy literals SHALL be one of "vendor_not_approved", "insufficient_budget", "manager_optional", "manager_required", "director_required".
The policy result SHALL contain exactly {policy_version, eligible, route, reasons}.
Store and return a request record with request_id, request, policy_result, draft_recommendation, and human_decision as distinct values; a new request has null draft and decision.

## 2. Advisory Bedrock draft
POST /requests/{request_id}/draft SHALL invoke Bedrock Sonnet using the stored synthetic request and immutable policy result. Before prompt construction, recursively convert every DynamoDB Decimal to a JSON-native int/float so JSON serialization cannot fail. For the canonical eligible fixture, explicitly instruct Sonnet to recommend approve.
The recommendation SHALL be one of "approve", "reject", "needs_information".
The advisory draft SHALL contain exactly {recommendation, rationale, advisory} and advisory is true. Store it separately, append draft_created, and never write/default human_decision.

## 3. One-time human decision
POST /requests/{request_id}/decision SHALL accept exactly {decision, approver_ref, comment}.
The human decision SHALL be one of "approved", "rejected".
Use a DynamoDB conditional write requiring human_decision to be absent and return HTTP 409 on any second decision. Preserve policy_result, draft_recommendation, and human_decision as distinct objects.

## 4. Independently verifiable audit
Use separate request-state and append-only audit DynamoDB tables.
Every transition SHALL append exactly {request_id, sk, event_type, payload, payload_sha256}.
Compute payload_sha256 as lowercase SHA-256 over UTF-8 JSON with recursive key sorting and compact separators. sk is a unique lexicographically time-ordered string. No route may update/delete an event. GET /requests/{request_id}/audit SHALL recursively convert DynamoDB Decimal values to JSON-native numbers, then return all events in ascending sk order with sk, payload, and payload_sha256.

## 5. Deployment posture
Every business endpoint SHALL require an API key. The canonical baseline SHALL contain no SNS resource or publish action. A rehearsal-only candidate may add exactly one AWS::SNS::Topic with logical ID ApprovalDecisionTopic solely to produce a non-applied CloudFormation changeset preview and is never deployed. Use internal inline-cfn code generation, Full Governance, restore the baseline design after preview, and teardown after proof.
"""

_APPROVAL_DESIGN = """# Approval Workflow Copilot — Design

## Baseline architecture
```mermaid
graph LR
    Caller["Keyed caller"] --> Api["API Gateway REST"]
    Api --> Intake["IntakePolicy Lambda"]
    Api --> Draft["AdvisoryDraft Lambda"]
    Api --> Decision["DecisionAudit Lambda"]
    Intake --> Requests["Request-state table"]
    Intake --> Audit["Append-only audit table"]
    Draft --> Requests
    Draft --> Audit
    Draft --> Bedrock["Bedrock Sonnet"]
    Decision --> Requests
    Decision --> Audit
```

Use exactly three cohesive Python handlers: IntakePolicy owns `POST /requests`; AdvisoryDraft owns `POST /requests/{request_id}/draft`; DecisionAudit owns `POST /requests/{request_id}/decision` and `GET /requests/{request_id}/audit`. Wire exact environment names `REQUESTS_TABLE`, `AUDIT_TABLE`, and (draft only) `BEDROCK_MODEL_ID`. Each handler that reads DynamoDB uses a recursive Decimal-to-int/float JSON adapter before prompt, hash, or response serialization. Keep each handler under the inline ZipFile ceiling and grant only its required DynamoDB/Bedrock actions.

The baseline has no SNS. The rehearsal appends the exact preview-only SNS instruction separately, verifies the candidate without deployment, then restores this design.
"""

_APPROVAL_TASKS = """# Approval Workflow Copilot — Implementation Plan

- [ ] 0. Treat the complete requirements and design snapshot as implementation source of truth (generation contract v2).
- [ ] 1. Implement the exact request contract and `northstar-procurement-v1` policy in IntakePolicy.
- [ ] 2. Implement the Sonnet advisory draft without human-decision write access.
- [ ] 3. Implement conditional one-time human decisions and ordered audit reads.
- [ ] 4. Create separate request/audit tables, exact payload SHA-256 events, keyed API routes, and least-privilege roles.
- [ ] 5. Emit a deployable baseline with no SNS; the rehearsal owns preview-only overlay and teardown.
"""


def _base_metadata(
    key: str,
    *,
    runtime_model: str | None,
    tech_stack: list[str],
    est_build_usd: float,
    est_run_usd_month: float,
) -> dict[str, Any]:
    return {
        "portfolio_key": key,
        "portfolio_schema_version": PORTFOLIO_SCHEMA_VERSION,
        "portfolio_namespace": PORTFOLIO_NAMESPACE,
        "governance": dict(_COMMON_POSTURE),
        "runtime_model": runtime_model,
        "tech_stack": tech_stack,
        "est_build_usd": est_build_usd,
        "est_run_usd_month": est_run_usd_month,
        # Rehearsal owns these fields. A fixture/seed run never fabricates them.
        "last_verified": None,
        "verification": {"status": "unverified"},
    }


def _assets(*, sample_data: dict[str, Any], required_evidence: list[str]) -> dict[str, Any]:
    return {
        "screenshots": [],
        "demo_url": None,
        "sample_data_s3_key": None,
        "sample_data_inline": sample_data,
        "evidence": {
            "status": "unverified",
            "private": True,
            "required": required_evidence,
        },
    }


PORTFOLIO_SNAPSHOTS: tuple[dict[str, Any], ...] = (
    {
        "portfolio_key": "sentiment-notes-agent",
        "legacy_title": None,
        "title": "Sentiment Notes Agent",
        "description": "Deterministic synthetic note sentiment with an API-keyed endpoint and B19 paste-key web console.",
        "long_description": (
            "A deliberately small, reproducible showcase: submit one synthetic note and "
            "receive `positive`, `neutral`, or `negative` from an exact frozen vocabulary. "
            "There is no runtime model. The API is keyed and the self-contained B19 browser "
            "console keeps a pasted key in memory only. Synthetic demo data only."
        ),
        "category": "data_analysis",
        "complexity": "beginner",
        "models_used": [],
        "template_name": None,
        "spec_snapshot": {
            "requirements_md": _SENTIMENT_REQUIREMENTS,
            "design_md": _SENTIMENT_DESIGN,
            "tasks_md": _SENTIMENT_TASKS,
        },
        "assets": _assets(
            sample_data={
                "notes": [
                    {
                        "note": "The onboarding guide was clear and helpful.",
                        "expected": {
                            "sentiment": "positive",
                            "score": 2,
                            "matched_terms": ["clear", "helpful"],
                        },
                    },
                    {
                        "note": "The weekly update arrived on Tuesday.",
                        "expected": {"sentiment": "neutral", "score": 0, "matched_terms": []},
                    },
                    {
                        "note": "The expense form failed twice and blocked submission.",
                        "expected": {
                            "sentiment": "negative",
                            "score": -2,
                            "matched_terms": ["blocked", "failed"],
                        },
                    },
                ],
                "vocabulary": {
                    "positive": ["clear", "easy", "great", "helpful", "resolved"],
                    "negative": ["blocked", "confusing", "error", "failed", "slow"],
                    "labels": ["positive", "neutral", "negative"],
                },
            },
            required_evidence=[
                "exact_spec_hash",
                "b23_deterministic_verdicts",
                "live_semantic_vector",
                "three_live_note_vectors",
                "browser_screenshot_sha256",
                "teardown_or_explicit_keep_active",
            ],
        ),
        "keywords": ["sentiment", "deterministic", "synthetic", "api-key", "web-console", "b19"],
        "metadata_extra": _base_metadata(
            "sentiment-notes-agent",
            runtime_model=None,
            tech_stack=["API Gateway", "Lambda", "DynamoDB"],
            est_build_usd=0.6,
            est_run_usd_month=2,
        ),
    },
    {
        "portfolio_key": "employee-handbook-qa",
        "legacy_title": "Policy Q&A Bot",
        "title": "Employee Handbook Q&A",
        "description": "Deterministic cited answers over four fictional Northstar handbook passages, with exact abstention and no runtime model.",
        "long_description": (
            "An institution-neutral, synthetic handbook demonstration with exactly four "
            "Northstar passages. Fixed retrieval terms produce extractive answers and one "
            "verifiable citation; absent evidence produces an exact abstention. No uploads, "
            "external corpus, embeddings, search service, or runtime model are used."
        ),
        "category": "chatbot",
        "complexity": "intermediate",
        "models_used": [],
        "template_name": None,
        "spec_snapshot": {
            "requirements_md": _HANDBOOK_REQUIREMENTS,
            "design_md": _HANDBOOK_DESIGN,
            "tasks_md": _HANDBOOK_TASKS,
        },
        "assets": _assets(
            sample_data={
                "corpus_name": "Northstar Employee Handbook (fictional demo v1)",
                "tokenization": "unicode-nfkc-lowercase-[a-z0-9]+-distinct",
                "stop_words": _HANDBOOK_STOP_WORDS,
                "passages": _HANDBOOK_PASSAGES,
                "known_answers": [
                    {
                        "question": "How many remote days can I work?",
                        "passage_id": "NS-HB-001",
                        "abstained": False,
                    },
                    {
                        "question": "What is the annual learning allowance?",
                        "passage_id": "NS-HB-002",
                        "abstained": False,
                    },
                    {
                        "question": "When do I submit an expense and when is a receipt required?",
                        "passage_id": "NS-HB-003",
                        "abstained": False,
                    },
                    {
                        "question": "How quickly must I report a lost device?",
                        "passage_id": "NS-HB-004",
                        "abstained": False,
                    },
                    {
                        "question": "What is Northstar's parental leave duration?",
                        "passage_id": None,
                        "abstained": True,
                    },
                ],
                "abstention": "I don't have enough evidence in the Northstar handbook to answer that.",
            },
            required_evidence=[
                "exact_spec_hash",
                "b23_deterministic_verdicts",
                "live_semantic_vector",
                "four_citation_vectors",
                "exact_abstention_vector",
                "teardown_complete",
            ],
        ),
        "keywords": [
            "handbook",
            "citations",
            "abstention",
            "deterministic",
            "synthetic",
            "northstar",
        ],
        "metadata_extra": _base_metadata(
            "employee-handbook-qa",
            runtime_model=None,
            tech_stack=["API Gateway", "Lambda"],
            est_build_usd=0.8,
            est_run_usd_month=1.5,
        ),
    },
    {
        "portfolio_key": "approval-workflow-copilot",
        "legacy_title": "Approval Workflow Copilot",
        "title": "Approval Workflow Copilot",
        "description": "Synthetic procurement policy, Bedrock advisory draft, distinct human decision, immutable DynamoDB audit, and preview-only SNS overlay.",
        "long_description": (
            "A governance-first synthetic procurement workflow. Policy is deterministic; "
            "Bedrock Sonnet drafts an advisory recommendation; only a separate human "
            "action records approval or rejection. Every transition appends hash-bearing "
            "DynamoDB audit evidence. SNS exists only in a non-applied preview candidate."
        ),
        "category": "workflow_automation",
        "complexity": "advanced",
        "models_used": [SONNET],
        "template_name": None,
        "spec_snapshot": {
            "requirements_md": _APPROVAL_REQUIREMENTS,
            "design_md": _APPROVAL_DESIGN,
            "tasks_md": _APPROVAL_TASKS,
        },
        "assets": _assets(
            sample_data={
                "request": {
                    "requester_ref": "REQ-DEMO-1042",
                    "department": "Operations",
                    "category": "software",
                    "amount_usd": 18000,
                    "vendor_status": "approved",
                    "budget_remaining_usd": 25000,
                    "justification": "Synthetic workflow tooling for the Northstar operations demo.",
                },
                "expected_policy": {
                    "policy_version": "northstar-procurement-v1",
                    "eligible": True,
                    "route": "manager_required",
                    "reasons": [],
                },
                "expected_draft_recommendation": "approve",
                "evidence_human_decision": "rejected",
                "sns_preview_overlay": {
                    "logical_id": "ApprovalDecisionTopic",
                    "resource_type": "AWS::SNS::Topic",
                    "must_not_deploy": True,
                },
            },
            required_evidence=[
                "exact_spec_hash",
                "b23_deterministic_verdicts",
                "live_semantic_vector",
                "policy_draft_human_separation",
                "ordered_hashed_audit_events",
                "same_build_no_change_preview",
                "sns_candidate_preview_only",
                "baseline_restore",
                "teardown_complete",
            ],
        ),
        "keywords": [
            "approval",
            "procurement",
            "human-decision",
            "audit",
            "bedrock",
            "changeset-preview",
        ],
        "metadata_extra": _base_metadata(
            "approval-workflow-copilot",
            runtime_model=SONNET,
            tech_stack=["API Gateway", "Lambda", "DynamoDB", "Bedrock"],
            est_build_usd=1.8,
            est_run_usd_month=8,
        ),
    },
)


def canonical_json(value: Any) -> str:
    """Stable JSON representation used by seed/rehearsal evidence."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fixture_sha256(fixture: dict[str, Any]) -> str:
    """SHA-256 of one complete canonical fixture."""
    return sha256(canonical_json(fixture).encode()).hexdigest()


def spec_sha256(snapshot: dict[str, Any]) -> str:
    """Exact server build hash for one three-document snapshot."""
    payload = (
        f"requirements:{snapshot['requirements_md']}\n"
        f"design:{snapshot['design_md']}\n"
        f"tasks:{snapshot['tasks_md']}"
    )
    return sha256(payload.encode()).hexdigest()


def catalog_payload(fixture: dict[str, Any]) -> dict[str, Any]:
    """Verification-bound catalog fields, excluding live evidence state."""
    metadata = fixture["metadata_extra"]
    assets = fixture["assets"]
    return {
        "portfolio_key": fixture["portfolio_key"],
        "title": fixture["title"],
        "description": fixture["description"],
        "long_description": fixture["long_description"],
        "category": fixture["category"],
        "complexity": fixture["complexity"],
        "models_used": fixture["models_used"],
        "template_name": fixture["template_name"],
        "spec_snapshot": fixture["spec_snapshot"],
        "keywords": fixture["keywords"],
        "assets": {
            "sample_data_inline": assets["sample_data_inline"],
            "required_evidence": assets["evidence"]["required"],
        },
        "metadata_extra": {
            "portfolio_key": metadata["portfolio_key"],
            "portfolio_schema_version": metadata["portfolio_schema_version"],
            "portfolio_namespace": metadata["portfolio_namespace"],
            "governance": metadata["governance"],
            "runtime_model": metadata["runtime_model"],
            "tech_stack": metadata["tech_stack"],
            "est_build_usd": metadata["est_build_usd"],
            "est_run_usd_month": metadata["est_run_usd_month"],
        },
    }


def catalog_payload_sha256(fixture: dict[str, Any]) -> str:
    """SHA-256 binding rehearsal evidence to the forker-visible payload."""
    return sha256(canonical_json(catalog_payload(fixture)).encode()).hexdigest()


def portfolio_sha256() -> str:
    """SHA-256 of the ordered exact-three fixture set."""
    return sha256(canonical_json(PORTFOLIO_SNAPSHOTS).encode()).hexdigest()


def fixture_by_key(key: str) -> dict[str, Any]:
    """Return one canonical fixture by stable portfolio key."""
    for fixture in PORTFOLIO_SNAPSHOTS:
        if fixture["portfolio_key"] == key:
            return fixture
    raise KeyError(key)


def _validate() -> None:
    if len(PORTFOLIO_SNAPSHOTS) != 3:
        raise RuntimeError("canonical demo portfolio must contain exactly three snapshots")
    keys = [str(item.get("portfolio_key", "")) for item in PORTFOLIO_SNAPSHOTS]
    titles = [str(item.get("title", "")) for item in PORTFOLIO_SNAPSHOTS]
    if tuple(keys) != PORTFOLIO_KEYS or len(set(keys)) != 3 or len(set(titles)) != 3:
        raise RuntimeError(
            "canonical demo portfolio keys/titles are missing, duplicated, or reordered"
        )
    for item in PORTFOLIO_SNAPSHOTS:
        key = item["portfolio_key"]
        metadata = item.get("metadata_extra") or {}
        posture = metadata.get("governance") or {}
        if metadata.get("portfolio_key") != key:
            raise RuntimeError(f"{key}: metadata portfolio key mismatch")
        if any(posture.get(field) != value for field, value in _COMMON_POSTURE.items()):
            raise RuntimeError(f"{key}: canonical governance posture mismatch")
        snapshot = item.get("spec_snapshot") or {}
        if any(
            not str(snapshot.get(name, "")).strip()
            for name in ("requirements_md", "design_md", "tasks_md")
        ):
            raise RuntimeError(f"{key}: incomplete three-document snapshot")
        assets = item.get("assets") or {}
        if assets.get("evidence", {}).get("status") != "unverified":
            raise RuntimeError(f"{key}: fixtures must not claim unearned evidence")


_validate()

__all__ = [
    "PORTFOLIO_KEYS",
    "PORTFOLIO_NAMESPACE",
    "PORTFOLIO_SCHEMA_VERSION",
    "PORTFOLIO_SNAPSHOTS",
    "canonical_json",
    "catalog_payload",
    "catalog_payload_sha256",
    "fixture_by_key",
    "fixture_sha256",
    "portfolio_sha256",
    "spec_sha256",
]
