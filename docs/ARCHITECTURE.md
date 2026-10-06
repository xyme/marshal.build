# marshal — Engineering Walkthrough (architecture)

> This is the mechanism-level companion to the product documentation: how
> each functionality actually works, which backend service powers it, and
> the exact seams a change would touch. File references are to the
> repository as of 17 Sep 2026 (post 13.5X); later entries are noted inline.
> The Functional Specification Document and the full Build Log are
> owner-internal and not part of this repository; the public record is
> [`CHANGELOG.md`](../CHANGELOG.md) (release history) and
> [`docs/ENGINEERING_LOG.md`](ENGINEERING_LOG.md) (a redacted excerpt of the
> delivery log). Identifiers of the form **13.5X** index entries of that
> delivery log; the ones that matter to a reader of this document appear in
> `ENGINEERING_LOG.md`. Per-feature requirements, design and task documents
> are kept by the maintainers and are not published.

How to read this: each section walks one functional area end to end —
user action → request path → service mechanics → data written → governance
hooks → failure modes. Cross-cutting machinery (the model seam, spend,
audit, tenancy) is described once in §2 and referenced everywhere else.

---

## 0. System at a glance

**Topology.** Browser → CloudFront (WAF, S14-01) → ALB → Next.js container
(`marshal-frontend`, :3000) → in-VPC hop → FastAPI container
(`marshal-backend`, :8000) → PostgreSQL (RDS), DynamoDB (chat messages +
shared counters), S3 (codegen workspace + audit archive), Bedrock, Cognito.
Deployed agents run in **separate AWS accounts** ("Enclaves") leased from
Innovation Sandbox (ISB) or, in `direct` mode, the installation account.

**Containers.** Three images built by CodeBuild (`marshal-image-build`)
from one repo: backend (FastAPI/uvicorn), frontend (Next.js standalone),
runner (the codegen/packaging CodeBuild environment). ECS Fargate runs
backend and frontend (2 tasks each); the runner image is *pulled by*
CodeBuild jobs, never long-running.

**Stacks** (CDK, `infra/`): MarshalNetworkStack (VPC), MarshalAuthStack
(Cognito user pool + hosted UI), MarshalDataStack (RDS, DynamoDB tables),
MarshalBuildStack (ECR repos, image-build project, codegen runner project,
workspace bucket), MarshalAppStack (ECS, ALB, CloudFront, alarms, IAM),
optional MarshalMarketingStack. The Enclave baseline is a CloudFormation
**StackSet** (`infra/blueprints/marshal-baseline.yaml`) instantiated
per-lease by ISB.

**Design principles that explain most of the code:**

1. **One seam per concern.** Every model call goes through
   `services/bedrock.py`; every access decision through
   `collab.resolve_role`; every clamp through `guardrails.resolve_models`
   / `resolve_session_call`; every audit row through `services/audit.py`.
   Governance attaches at seams, so features can't accidentally bypass it.
2. **Deterministic signals, not inference.** Anything a spec "opts into"
   (API keys, console, connectors, memory, tools, planning, packaged
   dependencies, composition) is an exact regex over the frozen
   requirements document (§7.2). The same parser feeds generation, the
   validation gate, the manifest, and the deployer — four consumers, one
   truth.
3. **Fail-closed for security, fail-open for accounting.** Missing
   security settings → 503; missing guardrail in cloud → 503; custody
   trouble at deploy → named failure. Rate/cost accounting outages →
   proceed and log.
4. **Provenance-blind governance.** Imported, forked, generated and
   hand-edited content flows through identical risk scoring, gates and
   custody. Where content came from never changes what governs it.
5. **Named refusals.** Every gate produces a machine-readable code
   (`{code, detail}` 422s, gate findings with `check` names). Appendix B
   catalogs them.
6. **Restart honesty.** In-process jobs (internal codegen, specgen) FAIL
   on restart with "please retry"; externalized jobs (CodeBuild builds)
   RE-ATTACH. Sweeper ticks are timestamp-derived and idempotent, so
   double-fires degrade to re-checks.

---

## 1. Identity and the request path

### 1.1 Edge → frontend → backend

Every request enters through CloudFront, which stamps a shared-secret
header onto origin requests (`x-origin-verify`, `infra/lib/app-stack.ts`
~561). The ALB's **default action is 403**; only two rules forward:
`/healthz` with the header → backend, anything else with the header →
frontend. Direct-to-ALB traffic dies at the edge. The secret lives in the
gitignored `.origin-verify` file, generated/exported by
`scripts/deploy-env.sh`.

The FastAPI service is therefore *never* internet-reachable except
`/healthz`. All API traffic goes through the Next.js route-handler proxy
`frontend/src/app/api/backend/[...path]/route.ts`:

- **Browser callers**: the proxy resolves the NextAuth session (encrypted
  httpOnly JWE cookie) and attaches `Authorization: Bearer
  <cognito access token>`. No session, or a failed token refresh
  (`tokenError`) → 401 `{"detail": "Not authenticated"}` from the proxy
  itself — the backend never sees the request. This is why direct `curl`
  with a Cognito JWT gets 401: **Cognito JWTs never bypass the session**.
- **Machine callers (B9)**: a bearer starting `mat_` is forwarded
  verbatim; FastAPI validates it (§1.4). This is the single sanctioned
  headless path through the same CloudFront/WAF ingress.
- Bodies stream both ways (`duplex: "half"`, hop-by-hop headers stripped)
  so SSE passes untouched; idempotent requests retry once on socket death.

Page-shell gating is separate: `frontend/src/middleware.ts` redirects
unauthenticated visits to `PROTECTED_PREFIXES` (/home, /chat, /projects,
/deployments, /admin, …) to the sign-in page. Checks are explicit path
code, not a `matcher` export (turbopack silently dropped the matcher).

### 1.2 NextAuth session (frontend/src/auth.ts)

Auth.js v5 + Cognito hosted UI, authorization-code flow, confidential
client. The OAuth scope requests `aws.cognito.signin.user.admin` — required
ON the issued token for self-service TOTP enrollment (Cognito user-context
APIs authenticate by the access token itself). Session policy: 12h JWT
cookie; the `jwt` callback refreshes the access token 60s before expiry
against `/oauth2/token`; refresh failure marks `tokenError` (→ proxy 401).
Sign-out POSTs the refresh token to `/oauth2/revoke` (best-effort), so a
stolen cookie cannot mint new access tokens; already-issued access tokens
stay valid ≤1h by design.

### 1.3 Backend authentication (backend/app/core/auth.py)

`get_current_user` is the single dependency:

1. **`mat_` branch first** (before any JWKS work): `service_accounts.
   resolve_token` hashes the bearer (sha256) and looks it up; revoked/
   expired/suspended → 401/403. The resolved service user gets
   `token_claims={}` (→ platform tenant) and `amr=[]`.
2. **JWT validation**: `PyJWKClient` (cached JWKS, 1h) → RS256 decode with
   issuer check → Cognito-specific checks `token_use == "access"` and
   `client_id == settings.cognito_client_id`.
3. **JIT provisioning**: unknown `sub` → create the `users` row. Email
   comes from Cognito `GetUser` *authenticated by the token itself* — the
   old `x-user-email` proxy header was removed because any direct token
   holder could spoof it. Concurrent first requests race on the unique
   `cognito_sub`; the loser rolls back and reuses the winner. First
   *federated* sign-in (username prefix `entraid_`/`okta_`) emits a
   SECURITY audit row.
4. **SSO role sync**: `cognito:groups` → role via precedence
   (admin > power > business); applied on the next request when
   `role_source == "sso"`. Admin-pinned roles (`role_source == "admin"`)
   win until Cognito catches up.
5. **Suspension**: blocks even tokens issued before the suspension.

### 1.4 The admin MFA gate (S14-02, the 13.5M lesson)

Cognito user-pool access tokens carry **no `amr` claim** (verified live —
the original gate locked every admin out). `session_mfa_satisfied(claims,
user)` therefore uses a **temporal seam**: Cognito always challenges a
user whose preferred factor is set, so any session whose `auth_time` ≥
`users.mfa_enrolled_at` (whole-second floored) necessarily passed the TOTP
challenge. `mfa_enrolled_at` is written by `POST /users/me/mfa/confirm`
after `VerifySoftwareToken` returns SUCCESS *and* the preference is set —
both steps required. The amr scan remains as a forward-compatible
disjunct. `enforce_admin_security` reads `security.admin_mfa_required`
from the fail-closed settings snapshot (cold read failure → dedicated
503, never a silent bypass) and 403s `{code: "admin_mfa_required"}`.

Dependency taxonomy:
- `require_role("admin")` — admin routers; includes the MFA gate; the
  `admin_readonly` carve-out lets flagged viewer accounts GET/HEAD under
  the same bar but 403s any mutation (`admin_read_only`).
- `require_editor_persona()` — Power-User surfaces (spec editing, import).
- `require_admin_security_if_admin` — shared surfaces (workbench, the
  deployments page, chat session create): no-op for non-admins, canonical
  admin security for admins.

### 1.5 Roles, personas, tenancy

**Role** (admin/power/business) is authorization, sourced from Cognito
groups. **Persona** (power/business) is UX surface, user-switchable —
except business-role users requesting the power persona set
`persona_upgrade_requested=True` and wait for
`POST /admin/users/{id}/persona-request/decide`.

