# Security posture, threat model & control map

Sprint 14 (Alpha 4) deliverable. This is the document an enterprise security
review asks for: what the controls are, where they live in the code, what was
tested, and what risk is knowingly accepted.

Scope: the marshal control plane (your installation), its codegen pipeline,
and the Enclave (AWS account) vending chain. Generated customer workloads run
in isolated vended accounts and are out of scope beyond how the platform
creates and tears them down.

---

## 1. Control map

Cross-reference for reviewers. "Evidence" is where to look, not a promise.

| # | Control | Implementation | Evidence |
|---|---|---|---|
| AC-1 | Authentication | Cognito user pool, OIDC authorization-code flow; every API route validates a signed access token against JWKS (issuer, `token_use`, `client_id` all checked) | `backend/app/core/auth.py::validate_token` |
| AC-2 | Second factor | TOTP available to all users; **required for admins** when `security.admin_mfa_required` is on (enforced platform-side because Cognito cannot condition MFA on group membership). SMS deliberately not enabled | `auth.py::require_role`, `services/mfa.py`, `tests/test_security_hardening.py` |
| AC-3 | Authorization (roles) | `business | power | admin` resolved from Cognito groups on every request; admin-pinned roles survive stale claims | `auth.py::resolve_role`, `require_role` |
| AC-4 | Authorization (resources) | One project access seam: `require_project_role(min)`; membership + governance-viewer rules resolved per request | `services/collab.py::resolve_role` |
| AC-5 | Session revocation | Suspension blocks tokens already issued (checked per request), plus Cognito admin-disable for new tokens | `auth.py::get_current_user`, `tests/test_projects_admin.py` |
| AC-6 | Tenant isolation | `tenant_id` on 19 tables; filters at the read seams; cross-tenant reads invisible even to the row owner | `core/tenant.py`, `tests/test_platform_scale.py` |
| NE-1 | Edge protections | WAF on CloudFront: AWS core rule set + known-bad-inputs + per-IP rate rule (3000/5min, above measured 5× load) | `infra/lib/app-stack.ts::AppWebAcl` |
| NE-2 | Transport | TLS terminated at CloudFront; HSTS 1 year with preload; HTTP redirected | `app-stack.ts::SecurityHeaders` |
| NE-3 | Browser hardening | CSP, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, strict referrer policy | `app-stack.ts::SecurityHeaders` |
| NE-4 | Origin lockdown | Backend not internet-reachable (private subnets, Service Connect); ALB returns 403 without CloudFront's verify header | `app-stack.ts` listener default action |
| DP-1 | Encryption at rest | DynamoDB, S3 (SSE-S3), and Secrets Manager are encrypted; S3 blocks public access and enforces TLS. **RDS storage is created unencrypted by the current `data-stack.ts`** despite private networking, Multi-AZ, PITR, and deletion protection; enabling encryption on an existing instance is a snapshot-copy cutover (see ROADMAP.md) | `infra/lib/data-stack.ts`, `build-stack.ts` |
| DP-2 | Encryption in transit | HTTPS at the edge; AWS SDK TLS for Bedrock/S3/DDB; Postgres over the VPC | — |
| DP-3 | Secrets handling | No credentials in code or images; DB and auth secrets injected from Secrets Manager at task start; ISB JWT read at runtime with `kms:Decrypt` scoped via `kms:ViaService` | `app-stack.ts` task role, `entrypoint.sh` |
| DP-4 | Sensitive-data masking | Bedrock guardrail anonymises cards, bank details, government IDs and credentials in prompts/completions when `security.pii_redaction` is on. Off by default; an installation that handles regulated data should turn it on with scope `all` (chat included) and accept the latency cost measured in §4 | `app-stack.ts::PiiGuardrail`, `services/bedrock.py::guardrail_config` |
| DP-5 | Data residency | Single region us-east-1 for compute, data, and Bedrock inference profiles | `infra/bin/marshal.ts` (region pin) |
| AU-1 | Audit trail | Every mutating route audited via a registry whose **completeness test fails the build** when a route is added without coverage | `services/audit.py`, `tests/test_audit.py::test_registry_covers_mutating_routes` |
| AU-2 | Model-call ledger | Every Bedrock call recorded (purpose, model, tokens, cost, prompt hash, success) | `services/bedrock.py::_record_invocation` |
| AU-3 | Retention | 180 days hot in Postgres, then archived to S3 as NDJSON and pruned; **rows are never deleted before `archived_at` is set** | `services/audit_retention.py`, tests |
| AU-4 | Export | CSV (humans) and NDJSON (machine/compliance) from one endpoint sharing filters and caps | `api/admin_audit.py::export_audit_logs` |
| GV-1 | Deployment gate | Risk-scored spec content must be approved before any Enclave lease; maker-checker for medium/high risk | `services/risk.py::deployment_gate` |
| GV-2 | Usage bounds | Cost caps (user/project/platform, alert or hard block), rate limits, deployment concurrency/TTL/budget policies — all fail closed with actionable messages | `services/spend.py`, `ratelimit.py`, `policies.py` |
| GV-3 | Generated-code gate | Static validation before deploy: resource-type allowlist, forbidden-pattern detectors, packaging rules, deploy contract | `services/codegen/validate.py` |
| SC-1 | Image scanning | ECR scan-on-push on all three repositories | `infra/lib/build-stack.ts` |
| SC-2 | Dependency gate | `pip-audit` + `npm audit` run before images build; **new high/critical fails the build** (`AUDIT_STRICT=1`); exceptions documented per advisory | `scripts/supply-chain-audit.sh`, `scripts/audit-allowlist.txt` |
| SC-3 | SBOM | Generated per build and retained one year at `s3://<workspace>/sbom/<image-tag>/` | `build-stack.ts` post_build |
| SC-4 | Codegen supply chain | Runner uses a vendored, lockfile-pinned dependency closure with `npm ci` offline — no arbitrary installs during generation | `runner/`, S10 as-built |