**Tenancy** is a live seam, dormant at beta: `core/tenant.py` reads
`custom:tenant` from the verified claims, defaulting to
`PLATFORM_TENANT_ID`. `tenant_scope(stmt, Model)` is applied at list/read
seams; GA multi-tenancy is activation (B5), not rewrite.

**Service accounts (B9)** are `users` rows with `kind='service'`, roles
restricted to business/power (never admin, by design). Tokens:
`mat_` + 32 urlsafe bytes, stored as sha256 + display prefix, ≤2 live per
account, 1–365d expiry, value shown exactly once. They satisfy every
downstream contract (tenant seam, caps, audit) through the same user row.

---

## 2. Cross-cutting governance machinery

Everything in this section runs on *every* feature; later sections just
name it.

### 2.1 The model seam (services/bedrock.py)

`converse()` / `stream_converse()` are the only ways the platform talks to
a model. Mechanics:

- **Dispatch**: `model_id.startswith("ext/")` routes to the
  OpenAI-compatible adapter (`model_providers/openai_compat.py`), which
  honors the identical contract (preflight, clamps, recording) and fails
  closed with a named 502 (`external_endpoint_failure`).
- **Preflight** (`preflight_model_call`): (1) resolve the trusted security
  snapshot (can 503 — establishes a trustworthy redaction policy before
  anything else); (2) rate bucket `BUCKETS.acquire` — 60s fixed windows on
  shared DynamoDB counters across scopes platform/user/project, queueing
  ≤5s for streaming (fast-fail pre-SSE) or ≤30s otherwise, refusal = 429 +
  `Retry-After` + a SECURITY audit row; (3) cost caps via
  `TRACKER.check` — only refuses in `at_cap: "block"` mode and only when
  hydrated (fail-open accounting).
- **Clamping** (`_inference_config`): the single choke point — maxTokens
  min'd against platform bounds, temperature/top_p clamped into the
  platform window; temperature XOR topP (model families reject both). A
  `ValidationException` naming an optional param triggers one
  maxTokens-only retry.
- **PII redaction (S14-04)**: `guardrail_config(streaming, purpose)` —
  when `redaction_active(purpose)`, native calls attach the managed
  Bedrock guardrail with `streamProcessingMode: "sync"` (async would leak
  un-redacted tokens ahead of the verdict). Missing guardrail id in
  fail-closed cloud → 503. External endpoints can't use the managed
  guardrail, so the adapter does **pre-egress in-process regex masking**
  (`services/redaction.py: redact_messages`) under the same policy.
- **Recording**: success or failure, a `model_invocations` row lands
  (fire-and-forget, fresh session): prompt/response text + sha256s,
  tokens, stop_reason, latency, `cost_usd`, purpose, attribution ids from
  `InvocationCtx`. Streaming records partial text on failure because
  `StreamResult.text_parts` accumulates before each yield.
- **Reasoning suppression (13.5Y)**: `additional_request_fields(model_id)`
  pins `thinking: disabled` for `anthropic.claude*` on both paths.
  Adaptive-thinking models otherwise emit `reasoningContent` blocks that
  bill as outputTokens while the text-join drops them — invisible spend on
  every purpose and starvation of fixed-budget calls (a beta tester's codegen
  failures). Families that reject the field get one retry without it. The
  codegen runner's `HeadlessBedrock` mirrors the policy.
- **Retries**: ≤3 attempts on Throttling/ServiceUnavailable/ModelTimeout +
  transport errors, but *only before the first streamed token*. A
  transient "model identifier is invalid" ValidationException (observed
  live on the cross-region profile, 13.5Y) retries with the same backoff;
  a genuinely wrong id still fails after the attempt cap, pre-billing.

### 2.2 Spend (services/spend.py, pricing.py)

Real-time month-to-date counters in cents on shared state
(`spend:{YYYY-MM}:{scope}[:{id}]`), hydrated at boot from
`model_invocations` truth (seed-if-absent, idempotent across racing
tasks), re-hydrated on month rollover. `resolve_caps` merges platform
defaults (`PlatformSettings.cost`) with per-user/per-project
`budget_override_usd`. `TRACKER.add` returns threshold `Crossing`s
(50/75/90/100% by default) → `record_crossings` writes deduped `Alert`
rows + notification fan-out (owner always; admins at ≥100% or platform
scope). `check` raises `CostCapExceeded` → 429 `{code: "cost_cap"}`.
Purposes `classification`/`title` are user/project-cap exempt (governance
must not starve itself) but still charge the platform total.

Pricing: exact per-1K Decimals in `PRICING_MAP`; governed Bedrock models
without a SKU price at a deliberate **ceiling estimate** (0.100/0.500) so
unknown models can block a budget early but never bypass caps with NULL;
`ext/` models price from admin-entered rates (required at registration).
Enclave infra spend arrives separately (§12.3).

### 2.3 Audit (services/audit.py)

An outermost raw-ASGI middleware captures the response status and, *after*
the response has flushed (zero user-path latency), maps
`(METHOD, route-template)` through `AUDIT_ROUTE_REGISTRY` →
`AuditSpec(category, action, resource_type, id_param)`. Rules: 401 →
SECURITY `auth_failed`; 403 → SECURITY `permission_denied`; 422 on a
registered mutation → SECURITY `{action}_rejected`; ≥500 or unregistered →
skipped. Endpoints enrich via `set_audit_detail(request, **kv)` (before/
after values, sizes — **never content**). Registry completeness is
test-enforced (`test_audit.py`); deliberate omissions live in
`AUDIT_EXEMPT` (read-markers, presence heartbeats). `write_entry` mirrors
one-line JSON to stdout (independent CloudWatch copy) then inserts on a
fresh session, never raising — availability over completeness. There is
**no hash chaining**; tamper evidence = the CloudWatch mirror + the S14-06
retention pipeline (180d hot → S3 NDJSON `audit-archive/…` → prune only
after `archived_at` is set; 6h scheduled tick).

Model calls are audited separately as `model_invocations` and merged into
the admin audit browser as `source="model"` rows; viewing the log is
itself audited (`audit_viewed`).

### 2.4 Platform settings, feature flags, shared state, scheduler

- `PlatformSettings` is a singleton row (id=1) of JSONB blobs:
  `model_allowlist`, `param_bounds`, `rate_limits`, `cost`, `codegen`,
  `deployment_policies`, `security`, `governance` (risk policy),
  `connectors`, `feature_flags`. `get_controls()` caches 60s in-process;
  the PUT path validates hard, **merges** feature flags (an omitted flag
  must not silently die), commits, invalidates. In cloud, an emptied
  model allowlist fails closed. Security keys ride a separate
  last-known-good cache with fail-closed semantics
  (`SecuritySettingsUnavailable` → 503).
- **Shared state** (`services/shared_state.py`): atomic counters behind
  one interface — DynamoDB across ECS tasks in cloud, in-process locally.
  Rate limits, spend counters, and scheduler fences live here.
- **Scheduler election** (`services/scheduler.py: elected_loop`): shared
  counter window fence + Postgres advisory lock → exactly-one-task ticks.
  Cadences: lifecycle (health+expiry) 15 min, review escalation 15 min,
  webhook retries 60s, audit retention 6h, sandbox-spend poll 6h. Ticks
  are idempotent by design, so a double fire is a harmless re-check.

### 2.5 Body limits and exception mappers (app/main.py)

`BodySizeLimitMiddleware` enforces a 2MiB request backstop (413
`payload_too_large`) under the audit layer; field-level caps do the fine
work (the WAF's 8KB body rule deliberately stays COUNT). Global mappers:
`CostCapExceeded`→429 cost_cap, `RateLimited`→429 + Retry-After,
`SecuritySettingsUnavailable`→503 + Retry-After 5,
`ExternalEndpointError`→502 named. All routers mount under `/api/v1`.

---

## 3. Chat and authoring

### 3.1 Sessions (api/chat.py, services/chat.py)

`POST /chat/sessions`: business personas are **forced into guided mode**
(`resolve_session_mode`; requesting freeform → 422). A project-scoped
session requires editor+ on the project and inherits its `template_id`;
an explicit template must exist and be `active`. Template rails resolve at
creation (`resolve_models`, §6.1) and the session stores the resolved chat
model. Session metadata lives in Postgres (`chat_sessions`); **messages
live in DynamoDB**, keyed `session_id` + `sk = "{epoch_ms:013d}#{uuid8}"`.

**Delete** (`DELETE /sessions/{id}`, creator or project owner): detach
provenance (`specs.session_id`, `spec_generations.session_id` → NULL —
also enforced by the DB's `ON DELETE SET NULL` since 13.5AB), delete the
row and **commit first**, then drop the DynamoDB transcript inside a
try/except. The order matters: an orphaned transcript is invisible and
reapable, a headless session row is user-visible breakage — the old
transcript-first order produced exactly that "zombie" when the FK
violation fired.

### 3.2 Sending a message (SSE)

`POST /sessions/{id}/messages` in order:

1. `resolve_session_call(template, session.params)` → the `SessionCallPlan`
   (requested vs effective vs clamped_by — §6.1). GuardrailViolation → 422.
2. For native models, `guardrail_config(streaming=True, purpose="chat")`
   runs **before the SSE opens** so a missing fail-closed guardrail is a
   real 503, not an error event inside a 200 stream.
3. `preflight_model_call(streaming=True)` — same reasoning: refusals are
   real 429s pre-stream.
4. First message sets the session title (`derive_title` — heuristic
   truncation, not a model call). The user message is persisted, history
   fetched, and **context trimming** runs: `build_converse_messages` walks
   history newest-first within `chat_context_char_budget`, always keeps
   the newest message, then pops leading non-user messages (Bedrock
   requires user-first alternation).
5. The system prompt is assembled fresh each call:
   `chat_system_prompt(persona, template_prompt_block, substrate_prompt_block,
   connector_grounding_block)`. Substrate is clipped at 16k chars with a
   truncation marker and **never stored as a message** — trimming-proof by
   construction. Connector grounding (§10.2) is flag-gated metadata.
6. `stream_converse(..., enforce=False)` (already preflighted) yields
   `delta` events; `done` carries model/tokens/latency; on failure the
   partial text persists with `truncated=True` and an `error` event closes
   the stream.

### 3.3 Brownfield substrate (13.5N)

`PUT/DELETE /sessions/{id}/substrate` stores up to 200k chars of the
user's existing agent definition on the session row (`substrate`,
`substrate_source`). Input is pasted text, a client-read text file, or —
via `document_b64` — a PDF/DOCX the server extracts (§5.3). List/read
surfaces expose only presence/source/size; audit records size+source,
never content. The substrate block rides the SYSTEM prompt of chat *and*
all three specgen documents (the transcript seam would miss tasks.md and
get clipped by the design doc's 20k transcript tail).

### 3.4 Guided mode (services/guided.py)

A server-owned state machine in `session.guided_state` JSONB:
`use_case → context → behavior → clarify → generate → done`, pydantic
per-step schemas, lossless back-navigation (resubmitting earlier steps
allowed until clarification starts), never skip-ahead. Clarification: ≤3
rounds and ≤15 questions total; each round is ONE fast-model call
(`temperature=0`, `purpose="classification"` — cap-exempt) demanding
strict JSON (`{"questions":[…]}` or `{"proceed": true}`); parse failure
retries once then **freezes assumptions** instead of stranding the wizard.
Unanswered questions become labeled assumptions; `build_brief` renders a
deterministic markdown brief; `prepare_generation` persists it as a user
chat message (idempotent) so the *existing* generation pipeline consumes
it — no parallel path. Power/admin users may `switch-freeform`.

### 3.5 The Studio (S18)

Transparency rail over the same machinery — no separate execution path.
`GET /sessions/{id}/prompt-context` rebuilds exactly the system-prompt
segments the next message would carry (persona framing, template block,
substrate, connectors, clamped params) — pure read. `GET
/effective-params` serializes the same `SessionCallPlan` the invocation
seam uses, so display cannot drift from enforcement. Gating: business
personas 403 (admins exempt — dark-launch preview), non-admins 404 while
`feature_flags.studio_enabled` is off; `/meta/features` computes
`studio`/`studio_dark` per caller.

---

## 4. Spec generation and the editor

### 4.1 Generation job (services/specgen.py)

`POST /sessions/{id}/generate-spec` (guided sessions first persist the
brief) → `start_generation`: one running `SpecGeneration` per session
(else 409), docs array `[{type, status:"pending"}…]`, an asyncio task
under a **420 s budget** (300 until 13.5AB; sized so ONE 180 s Bedrock
stall on a document call survives a large three-document set — healthy
sets finish in 131–146 s). `_run_generation`:

- Builds the transcript, substrate block, connector grounding block,
  resolves models, and `_ensure_project` — creating the project if absent
  with a fast-model 2–5-word name (`purpose="title"`). The name call is
  **capped at 12 s** (`TITLE_TIMEOUT_S`) and its answer must be
  name-shaped (`valid_project_name`: one line, ≤6 words, no sentence
  punctuation); otherwise `fallback_project_name` (session title unless
  default, else the first five words of the opening message). Live
  (13.5AB) this call once stalled 183 s inside the budget and named a
  project with a sentence — a cosmetic call may never spend the budget.
- Documents run in order with hard dependencies: requirements → design
  (gets requirements + last 20k of transcript) → tasks (gets requirements
  + design, no transcript). A doc whose dependency failed is *skipped*
  with a named error, not crashed (R1.5). Per-doc status streams over an
  in-process bus to `GET /generate-spec/stream` (snapshot event first).
- `_generate_doc`: one converse call at the generation token budget;
  `stop_reason == "max_tokens"` fails with the real cause (template cap
  too small). Then `strip_fences` → structural check → if invalid, **one
  model repair pass** (`REPAIR_SYSTEM`) → `validate_doc` problems after
  repair are **blocking** for generation (they're merely advisory warnings
  in the editor). Required skeletons: requirements needs
  `# Requirements / ## Introduction / ## Functional Requirements /
  ## Assumptions`; design needs its five sections + a ```mermaid block;
  tasks needs `# Implementation Plan` + numbered task checkboxes (`- [ ] 1.`).
- Generated **requirements** additionally run `sync_project_composition`
  (§10.5) and `enforce_template_rail` (§6.3) before the Spec row lands — a
  generated cycle or forbidden capability fails THAT document by name.
- Wrap-up: project → `spec_complete` when all three types exist; success
  fires risk scoring (§9) + a notification. Restart rehydration marks
  running jobs failed-retriable.

### 4.2 Versions, drafts, rollback (services/specs.py)

Specs are **append-only versions** per (project, doc_type):
`save_version` = next version + advisory `validate_doc` warnings; for
requirements it first runs composition sync + capability rail (both are
aborting named 422s: nothing half-lands). Saving supersedes the author's
draft in the same transaction and triggers risk re-scoring (hash-cached).
`rollback` is non-destructive: copies version N's content as a NEW latest
version (`origin="rollback"`), re-running the same requirements checks.
Drafts are per (project, doc_type, user) upserts. Frontend editing uses
Monaco with version pickers and diffs; the ui-smoke pins the editor
rendering path.

### 4.3 Comments and presence (services/collab.py)

Comments anchor to (doc_type, anchor ≤256 chars, anchor_text ≤512);
viewers may comment; replies are one level deep (enforced), notifying the
parent author. Resolve/unresolve requires `can_moderate` (author or
editor+); delete requires author or owner and cascades replies. Presence
is a 30s heartbeat upsert per (project, user, surface); "active" = seen
within 90s; rows older than 24h are lazily pruned on write. Heartbeats and
read-markers are deliberate audit exemptions.

---

## 5. Import, export, and document ingestion

### 5.1 Export (services/export.py)

`GET /projects/{id}/export` (rate-limited 30/min/user) zips the latest
version of each doc at `.kiro/specs/<slug>/<doc_type>.md` + a
`manifest.json` (project, per-doc version/origin, template, risk verdict,
layout). The slug = sanitized name (≤48) + first 8 hex of the project id —
collision-free and the **round-trip contract** the importer accepts back.

### 5.2 Import (services/project_import.py, POST /projects/import)

Editor-persona surface; service accounts 403 by name. The request is an
XOR of four sources — `docs` (pasted markdown) / `archive_b64` (zip ≤1.4MB
client, 2M base64 chars server) / `url` / `document_b64` (PDF/DOCX) — with
one shared outcome: `import_spec_set` creates an independent
`Project(origin="imported")` + version-1 Spec rows, syncs composition,
enforces the capability rail, and fires the same risk trigger as authored
content (provenance-blind).

- **Zip path**: base64 → zipfile with bomb guards (≤64 entries, ≤4MiB
  uncompressed, per-entry UTF-8 required). Accepts the export layout
  `.kiro/specs/<slug>/<doc>.md` or root-level `<doc>.md`; first slug in
  sorted order wins when several sets exist.
- **URL path (I3)**: `fetch_public_url` — https/443 only, no credentials,
  every redirect hop (≤3) re-resolves DNS and requires every address to be
  globally routable (SSRF posture; the narrow DNS-rebind window is a
  recorded v1 risk), streaming 1.5MB cap, 8s timeout. The Content-Type
  header is **ignored by design**.
- **Detection** (`docs_from_bytes`, one truth for URL + upload): `%PDF-`
  → PDF extraction as the requirements doc; `PK\x03\x04` → open the zip
  once — spec-set entries win, else a `word/document.xml` probe routes to
  DOCX extraction, else the named no-spec-docs refusal; UTF-8 text →
  requirements; anything else → the supported-formats refusal.

### 5.3 PDF/DOCX extraction (services/doc_extract.py)

Deterministic, local, no OCR, no model calls. PDF via exact-pinned
`pypdf` (pure Python): encrypted flag checked before parsing → named
refusal; ≤500 pages; page texts accumulated with **early-stop** past the
caller's char cap (bounded CPU on hostile files); any pypdf exception maps
to one corrupt-file refusal (never a 500); empty text → the honest OCR
boundary refusal. DOCX via **stdlib only**: zipfile reads exactly
`word/document.xml` (≤16MiB uncompressed; media never decompressed),
expat parses it (Python 3.12 amplification protection), and a `w:p`/`w:t`/
`w:tab`/`w:br` walk yields paragraph text — table cells fall out as lines
for free. Caps: import 400k chars (the SpecSaveIn ceiling), substrate
200k. All extraction hops off the event loop via `asyncio.to_thread`.
The chat substrate document path shares the extractors but refuses
archives by name (a spec set is a project, not substrate).

### 5.4 Fork and submissions (services/marketplace.py)

`fork_sample` copies the sample's frozen `spec_snapshot` into a new
project (origin `marketplace_fork`) + v1 specs in ONE transaction with the
fork/usage counters; warnings surface deprecated templates and models that
left the registry. Submissions are sample rows with `status="submitted"`
and a snapshot frozen at submit time: one open submission per project
(409), resubmission lineage linked to the latest rejected/withdrawn row,
admin approve → **draft** (enters the normal curation path; author
credited), reject requires feedback. Notifications fan out both ways.

---

## 6. Templates, guardrails, and the marketplace

### 6.1 Rails resolution — the one clamp path (services/guardrails.py)

`resolve_models(template)`: effective allowlist = platform allowlist ∩
template `guardrails.model.allowed_models`, with `GuardrailViolation`
naming whichever layer emptied the intersection. Per-purpose model ids
(chat/requirements/design/tasks/fast/codegen) resolve via tier-mapped
fallback; **purpose pins** force `fast` and `codegen` to `sources={"aws"}`
so risk scoring and code generation can never route to an external
endpoint regardless of the allowlist. Token ceilings are min(platform,
template). `resolve_session_call(template, session.params)` then applies
session-requested overrides through the same rails, recording
`clamped_by` per field — chat consumes the effective values, the Studio
serializes the whole plan, so display equals enforcement.

`template_prompt_block` injects name/intent/required-sections into the
system prompt (a soft rail at authoring; the hard rails are model/params/
capabilities).

### 6.2 Template lifecycle (services/templates.py)

draft (edit/delete freely, invisible) → active (publish requires ≥1
allowed model; guardrail edits **bump `version`**) → deprecated (no new
sessions; existing work keeps functioning; never republishable; only
drafts delete). `_validate_guardrails` on create/update validates the
capabilities blob (§6.3). Session create + fork bump `usage_count`.

### 6.3 The capability rail (agent-substance R3.2)

`guardrails.capabilities.allowed_capabilities ⊆ {memory, packaged,
planning, tools}` (absent = all allowed) and `max_loop_iterations` (1–8,
default 5). Enforcement (`services/capabilities.py`):

- `declared_capabilities(requirements_md)` reuses the §7.2 signal parsers
  — one truth.
- `enforce_template_rail` runs at **all four requirements write seams**
  (editor save, rollback, import, generation) beside the composition sync,
  raising `CapabilityNotAllowed` → 422 `{code: "capability_not_allowed"}`
  naming the blocked rungs and the template.
- `blocked_for_manifest` re-checks at **deploy preflight** against the
  *current* rail: a template tightened after a build refuses the stale
  build by name — never a deploy-time surprise in the other direction.

### 6.4 Marketplace mechanics

Published-only visibility for users (admins see all), ILIKE search +
keyword containment, sort by forks/newest/views, per-request view counting
(non-admin), category counts grouped server-side. Curation validates
category + `models_used` against the live registry; publish requires the
full snapshot + metadata and fans out interest-based notifications (users
building in the category, capped 50).

---

## 7. Code generation

### 7.1 Providers, profiles, and the build row (services/codegen/)

`POST /projects/{id}/builds` → `runner.start_build`: one active build per
project (409); requirements + design snapshots required; the **frozen
`spec_snapshot`** on the `codegen_builds` row is the single source every
later derivation reads (signals, auth posture, conformance) — mid-build
edits cannot drift a build.

**Providers** (`provider.py`): `internal` (in-process Bedrock generation)
and `runner` (the S9 external workspace runner — same generation logic
executed in a CodeBuild container against an S3 workspace; `kiro` is
accepted as a legacy alias). In cloud the env var
`CODEGEN_PROVIDER` is authoritative; unknown values fail safe to
internal. `BuildCtx` is the frozen, DB-free carrier of everything a
provider may use (spec docs, model, budgets, auth decision, declared
connectors/tools/deps, loop caps).

**Artifact profiles** (`contract.ARTIFACT_PROFILES`):

| Profile | Code shape | Selected by | Constraint |
|---|---|---|---|
| `inline-cfn` (default) | Lambda `Code.ZipFile` inline, ≤4000 chars/handler, template ≤51k bytes | default / template scaffolding | stdlib+boto3 only |
| `packaged-cfn` | ONE deployer-staged zip via `PackageBucket`/`PackageKey` parameters | **spec signal only** ("SHALL use packaged dependencies") — template defaults excluded, request conflicts 409 | exact-pin deps ≤8; internal provider only (v1) |
| `cdk-app` | full TS CDK app synthesized on the runner, S3 asset parameters | request/template default | external runner only; vendored dependency closure |

**Lifecycles**: internal `queued → generating → validating →
ready/failed/cancelled` under a 480s in-process budget, cooperative cancel
between files; external inserts `dispatched` and **re-attaches after
restarts** (the job continues in CodeBuild; internal builds fail loudly —
the work died with the process). External timeout/contract-violation gets
exactly ONE automatic re-dispatch (`-r2` prefix).

### 7.2 Spec signals — the deterministic opt-in table

All regexes run over the frozen requirements snapshot; each phrase is the
ONLY door into its capability, and every consumer (generation prompt, the
gate, the manifest, the deployer) reads the same parser:

| Phrase | Parser | Effect |
|---|---|---|
| `Endpoint authentication: PUBLIC` (line-anchored) | `endpoint_auth_decision` | explicit-public build; default is keyed; conflicts block |
| "requires an API key" (negation-aware) | same | explicit-keyed |
| "SHALL provide a web test page" | `requires_web_console` | B19 console (§7.4) |
| "SHALL use connector <slug>" | `declared_connectors` (≤5, flag-gated) | C1 custody + helper |
| "SHALL use MCP tools from connector <slug>" | `mcp_tool_connectors` (=1) | C3 scaffold |
| "SHALL call agent <slug>" / "SHALL orchestrate agents a, b" | `composition.declared_dependencies` | composable agents (§10.5) |
| "SHALL keep conversation memory" | `requires_memory` | memory rung |
| "SHALL use packaged dependencies" | `requires_packaged` | packaged profile |
| "SHALL use tools: a, b" (≤6 snake names) | `declared_tools` | tools rung |
| "SHALL plan multi-step responses" | `requires_planning` | planning rung |

Connector-family signals are inert until `feature_flags.connectors_enabled`
— re-derived through the same flag-gated helper at validation time, so a
mid-build flag flip converges on the stricter read.

### 7.3 Internal generation (services/codegen/internal.py)

Three model phases, all through the bedrock seam with `purpose="codegen"`:

1. **Plan** — profile-specific system prompt (inline/packaged/cdk) → JSON
   plan of ≤6 files. Reserved files are **force-inserted, never
   model-optional**: the MCP scaffold, the tools agent, the planning
   agent, `requirements.txt` on packaged builds, the console page.
2. **Per-file generation** — chunked with budgets; the platform-verbatim
   shortcuts skip the model entirely: `src/mcp_agent.py` and
   `src/planning_agent.py` return byte-frozen scaffolds; the tools agent
   is **platform-assembled** (fixed imports + exact `TOOLS_JSON` + the
   verbatim Converse driver, with the model writing only the leaf
   `tool_<name>()` bodies — variance confined to local computation).
   Verbatim helper snippets (`CONNECTOR_HELPER_CODE`, `MEMORY_HELPER_CODE`,
   `AGENT_CALL_HELPER_CODE`) are injected into prompts and gate-checked.
3. **Template assembly** — the model receives handler *metadata* (env
   names and boto3 clients regex-extracted from the sources) plus wiring
   blocks (auth contract, console, connectors, dependencies, MCP, tools,
   planning, memory), and returns minified CFN JSON using
   `__MARSHAL_SOURCE_N__` placeholders; the platform injects exact code
   after JSON parsing, requiring each placeholder exactly once. Packaged
   builds skip the placeholder protocol (business code isn't inline) and
   must declare exactly the two package parameters.

An **inline-ceiling auto-retry** (inline profile only) spends one bounded
compaction pass when the only findings are oversized handlers, splicing
regenerated bodies into the template without a second assembly call.

The phase budgets (plan 2000, per-file/assembly ≤8192 min'd with the
template rail) assume **every output token is text** — that assumption is
enforced at the seam, not here: reasoning suppression (§2.1) exists
because adaptive-thinking models silently spent these budgets on
`reasoningContent` and starved the phases (13.5Y, a beta tester's failures).

**The planner receives the resolved profile** (`plan(ctx, profile=…)`, part
of the `CodegenProvider` Protocol since 13.5Z): it selects the
profile-specific system prompt (inline 3200-char handlers vs packaged
11k vs cdk) AND drives the reserved-file force-inserts. The in-process
runner once omitted it, so packaged builds planned as inline and never got
a `requirements.txt` — the external runner had always passed it.

**`requirements.txt` on packaged builds is derived, not authored.** After
the handlers generate, `third_party_imports()` walks their import lines
against `sys.stdlib_module_names` + the runtime-provided boto3/botocore
set. Nothing third-party → empty manifest, zero model calls (the common
case: packaged chosen for SIZE, not libraries — the contract accepts an
empty manifest). Otherwise one 300-token pin-only call, filtered by
`pin_lines_only()` so narration can never reach the deliverable.

**Size escalation (13.5AA).** The in-process pipeline is
`_generate_artifact_set` (plan → files → template) followed by, for inline
builds, the compaction retry and then the escalation check: if the gate's
findings are *only* inline-ceiling overflows and the workspace bucket is
configured, the build row's `artifact_profile` flips to `packaged-cfn`,
inline artifact rows are cleared, and `_generate_artifact_set` runs again
with `BuildCtx(require_packaged=True, size_escalated=True)` — stdlib-only
packaged prompts, 11k-char handler guidance. The re-plan may not import
anything third-party (`SizeEscalationDependencyError` → a named
`packaged_dependencies` finding, no CodeBuild spend); `_finalize` treats
the platform as the packaged declarer for the symmetry gate, writes
`manifest.packaged = true` (the deployer and the template capability rail
key on it) and `manifest.size_escalation` (resolved flips True only when
the gate passes). The spec phrase remains the only door to real
dependencies; escalation lifts the size limit and nothing else. Risk is
unaffected by escalation: `capability_reach` scores from the *documents*
(declared rungs), and an escalated build carries no third-party code, so
its reach equals the inline build's. (A spec that DECLARES packaged does
score +3 — drill 2 in 13.5AA routed medium → review for that reason.)

### 7.4 The validation gate (services/codegen/validate.py)

Pure-local, deterministic, ANY finding fails the build (no deploy-anyway).
Check catalog:

| check | contract |
|---|---|
| `template_json` | parses; Resources present |
| `service_allowlist` | every resource type within the §4.1.4 prefix set |
| `inline_packaging` / `packaged_dependencies` / cdk twins | profile shape: inline ≤4000 chars ZipFile; packaged = exact `{"S3Bucket":{"Ref":"PackageBucket"},…}` refs + `<module>.handler` + pinned requirements.txt; cdk = asset params + dependency closure |
| `endpoint_auth_signal` | contradictory auth intent blocks |
| `auth_contract` / `public_auth_contract` | keyed: ApiKey+UsagePlan(+ApiStages)+UsagePlanKey, ApiKeyRequired on every non-OPTIONS method (console GET /app exempt), ApiKeyId output; public: NO key infrastructure |
| `web_console` | fixed ids WebConsoleBucket/WebConsoleFunction, full PublicAccessBlock, GET /app, outputs, real page (no root-relative fetch — live 403 lesson), paste-key wiring when keyed |
| `connector_contract` | declared-only `marshal/agent-connectors/<slug>` reach; every declared slug wired (exact env value + IAM read grant); always-on so zero-connector builds stay clean |
| `memory_contract` | AgentMemoryTable iff declared: fixed id, TTL expires_at, session_id+sk keys, MEMORY_TABLE env, remember/recall helpers present — symmetric refusal |
| `agent_dependency` | declared-only `marshal/agent-dependencies/<slug>`; orchestrated pipelines must call every declared agent |
| `mcp_tool_loop` | scaffold artifact AND template ZipFile **byte-equal** to the platform scaffold; env slug/secret/system-prompt wiring; inline-cfn only |
| `tools_contract` | byte-prefix equality of the platform prelude (exact TOOLS_JSON + driver); one `tool_<name>()` per declared; bedrock:InvokeModel IAM |
| `planning_contract` | full byte equality of the planning scaffold |
| `loop_caps` | TOOLS/PLANNING_MAX_ITERATIONS integer within the template rail |
| `rung_combination` | MCP/tools/planning mutually exclusive (one conversation loop per agent — compose agents instead) |
| `packaged_profile` | profile ⟺ declaration symmetry both ways |
| `deploy_contract` | ApiUrl output; template ≤51k; ≥1 .py; README |
| `guardrail_patterns` | template forbidden-pattern detectors; unknown names surface as `unenforceable_patterns`, never silently dropped |
| `spec_conformance` | deterministic literal/field contract violations block; the model review is advisory |

`_finalize` re-derives every signal from the snapshot, runs the gate,
records the **manifest** (deploy truth: files+hashes, endpoint_auth,
declared connectors/mcp/deps/memory/packaged/tools/planning, validation
findings, packaging SBOM, external reconciliation), then the conformance
report (B23). The manifest — never a spec re-parse — is what the deploy
preflight and custody provisioning read.

### 7.5 The packaging pass (packaged-cfn; agent-substance R1)

After generation, the build pre-validates (broken templates must not burn
CodeBuild minutes), then dispatches ONE packaging job to the existing
`marshal-codegen-runner` CodeBuild project with `MARSHAL_MODE=package` on
workspace prefix `builds/<id>-pkg/` — **pip never runs on the control
plane**. The runner (`runner/main.py: run_package`) validates the
exact-pin discipline again (defense in depth), `pip install --target`, and
produces a **deterministic zip** (sorted entries, fixed 2020-01-01
timestamps, no __pycache__, ≤45MB) plus an SBOM-lite manifest
(name/version/license via importlib.metadata). The platform polls for
`package-results.json` (10s/420s), verifies sha256+size, copies the zip to
the durable `artifacts/<build_id>/package.zip` prefix, and records both
the binary artifact row and a browsable `package-manifest.json`.

### 7.6 The external runner contract (S9)

`s3://<workspace>/builds/<build_id>[-r2]/request/…` (build-request.json +
spec docs; `cancelled.json` tombstone) → `response/generated/*` +
`results.json`, all sha256-verified on ingest (`ContractViolation`
otherwise). The runner container is the reference engine: it imports the
platform's generation logic library-mode behind a `HeadlessBedrock` shim
(direct boto3, per-build token budget as a hard stop) and traps its own
crashes into a failed results manifest. Runner-reported usage reconciles
into priced `model_invocations` rows (`source="runner"`) + cap tracking.
Dispatch preflight enforces the rate bucket and estimates budget headroom
against caps before spending.

### 7.7 Artifacts, bundle, budgets

inline-cfn artifact content lives in Postgres rows; cdk-app/packaged
binaries live in S3 (`s3_key`, content NULL) with `storage.py` as the
single resolver. The handoff bundle zips the frozen spec set + generated
tree + build-manifest.json. Every internal model call is cap-governed
(`purpose="codegen"` is NOT exempt); external builds get one token budget
(60k default) enforced by the runner shim.

---

## 8. Deployment

### 8.1 Admission (api/deployments.py, services/deployment.py)

`POST /projects/{id}/deploy {build_id}` runs the **preflight chain**, all
before scoring or lease spend, each refusal a named `{code, detail}` 422:

1. Build sanity: exists on this project, `ready`, not a preview overlay.
2. Endpoint-auth policy: `policy_endpoint_auth` refuses legacy/sample
   payloads without a trustworthy keyed contract when the installation
   demands authentication.
3. `connectors_disabled` — manifest declares connectors but the flag is
   off; `connector_unavailable` — a declared slug unregistered/inactive,
   or an MCP source no longer `mcp_server`-typed.
4. `capability_not_allowed` — manifest capabilities vs the CURRENT
   template rail (§6.3).
5. `composition_dependency_unavailable` — every declared dependency must
   resolve, be visible to the deployer, and hold an ACTIVE deployment
   with an app_url (a gate-blocked agent cannot BE active — the running
   artifact IS the approved one).
6. Deployment policies: region/auth posture (422), capacity/concurrency
   (409) under the **atomic admission** transaction lock (reservation +
   complete policy snapshot, then a provider-aware capacity check: `isb`
   reads `/accounts` fresh and keeps a provider buffer, fail-closed on
   provider uncertainty; `direct` has no pool, so the concurrency limits
   already applied under the same lock are its capacity).
7. B20 mode gating (full_governance vs testbed), then the **risk gate**
   (§9): 403 GateBlocked / 409 ScoringInProgress.

An active deployment for the project turns the request into an
**in-place update** (same stack/lease; the prior row is superseded only on
success — failed updates leave the old row active, S11 rollback).

### 8.2 Leases and providers (services/sandbox/)

`isb`: request a lease from Innovation Sandbox (pooled accounts,
per-lease budget from deployment policies; the platform row commits
BEFORE the blocking activation so a crash can't orphan a provider lease),
then `deployment_session` = STS into the account's blueprint role.
`direct`: the installation account itself. The **blueprint StackSet**
stamps each pooled account with the cross-account role + scoped grants
(Secrets Manager prefixes `marshal/agent-connectors/*`,
`marshal/agent-dependencies/*`, staging-bucket rights). Stale blueprints
surface as the named `blueprint_outdated` custody error, never a bare
AccessDenied.

### 8.3 Custody provisioning (C1 copy pattern)

Inside `_run_deploy`, BEFORE `create_stack`, and again on update:

- `_ensure_connector_secrets`: for each manifest-declared slug, copy
  `{"base_url", "header", "value"}` (registry entry + platform-held
  credential) into the TARGET account at `marshal/agent-connectors/<slug>`.
  Fail-closed: an agent generated to call connectors must not deploy
  without them.
- `_ensure_dependency_secrets`: for each composition edge, read the
  dependency's LIVE ApiUrl + API key **through the dependency's own lease
  session** (cross-lease read) and copy `{"base_url", "header":
  "x-api-key", "value"}` to `marshal/agent-dependencies/<slug>` in the
  caller's account. Explicit-public dependencies ride `value: null`.

Generated code reads these at cold start via the verbatim helpers; caller
attribution rides the `x-marshal-caller` header (`MARSHAL_AGENT_ID` env),
NOT platform tokens — a platform credential inside an Enclave would be
worse custody than the problem it solves (recorded 13.5R deviation).

### 8.4 Stack create, assets, console, smoke

cdk-app and packaged-cfn assets stage into `marshal-assets-<account>`
before create (named blueprint error on denial); parameter binding via
`_asset_parameters` (CDK's `key||` convention vs the packaged plain key).
`create_stack` uses **TemplateBody** (hence the 51,200-byte ceiling),
`OnFailure="DO_NOTHING"` so the failure reason is captured before
self-cleanup. Event polling streams the CFN timeline onto the deployment
row (SSE to the UI). On CREATE_COMPLETE: health probe, TTL from policy →
`expires_at`, **web console staging** (B19: `web/*` artifacts put into the
console bucket through the assumed role, before smoke so `/app` exercises
the real page), then the **post-deploy smoke** — keyed APIs are smoked
WITH the key (a 403 must not pass vacuously); key expected-but-unreadable
reports inconclusive, never keyless.

`POST /deployment/api-key/reveal` fetches the key value LIVE from the
Enclave (stack outputs → ApiKeyId → value) — nothing platform-stored;
audited as a SECURITY event.

### 8.5 Lifecycle: health, expiry, teardown

The 15-minute elected tick (§2.4) runs both sweepers
(`deployment_lifecycle.py`): health probes every active deployment
(3 consecutive failures — derived from the timeline tail, not counters —
→ `degraded` + owner notification; success → recovery), and expiry warns
at 48h/24h (nearest threshold only) then tears down at TTL through the
STANDARD path with `force=True` (policy outranks composition).

`start_teardown` (owner action) refuses when other agents declare this
one (`DependentsBlockTeardown`, naming them). `_run_teardown`: empty the
console bucket (a non-empty bucket wedges delete_stack) → delete
connector/dependency secret copies (ISB leases only; direct mode shares
the account) → `delete_stack` → poll to DELETE_COMPLETE → delete staged
assets → terminate the lease → close sibling rows sharing the stack/lease
(the 27 Aug demo-reseed lesson: a surviving sibling claimed "active" over
deleted infrastructure).

### 8.6 The fleet view

`GET /users/me/deployments` (13.5X): cross-project list under the exact
projects-list visibility clause (owned ∪ shared ∪ team), newest-first,
capped 50 — backing the nav's `/deployments` page. Actions stay on the
project's deployment tab; the page is read-only by design.

---

## 9. Risk scoring and review

### 9.1 The rubric (services/risk.py, RUBRIC_VERSION = 3)

Seven weighted factors: three **model-scored** (data_sensitivity,
user_facing, request_volume — ONE temperature-0 fast-model call with
per-factor anchor lines, strict JSON parse, one retry) and four
**computed** (model_capability = max tier of the resolved models;
deployment_scope = fixed sandbox constant; composition_reach = from the
stored dependency graph — 0/3/6/8 by breadth/depth/orchestration;
capability_reach = from declared ladder rungs — 0 none / 2 memory / 3
packaged / 5 loop / 6 loop+packaged / +1 memory-combo, cap 8). Total =
round(Σ score×normalized_weight × 10), banded low ≤30 < medium ≤60 <
high by policy.

**Determinism key**: unique `(project_id, content_hash, rubric_version,
policy_version)` — identical content under the same rubric+policy scores
exactly ONCE; any edit voids approval and re-scores; error rows are
retryable; a per-project asyncio lock prevents double-scoring
(`ScoringInProgress` → 409 at the gate). A rubric or policy bump is a
cache miss by construction — each project re-scores once on its next
assessment, never a stampede.

**Triggers**: save / generation / fork / import all fire
`trigger_assessment` (fire-and-forget) — the S4-02 contract that keeps
provenance out of the gate.

### 9.2 Policy (B17)

Admins tune weights (scale-free proportions), band thresholds,
auto-approve-low, and the LLM-factor anchor texts through the versioned
risk policy (`PlatformSettings.governance`). `_policy_from_blob` fills
missing computed factors at their **default share** (proportional, never
absolute — an admin's 30/20/15 scale must not dilute a new factor 100×),
degrades silently on read, refuses loudly on write. `update_risk_policy`
bumps `policy_version` exactly once iff the effective policy changed —
the no-op detector compares weights rounded to 9dp (two proportional
fills disagree at 1e-17; a no-op PUT must not re-score the world).

### 9.3 The gate and the review workflow (S6)

`deployment_gate` demands an approved assessment of the CURRENT content:
low + auto-approve → `auto_approved`; medium/high → `pending`, routed to
reviewer groups (medium → managers, high → governance board). Reviewers
approve / reject (notes required) / request changes (one author reply;
the resubmission links its predecessor). Escalation runs on the 15-min
tick: overdue items climb the reviewer chain, then administrators; empty
groups skip a link rather than stall. In **Testbed** mode the gate is
advisory by default (still scored, still routed, bypass audited) —
Full Governance always enforces.

---

## 10. Connectors, composition, and integrations

### 10.1 C0 — the registry (services/connectors.py, admin/integrations)

Admin-registered connectors: slug, type (`http_api` | `mcp_server`),
base_url, auth header + write-only credential (platform-held at
`marshal/connectors/<slug>`), description, active flag. Health probes
record reachability; for `mcp_server` types the probe additionally runs
the **C2 handshake** (initialize → tools/list, SSE-frame tolerant) and
stores tool names/descriptions as card metadata. Everything is inert to
agents until `feature_flags.connectors_enabled` — the C0 honesty
contract: declarations in specs are prose until the installation opts in.

### 10.2 C2 — assistant grounding

`connector_grounding_block(db)` (flag-gated, **metadata-only** — names +
descriptions + tool lists, never content, satisfying redaction parity by
construction) rides the system prompt of chat, the prompt-context rail,
and all three specgen documents — so generated requirements can declare
real registered slugs instead of hallucinated ones.

### 10.3 C1 — agent consumption (copy custody)

The signal "SHALL use connector <slug>" wires generated code with the
verbatim `call_connector` helper reading the composite secret
`marshal/agent-connectors/<slug>` that the DEPLOYER copies into the
Enclave (§8.3). The `connector_contract` gate enforces declared-only
reach both ways. Egress control v1 is conformance-enforced declared
reach; VPC default-deny is the recorded v2.

### 10.4 C3 — MCP tool-loop agents

"SHALL use MCP tools from connector <slug>" (exactly one) reserves
`src/mcp_agent.py`: a byte-frozen stdlib scaffold (mini JSON-RPC client +
bounded 5-iteration Converse tool loop, tools discovered at cold start,
toolResults truncated 4k). Enforced by byte equality in BOTH the artifact
and the template ZipFile. Build start fails fast if the declared slug
isn't a registered active mcp_server; the deploy preflight re-checks.
The SharePoint adapter (`adapters/sharepoint/`) is an ordinary external
`mcp_server` for this machinery: an operator-run FastAPI service owning
the Entra client-credentials flow + token cache (single-flight, 5-min
margin), four read-only Graph tools with per-site allowlisting and honest
format refusals — marshal custodies only the adapter's static key. Zero
platform change; registering it requires an operator-hosted adapter and
an Entra app with Sites.Selected grants.

### 10.5 Composable agents (13.5R)

Signals "SHALL call agent <slug>" / "SHALL orchestrate agents a, b" parse
at every requirements write seam into `projects.composition` with
**RESOLVED project ids** (slugs embed names and break on rename; the
trailing 8-hex id fragment is the durable edge). Named 422s:
`composition_unresolved|self|cycle|depth|limit` (transitive walk, depth
≤2, ≤5 edges). Deploy preflight refuses non-callable dependencies;
custody per §8.3; generated callers use the verbatim `call_agent` helper
with attribution header; the `agent_dependency` gate enforces
declared-only reach and pipeline coverage (an orchestrator must call
every declared agent). Teardown refuses when dependents are live (§8.5).
`composition_reach` feeds risk (§9.1). v1 orchestration is a generated
coordinator handler; Step Functions is the recorded v2.

### 10.6 Webhooks and chat-ops (B10/B11)

Admin-registered notification integrations: webhook endpoints (HMAC-signed
posts, retried on the 60s tick) and Slack/Teams incoming-webhook chat-ops
with per-event subscriptions. They dispatch BEFORE user notification
preferences (an integration subscription is not a user pref), with
write-only secrets and test-send endpoints.

---

## 11. Teams, sharing, search

**Sharing**: `ProjectMember` rows (viewer/editor) by email; owner
transfer is one transaction (old owner → editor, target promoted).
**Teams** overlay the same seam: `TEAM_ROLE_TO_PROJECT_ROLE = lead→editor,
member→viewer`, consulted AFTER ownership and explicit membership;
**highest role wins, never lowers**. `collab.resolve_role` is THE access
rule: owner → max(member, team) → the governance-viewer carve-out
(pending-assessment reviewers get read-only viewer so "View spec" works;
admins deliberately get NO blanket implicit membership). Projects-list
visibility = owned ∪ shared ∪ team, tenant-scoped — reused verbatim by
search, the workbench, and the deployments page, so aggregates can never
show more than list surfaces.

**Search (B12)**: one endpoint fanning out to the EXISTING list queries
(projects/sessions/samples, 5 per group; spec-content ILIKE behind the
default-off `global_search_spec_content` flag); docs search is
client-side over the static registry. Nothing searchable that is not
listable.

---

## 12. Notifications, analytics, costs

### 12.1 Notifications (services/notifications.py)

`notify()` is the choke point: integrations dispatch first, then
per-event defaults merged with sparse user overrides (in_app/email);
`risk_review_requested`/`risk_escalated` are un-mutable. Email rides
SESv2 in a thread, gated by the enablement flag, suspension, and the
demo-account suppression (seeded demo users never receive e-mail); send
status is written back. Dedupe is a
partial unique index on `dedupe_key` — an IntegrityError IS the dedupe
hit. `emit()`/`emit_for_user()` are fire-and-forget with fresh sessions.
No digest mechanism exists; dedupe keys are the only aggregation.

### 12.2 Analytics (services/analytics.py)

Write-only from six product seams (sign_in daily-deduped, chat_message,
spec_saved, build_started, deploy_started, teardown), `team_id`
denormalized at write time; deliberately NO ingest endpoint. Admin reads:
funnel (distinct users + step conversion), DAU, team rollups.

### 12.3 Costs surfaces (services/costs.py, sandbox_costs.py)

Dashboard (totals/daily/by-model/top-spenders/projection + active
alerts), breakdown pivots (user/project/team/model/purpose/day),
chargeback (model spend → the CALLER; Enclave spend → the project OWNER —
the owner holds the lease; CSV export), team budget-vs-spend
(visibility-only). Enclave spend arrives via the 6-hour Cost Explorer
poller: STS into the management account's `MarshalCostReader`, DAILY
cost by LINKED_ACCOUNT, each account-day attributed to the lease with the
**largest overlap** of that day (containment attributed nothing for
sub-day leases — live S13 lesson), upserted into `sandbox_spend`.

---

## 13. Platform operations

### 13.1 Release path

`source scripts/deploy-env.sh && ./scripts/deploy-cloud.sh` — the
generic script sources the gitignored installation overlay
(`scripts/deploy-env.local.sh`) first, derives Cognito/CloudFront from
stack outputs when unpinned, and refuses to run without the origin-verify
token. Seven fail-closed steps: preflight identity cross-check → CDK
foundation stacks → frontend auth secret → CodeBuild image build (all
three images, stamped with `GIT_SHA`) → app stack → forced ECS rollout +
stability wait → landing/healthz smoke. First-run installs use
`--bootstrap`. The live acceptance bar is `scripts/ui-smoke.mjs`
(Playwright, 86 checks): real hosted-UI sign-ins including the
smoke-admin TOTP challenge (stdlib RFC-6238 with a single-use-window
guard), authenticated flows across collaboration/codegen/deploy surfaces,
docs-content pins, the deployments page pin, axe scans, and a
no-5xx/page-error sweep.

### 13.2 Migrations and build info

Alembic on Postgres; the entrypoint upgrades before serving.
`GET /meta/build-info` exposes `git_sha` and `migrations_in_sync`
(alembic head shipped in the image vs `alembic_version` in the DB) — the
first thing to check in any incident, and a pinned smoke check.

### 13.3 Observability

Ten CloudWatch alarms (ALB 5xx/latency, ECS health, RDS, DynamoDB,
Bedrock throttling) → SNS `marshal-ops-alerts` (human receipt is a
runbook gate). In-app governance alerts are separate (§2.2). The audit
stdout mirror lands in CloudWatch Logs independently of Postgres.
Deployment health/expiry, escalation, webhook retries, audit archival and
CE polling all run on the elected scheduler (§2.4) — single-flight across
tasks, harmless double-fires.

### 13.4 Sandbox estate

ISB hub in the management account; three pooled sandbox accounts recycled
through Available → Active(lease) → CleanUp. The baseline blueprint
StackSet stamps per-lease roles/grants; updating the template requires
`UsePreviousValue=true` for the account parameter (its baked default was
removed for portability). Zero standing instances is the healthy state —
instances exist only while leases do.

---

## Appendix A — where things live

| Concern | Files |
|---|---|
| Identity / MFA / RBAC | `backend/app/core/auth.py`, `frontend/src/auth.ts`, `frontend/src/app/api/backend/[...path]/route.ts`, `backend/app/services/{service_accounts,mfa}.py` |
| Model seam / redaction / spend / limits | `backend/app/services/{bedrock,spend,pricing,ratelimit,redaction}.py`, `backend/app/services/model_providers/openai_compat.py` |
| Audit / settings / scheduler | `backend/app/services/{audit,audit_retention,platform_settings,shared_state,scheduler}.py`, `backend/app/main.py` |
| Chat / guided / studio / substrate | `backend/app/api/chat.py`, `backend/app/services/{chat,guided}.py` |
| Specgen / editor / collab | `backend/app/services/{specgen,spec_validation,specs,collab}.py`, `backend/app/api/{specs,collab}.py` |
| Import / export / extraction | `backend/app/services/{project_import,export,doc_extract,exportlimit}.py` |
| Templates / rails / capabilities | `backend/app/services/{templates,guardrails,capabilities}.py`, `backend/app/api/templates.py` |
| Codegen | `backend/app/services/codegen/{runner,provider,internal,validate,scaffolds,contract,packaging,workspace_runner,storage,bundle,conformance,literals}.py`, `runner/main.py` |
| Deployment | `backend/app/services/{deployment,deployment_lifecycle}.py`, `backend/app/api/deployments.py`, `backend/app/services/sandbox/*` |
| Risk / review | `backend/app/services/{risk,risk_review}.py` |
| Connectors / composition | `backend/app/services/{connectors,composition}.py`, `adapters/sharepoint/` |
| Marketplace / teams / search | `backend/app/services/{marketplace,teams}.py`, `backend/app/api/search.py` |
| Costs / analytics / notifications | `backend/app/services/{costs,sandbox_costs,analytics,notifications}.py` |
| Infra / release | `infra/lib/*.ts`, `infra/blueprints/marshal-baseline.yaml`, `scripts/{deploy-env.sh,deploy-cloud.sh,ui-smoke.mjs}` |

## Appendix B — named refusal codes (machine-readable 422/403/409/429/5xx)

| Code | Where | Meaning |
|---|---|---|
| `admin_mfa_required` | admin surfaces | session predates TOTP enrollment while the policy is on |
| `admin_read_only` | admin mutations | beta viewer account attempted a write |
| `security_settings_unavailable` | any privileged/model path | fail-closed settings cold-read failure (503) |
| `rate_limited` / `cost_cap` | model seam | 429 with Retry-After / MTD cap in block mode |
| `external_endpoint_failure` | ext/ models | named 502, never a silent Bedrock fallback |
| `payload_too_large` | any mutation | 2MiB body backstop (413) |
| `composition_unresolved/self/cycle/depth/limit` | requirements writes | agent-graph refusals |
| `capability_not_allowed` | requirements writes + deploy preflight | template capability rail |
| `connectors_disabled` / `connector_unavailable` | deploy preflight | flag off / registry mismatch for declared connectors |
| `composition_dependency_unavailable` | deploy preflight | dependency lacks an active callable deployment |
| `policy_endpoint_auth` / `policy_region` | deploy preflight | deployment policy refusals |
| `blueprint_outdated` | deploy custody/staging | Enclave blueprint predates a required grant |
| `preview_only_build` | deploy | changeset-preview overlay selected for deploy |
| `validation_failed` / `packaging_failed` / `engine_timeout` / `contract_violation` / `cost_cap` / `rate_limited` / `crashed` / `restarted` | build error codes | gate findings / packaging pass / external runner outcomes |
| `DependentsBlockTeardown` (409) | teardown | live callers named; TTL sweeper overrides with force |
| import refusal strings | /projects/import | free-text ImportValidationError family (zip bombs, SSRF, formats, OCR boundary, encrypted, pins) |

## Appendix C — the data model

Source of truth: `backend/app/models/entities.py` (27 tables) + Alembic
(`backend/alembic/versions/`, linear chain). Conventions that repeat
everywhere:

- **Mixins**: `TenantMixin` puts an indexed `tenant_id` (default
  `PLATFORM_TENANT_ID`) on almost every table — the dormant GA multi-tenancy
  seam; `TimestampMixin` = created_at/updated_at (UTC, tz-aware).
- **Enums are code-enforced strings**, not DB enums — additive states need
  no migration; validators live at the service layer.
- **Dedupe via partial unique indexes** on nullable `dedupe_key` columns
  (notifications, alerts, usage_events, webhook_deliveries): an
  IntegrityError IS the dedupe signal.
- **JSONB blobs for evolving shapes** (manifests, guardrails, composition,
  guided state, factors) — mutations must deep-copy (in-place edits defeat
  the ORM change detector).
- **Soft state machines**: rows carry status strings + timestamps; nothing
  is hard-deleted except drafts and cascades (projects soft-delete via
  `status="deleted"` + `deleted_at`).

### C.1 Identity and access

| Table | Purpose | Key columns and semantics |
|---|---|---|
| `users` | Humans AND service accounts (`kind` human\|service) | `cognito_sub` unique (JIT key; `svc-<hex>` for service accounts); `role` (business/power/admin) + `role_source` (sso follows tokens; admin pins win); `persona` + `persona_upgrade_requested`; `mfa_enrolled_at` — the S14-02 temporal MFA seam; `status` active\|suspended (blocks pre-issued tokens); `budget_override_usd`; `notification_prefs` sparse JSONB; demo fields `account_class`/`experience_view`/`admin_readonly` guarded by CHECK constraints (presentation-only, human-only) |
| `service_account_tokens` | B9 bearer tokens | `token_hash` sha256 unique + `token_prefix` display; mandatory `expires_at`; `revoked_at`; `last_used_at` (writes throttled 1/min); ≤2 live per account enforced in service |
| `reviewer_group_members` | S6 review chains | `(group_name, user_id)` unique; groups `managers`\|`governance_board` — the Alpha-honest org model |
| `tenants` | GA groundwork | default row only at beta; `tenant_id` columns reference it logically (no FK — activation without surgery) |

### C.2 Projects and authoring

| Table | Purpose | Key columns and semantics |
|---|---|---|
| `projects` | The unit of governance | `user_id` = owner (membership is separate); `status` draft→spec_complete→…→archived/deleted; `origin` scratch\|template\|marketplace_fork\|imported (provenance display, never gating); `template_id`; `composition` JSONB — the RESOLVED agent-dependency graph `{dependencies:[{slug, project_id, name}], orchestration, depth}`, synced at every requirements write; `team_id` nullable (personal projects); `budget_override_usd` |
| `specs` | **Append-only** document versions | `(project_id, type, version)`; `type` requirements\|design\|tasks; `origin` generated\|edited\|rollback\|forked\|imported; `model_id` provenance; `session_id` provenance **`ON DELETE SET NULL`** (13.5AB — deleting a session detaches the pointer, never the version); content is full text — diffs are computed, never stored |
| `spec_drafts` | Autosave | `(project_id, type, user_id)` unique upsert — one draft per author, superseded transactionally on save |
| `spec_generations` | Specgen job rows | `docs` JSONB array `[{type, status, spec_id?, version?, error?}]` — the SSE snapshot source; status running\|done\|failed; restart rehydration fails running rows; `session_id` nullable **`ON DELETE SET NULL`** (13.5AB), history survives on `project_id` for audit/cost attribution |
| `chat_sessions` | Session metadata (messages live in DynamoDB) | `mode` freeform\|guided + `guided_state` JSONB (server-owned wizard state); `params` JSONB — S18 *requested* overrides (clamps happen at call time); `substrate` + `substrate_source` — brownfield text on the ROW (≤200k; deliberately not a message: immune to trimming and the 400KB Dynamo item ceiling); `template_id`, `model_id`, `status` |
| `spec_comments` | Anchored review threads | heading-slug `anchor` (≤256) + `anchor_text` snapshot; `parent_id` one level deep (service-enforced); `resolved_at/by`; orphan detection is a frontend concern |
| `project_presence` | Ephemeral heartbeats | composite PK (project_id, user_id); `last_seen_at` — active window 90s, lazy 24h prune; deliberately audit-exempt |
| `project_members` | Explicit shares | `(project_id, user_id)` unique; `role` viewer\|editor — owner stays `projects.user_id` |
| `teams` / `team_members` | S15 workspaces within the tenant | team name unique per tenant; member role lead\|member → project roles editor\|viewer (conservative mapping, never lowers explicit grants); `budget_usd` visibility-only |

### C.3 Catalog

| Table | Purpose | Key columns and semantics |
|---|---|---|
| `templates` | Governance templates | `guardrails` JSONB (`model.{allowed_models, max_tokens, temperature}`, `architecture.forbidden_patterns`, `capabilities.{allowed_capabilities, max_loop_iterations}`), `scaffolding` JSONB (`codegen.artifact_profile`, `required_spec_sections`, `starter_prompts`); `status` draft\|active\|deprecated; guardrail edits on ACTIVE templates bump `version`; `usage_count` |
| `marketplace_samples` | Curated samples AND submissions (one table) | frozen `spec_snapshot` `{requirements_md, design_md, tasks_md}`; `status` draft\|published\|archived + submitted\|rejected\|withdrawn; a submission = row with `source_project_id` + `submitted_at`; partial unique index = ONE open submission per project; `resubmission_of` lineage; `fork_count`/`view_count`; approval is the submitted→draft TRANSITION (recorded deviation: no resting 'approved' state) |

### C.4 Code generation

| Table | Purpose | Key columns and semantics |
|---|---|---|
| `codegen_builds` | Build jobs | partial unique index = one ACTIVE build per project; `provider` internal\|runner (legacy rows may carry `kiro`); `artifact_profile` inline-cfn\|packaged-cfn\|cdk-app; **`spec_snapshot`** JSONB — the frozen docs every derivation reads; `spec_hash` / `content_hash` (rebuild variance made visible, determinism not promised); **`manifest`** JSONB — deploy truth (§7.4): endpoint_auth, declared connectors/mcp/deps/memory/packaged/tools/planning, files+hashes, validation findings, package SBOM, external reconciliation; `external_job_id` + `retried` (re-attach + one re-dispatch); `error` `{code, message, findings[]}` |
| `codegen_artifacts` | Generated files | `(build_id, path)` unique; `content` in-DB for inline-cfn, `s3_key` (content NULL) for binaries/cdk — `storage.py` is the single resolver; `content_hash`, `size_bytes`, `language` |

### C.5 Deployment

| Table | Purpose | Key columns and semantics |
|---|---|---|
| `leases` | Sandbox account custody | `provider` direct\|isb; `external_lease_id` + `aws_account_id` (persisted via on_created BEFORE blocking activation — crash-safe); `budget_usd` from policy; status requested→active→terminated |
| `deployments` | Stack lifecycle | `build_id` (NULL = the legacy sample app); `mode` full_governance\|testbed — IMMUTABLE post-create, updates inherit; `status` pending→pre_flight→leasing→deploying/updating→active→tearing_down→torn_down (+failed/superseded); `health` unknown\|healthy\|degraded + `health_path` + `last_health_at` (3-strike derivation from the `timeline` tail); `timeline` JSONB (CFN events + probe/phase entries, probe noise trimmed to 12); `resources` JSONB; `app_url`; `expires_at` (TTL; NULL = grandfathered); sibling rows sharing stack/lease are closed together at teardown |
| `sandbox_spend` | Enclave infra cost | `(lease_id, date)` unique; daily CE-attributed USD (largest-overlap rule); folded into cost surfaces only when Cost Explorer is enabled |

### C.6 Governance and telemetry

| Table | Purpose | Key columns and semantics |
|---|---|---|
| `ai_risk_assessments` | The risk gate's memory | **unique `(project_id, content_hash, rubric_version, policy_version)`** — the determinism key; `factors` JSONB `{name: {score, rationale, weight}}` (7 factors at rubric v3); `decision` auto_approved\|pending\|approved\|rejected\|changes_requested; S6 routing fields (`assigned_group`, `escalated_to/at`, `admin_alerted_at`, `resubmission_of`, owner comment); `status` scored\|error (error rows retryable) |
| `audit_logs` | The action trail | `category` user\|admin\|deployment\|security; `action` from the route registry; `detail` JSONB (before/after, sizes — never content); `request_id`, `source_ip`, `http_status`; `archived_at` — retention NEVER deletes before it is set (180d hot → S3 NDJSON) |
| `model_invocations` | Every model call | `purpose` (chat/requirements/design/tasks/title/classification/risk/codegen); `source` platform\|runner (external reconciliation); prompt/response TEXT + sha256s; tokens, `cost_usd` (NULL = unpriced, ceiling-priced for unknown Bedrock SKUs), `usage_estimated` (ext endpoints omitting usage); joins the admin audit browser as `source="model"` rows |
| `alerts` | Governance alerts (≠ CloudWatch ops alarms) | `kind` cost_user/cost_project/cost_platform/…; monthly `dedupe_key` partial-unique; status active\|acknowledged |
| `notifications` | Per-recipient in-app/email | `(user_id, dedupe_key)` partial-unique; `email_status` skipped\|queued\|sent\|failed\|suppressed; prefs = code defaults merged with `users.notification_prefs` |
| `usage_events` | In-house analytics (D6: no third party) | six server-side funnel seams; `team_id` denormalized AT WRITE TIME (rollups reflect where work happened, not later moves); `dedupe_key` unique (sign_in = 1/user/day); no client beacons |
| `platform_settings` | THE singleton (id=1) | JSONB blobs: `model_allowlist`, `param_bounds`, `rate_limits`, `cost`, `codegen` (provider), `deployment_policies`, `security` (admin_mfa_required, pii_redaction — fail-closed cache), `custom_model_endpoints` (S17; keys in Secrets Manager), `connectors` (C0 registry; credentials in Secrets Manager), `feature_flags` (merge-on-PUT), `chat_ops` (B11), `governance` (B17 risk policy) |
| `webhook_endpoints` / `webhook_deliveries` | B10 outbound | HMAC secret in Secrets Manager; deliveries = at-least-once with bounded retries (`status` pending\|delivered\|failed\|dead — dead rows are the dead-letter view), per-endpoint dedupe |

### C.7 Stores outside Postgres

| Store | Contents | Shape |
|---|---|---|
| DynamoDB `marshal-chat-messages` | Chat transcripts | PK `session_id`, SK `"{epoch_ms:013d}#{uuid8}"`; items carry role/content/model_id/tokens/truncated |
| DynamoDB shared-state table | Cross-task atomic counters | `rate:{scope}:{window}` (TTL 2 windows), `spend:{YYYY-MM}:{scope}[:{id}]` (cents), scheduler fences |
| S3 codegen workspace bucket | Build exchange + durable artifacts + audit archive | `builds/<id>[-r2|-pkg]/request|response/…` (30d lifecycle), `artifacts/<build_id>/…` (180d), `audit-archive/<date>/…jsonl` |
| Secrets Manager (control plane) | Platform-held credentials | `marshal/connectors/<slug>`, `marshal/models/<slug>`, `marshal/webhooks/<id>`, `marshal/chatops/url`, `marshal/frontend/auth`, `marshal/smoke/admin-totp`, `marshal/adapters/sharepoint` |
| Secrets Manager (Enclave copies) | C1 copy custody, per deployment | `marshal/agent-connectors/<slug>`, `marshal/agent-dependencies/<slug>` — created pre-stack, deleted at teardown (ISB only) |
| Generated-agent tables (per Enclave) | e.g. `AgentMemoryTable` | live in the DEPLOYED stack, never the platform DB; torn down with the agent |