---

## 2. Threat model (STRIDE-lite)

Assets: user specifications and chats; generated code; audit and cost records;
Enclave credentials; the platform's own AWS credentials.

Trust boundaries: browser → CloudFront/WAF → frontend SSR → backend → (Bedrock,
Postgres, DynamoDB, S3, ISB API, vended AWS accounts).

### T1 — Enclave vending chain abuse
*Spoofing / Elevation.* The platform mints ISB service JWTs and assumes a
deployment role in vended accounts. A leaked JWT secret or an over-broad role
would grant account-level access.
**Controls:** JWT secret in Secrets Manager, CMK-encrypted, readable only via
Secrets Manager (`kms:ViaService` condition); role assumption scoped to
`AIFactoryDeploymentRole` in any account (ISB owns pool membership); short-lived
service tokens (15 min); every lease request audited with project/user.
**Residual risk:** the deployment role inside a vended account is broad by
design (it must deploy arbitrary generated stacks). Blast radius is one
disposable account, which is the point of the Enclave model.

### T2 — Prompt injection steering generated code
*Tampering.* Spec text is untrusted. A user (or content pasted from elsewhere)
can try to steer generation into emitting privileged resources, backdoor IAM
users, or public data stores.
**Controls:** generation output is never trusted — the S8 validation gate
enforces a resource-type allowlist, forbidden-pattern detectors, packaging
rules and the deploy contract; deployment additionally requires risk approval;
generated stacks only ever run in a vended account. Regression test asserts a
hostile template (IAM user + public bucket) is rejected.
**Residual risk:** the gate is *static*. A generated app can still be
functionally wrong or insecure at runtime within allowed resource types — S16-02
adds post-deploy smoke probing. Recorded, not hidden.

### T3 — Cross-tenant / cross-project data exposure
*Information disclosure.* Multi-team workspaces (S15) make this the highest-value
target.
**Controls:** single access seam per project; tenant filters at read seams;
isolation tests assert a foreign-tenant row is invisible even to its owner.
**Residual risk:** tenancy is groundwork until S15 activates it; the seams are
tested but not yet exercised by real multi-team traffic. S14-05 scope includes
them deliberately for that reason.

### T4 — Model-layer abuse (cost and content)
*Denial of service / Repudiation.* Unbounded model use is a financial DoS.
**Controls:** per-user/project/platform cost caps with hard-block mode; shared
fixed-window rate limits across tasks; every call priced and attributed;
governance purposes are cap-exempt at user scope so the platform cannot starve
its own risk scoring. Guardrail masks sensitive values in prompts.
**Residual risk:** a determined user can still consume their full cap. That is
budget policy, not a vulnerability.

### T5 — XSS via rendered specification content
*Tampering.* Specs and chat render Markdown; diagrams render SVG.
**Controls:** `react-markdown` with no raw-HTML plugin (raw HTML is not
rendered); the single `dangerouslySetInnerHTML` renders Mermaid output produced
with `securityLevel: "strict"`, whose sanitiser resolves to a **patched**
DOMPurify (3.4.12); CSP restricts sources and forbids framing.
**Residual risk:** CSP still allows `'unsafe-inline'`/`'unsafe-eval'` for
scripts because Next.js hydration and Monaco need them without a nonce
pipeline. Tightening to nonces is GA work; recorded as an accepted gap.

### T6 — Supply-chain compromise
*Tampering.* Dependencies and base images are the widest untrusted surface.
**Controls:** dependency audit gate before build with per-advisory documented
exceptions; ECR scan-on-push; SBOM per build; codegen runner uses a vendored
pinned closure with no network installs.
**Residual risk:** three accepted advisories (postcss build-time, sharp removed
from the serving path, dompurify in monaco's read-only viewer) — each with a
re-open trigger in `scripts/audit-allowlist.txt`.

### T7 — Loss or tampering of audit evidence
*Repudiation.* Governance claims are only as good as the trail.
**Controls:** registry-enforced coverage of mutating routes (build fails on a
gap); archive-before-prune invariant with a test that an S3 failure leaves rows
intact and unmarked; exports share the list view's filters.
**Residual risk:** audit rows live in the application database, so a platform
admin with database access could alter them. Append-only external storage
(the §4.6.1 pipeline) remains deferred; the S3 archive gives an off-database
copy for rows past the hot window.

### T8 — Admin account takeover
*Elevation.* Admins approve deployments and set policy.
**Controls:** MFA requirement for admins (platform-enforced), Cognito adaptive
risk available, 1-hour access tokens, suspension effective within one token
lifetime, all admin actions audited, self-lockout prevented (an admin cannot
remove their own factor while the policy requires it).
**Residual risk:** `admin_mfa_required` ships **off** so admins can enrol
without locking themselves out. Turning it on is a one-checkbox administrator
action once every admin has enrolled.

---

## 3. OWASP ASVS L1 self-assessment

Assessed 29 Jul 2026 against ASVS 4.0 Level 1 sections applicable to this
application. This is a self-assessment, not a certification, and it is not a
substitute for an external test.

| Section | Status | Notes |
|---|---|---|
| V1 Architecture | Pass | Documented trust boundaries (this file), threat model, single access seam per resource type |
| V2 Authentication | Pass | Cognito-managed; 12-char minimum with complexity; no credentials handled by the app; TOTP second factor; adaptive risk available |
| V3 Session management | Pass | Stateless JWT with 1-hour access tokens, encrypted server-side session cookie (Auth.js), refresh handled server-side, global sign-out available |
| V4 Access control | Pass | Deny-by-default routes (every route requires a token); role and resource checks server-side; no client-side-only gating |
| V5 Validation & encoding | Pass with note | Pydantic schemas on every input; parameterised SQL only (SQLAlchemy); output encoding via React. Note: CSP allows inline scripts (T5) |
| V7 Error handling & logging | Pass | Structured audit trail, no stack traces to clients, secrets never logged (MFA secrets and prompts excluded from audit detail) |
| V8 Data protection | **Conditional** | TLS, masking, private storage and deletion protection are live; RDS storage encryption is not enabled by the current data stack (ROADMAP.md) |
| V9 Communications | Pass | TLS everywhere; HSTS preload; internal traffic inside the VPC |
| V10 Malicious code | Pass | Dependency gate, SBOM, image scanning, vendored codegen closure |
| V11 Business logic | Pass | Governance gates are server-side and fail closed; maker-checker enforced with segregation of duties |
| V12 Files & resources | Pass | Generated artifacts stored in private S3 with prefix-scoped roles; no user file execution on the platform |
| V13 API | Pass | Consistent auth on all endpoints, method-appropriate verbs, rate limiting at the model seam and the edge |
| V14 Configuration | Pass with note | IaC-defined, least-privilege task roles, no debug endpoints. `pii_redaction` and `admin_mfa_required` ship off; turning them on is an administrator action after TOTP enrollment and a recovery drill |

**Findings raised and resolved during this assessment**
1. Sharp's libvips CVEs were reachable through Next's image optimisation for no
   benefit (one static logo) — optimisation disabled, removing the surface.
2. A duplicate audit-export endpoint was introduced and removed in favour of
   extending the existing one, so filters and caps cannot drift apart.
3. Audit retention originally risked deleting rows whose archive upload had
   failed — the archive-before-prune invariant and its regression test came out
   of this review.

**Open items for an installation going to production**
- Turn on `admin_mfa_required` after every administrator has enrolled TOTP
  and you have drilled recovery (an admin locked out of MFA is a support
  incident).
- Encrypted RDS storage: new installations should set `storageEncrypted`
  before the first deploy; existing ones need a snapshot-copy cutover and a
  repeated restore drill (ROADMAP.md).
- CSP nonce pipeline (removes `unsafe-inline`) — ROADMAP.md.
- An external penetration test has not been performed.

---

## 4. PII redaction: measured cost and the resulting default

The guardrail works. Verified live: a request to echo a test card number came
back without the number, and normal chat was unaffected.

It is not free. Streaming must run in **synchronous** mode — asynchronous mode
would let un-redacted tokens reach the browser before the guardrail verdict,
which defeats the control — and that buffering shows up as time-to-first-token.
Measured back-to-back on the same fleet, same profile, 120 s each:

| | p50 | p95 | vs off |
|---|---|---|---|
| `pii_redaction` off | 1921 ms | 3085 ms | baseline |
| `pii_redaction` on | 2530 ms | 4354 ms | **+32% p50, +41% p95** |

The S14-04 acceptance gate was "under 10% p95 regression". The flag therefore
ships off. An installation that handles regulated data can accept the latency
cost and run `pii_redaction=true` with scope `all` (chat included); the
`non_interactive` scope masks everything except live chat, where the delay is
visible, at the price of leaving chat prompts unmasked.

Caveat on these numbers: single-session sequential chat grows conversation
context as it runs, so both columns sit slightly above the S13 mixed-profile
baseline (2927 ms p95). The *delta* between them is the meaningful figure.
