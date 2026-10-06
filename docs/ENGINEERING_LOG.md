# marshal — Engineering log (public excerpt)

This is a **redacted excerpt of the internal delivery log** (entries 13.5T
through 13.5AB, 17–30 September 2026, newest first). The internal log is the
dated as-built record: what was built, what broke live, the root cause, the
fix and the evidence. Entry identifiers (**13.5X**) are stable and are
referenced from code comments, commit messages, [`CHANGELOG.md`](../CHANGELOG.md)
and [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

Redactions in this excerpt: AWS account ids → `<account-id>`, Cognito
identifiers → `<cognito-pool-id>`, hostnames → `<host>`, beta-tester names
→ "a beta tester", and sentences about the private beta cohort's legal,
cost and gate status. Engineering facts are unchanged. Backend test counts
are the suite size at the time of each entry.

---

### 13.5AB Generation budget starved by a cosmetic call; chat-session delete 500 + zombie rows; supply-chain gate caught pyjwt/urllib3 (30 Sep 2026)

**Generation ("Generation exceeded 300s budget", an administrator's project).**
Root cause from the invocation rows: the 30-token project-NAME call
(`purpose=title`, fast model) hit the 180 s Bedrock read timeout, retried,
and succeeded at **183 s** — 61% of the set budget spent before the first
document. requirements 53 s + design 59 s followed; tasks.md was killed 4 s
in. Healthy sets take 131–146 s (30-day p50–max). The same stalled call
answered conversationally and the project was literally named *"I don't see
any attached specs in your message. However, based on your request f"*.
Fixes: `TITLE_TIMEOUT_S = 12` (`asyncio.wait_for`) with a deterministic
fallback (`fallback_project_name`: session title unless default, else the
first five words of the opening message); `valid_project_name` accepts only
name-shaped answers (one line, ≤6 words, no sentence punctuation);
`NAME_SYSTEM` told never to explain or refuse; **`GENERATION_TIMEOUT_S`
300 → 420** so ONE stall on a document call survives a large three-doc set
(live arithmetic 180 + 53 + 59 + 45 = 337 s). Troubleshooting doc gained the
"regenerate only the missing document" guidance. The affected project's
requirements/design v1 had been saved; tasks regenerates alone.

**Session delete (found in the same log window).** `DELETE /chat/sessions/
{id}` 500'd with `ForeignKeyViolation` for ANY session that ever produced a
spec version or ran a generation: `specs.session_id` (S1) and
`spec_generations.session_id` (S2) were bare FKs, and the route deleted the
DynamoDB transcript BEFORE the failing row delete — a headless "zombie"
session that rendered empty. Migration **`a0b1c2d3e4f5`** (first head move
in weeks): both FKs → `ON DELETE SET NULL`, `spec_generations.session_id`
nullable, constraint names resolved at runtime (originals were unnamed).
Route: detaches provenance explicitly (also the only enforcement on SQLite
test engines), deletes the row + commits FIRST, then drops the transcript in
a try/except (orphan transcripts are reapable; zombie rows are not).
`GenerationOut.session_id` Optional; `_run_generation`/rehydrator
null-guard a session deleted mid-flight. **Zombie sweep (live, one-off):**
4 sessions carried provenance refs; exactly ONE had an empty transcript —
deleted; its requirements/design specs stay on the
project. Live schema verified post-deploy: both delete rules `SET NULL`,
column nullable, `alembic_version = a0b1c2d3e4f5`, upgrade line in the boot
log.

**Ride-on: supply-chain gate refused deploy 1** (S14-03 doing its job): 14
fixable advisories published since the last deploy — `urllib3 2.7.0`
(3 CVEs, transitive via botocore) and `pyjwt 2.13.0` (11 CVEs, direct, on
the Cognito verification path). Upgraded in the lock (pyjwt 2.15.1,
urllib3 2.8.0) rather than allowlisted; 586 tests green on the upgraded auth
library; gate PASS locally and in CodeBuild on deploy 2.

**Validation.** Backend 586 (+7: budget/title pins, fallback derivation,
stall-uses-fallback, chatty-title rejection, delete-after-generate detaches
provenance, row-survives-transcript-failure). ui-smoke +6 (`s8ab`: message →
single-doc regenerate → DELETE 204 → 404 → project specs survive) — bar 95.
RDS PITR recovery point noted pre-migration (Multi-AZ, 7-day retention).

### 13.5AA Size escalation (inline → packaged, empty manifest); packaged DEPLOY drilled live; a beta tester's build completed (24 Sep 2026)

**Decision (reversing 13.5V's "never chosen silently" for the SIZE case
only).** When an in-process inline build still fails on NOTHING but
CloudFormation's 4 KB ZipFile ceiling after the compaction pass, the runner
re-plans it as `packaged-cfn` with an **empty dependency manifest**. No
third-party code enters — the same stdlib + boto3 code ships as an S3 asset
instead of inline text — so the supply-chain rationale behind the spec
phrase is untouched: "SHALL use packaged dependencies" remains the ONLY door
to actual libraries. Mechanics: `_generate_artifact_set` (plan → files →
template, extracted so it runs once normally, twice on escalation);
`_is_size_only_failure`; the build row's `artifact_profile` flips (the
deployer keys asset staging on it); inline artifact rows are cleared;
`BuildCtx.size_escalated` selects stdlib-only packaged prompts
(`PLAN_SYSTEM_PACKAGED_STDLIB` / `FILE_SYSTEM_PACKAGED_STDLIB`, 11k-char
handlers); `SizeEscalationDependencyError` → named `packaged_dependencies`
finding if the re-plan imports anything third-party (no CodeBuild spend);
`_finalize` treats the platform as the packaged declarer for the symmetry
gate and writes `manifest.packaged = true` (the template capability rail
and deployer read it), `manifest.size_escalation {from,to,reason,oversized,
resolved}`; phase stream announces it. Requires the workspace bucket —
without it, the 13.5Z finding stands. In-process builds only (v1).
`BUILD_TIMEOUT_S` 480 → 720 (two generation passes + packaging).

**Drill 1 — inline full-governance (unplanned proof).** The escalation drill
spec built INLINE: compaction rescued both oversized handlers this time
(3707/2526) — generation variance is real, which is why the tester's retries
sometimes "almost" worked. Then: risk medium 34 → pending → admin approve
(TOTP-minted token) → deploy → active/healthy 93 s → keyed 403 → teardown.

**Drill 2 — packaged DEPLOY (the backlog item "on first real
declaration").** Spec WITH the phrase → handlers 7231/9631/7119 → the model
chose `rapidfuzz==3.9.7` (MIT) → derived manifest pinned it → pip on the
runner → **13.7 MB zip**, SBOM-lite name/version/license → READY → the
deploy gate re-scored the current content medium 32 (rubric v3
`capability_reach` +3 for the DECLARED packaged rung; the earlier cached
read had shown low 28 from a pre-declaration hash) → pending → admin
approve → deploy →
**"Staging 1 asset(s) into the Enclave"** → `PackageBucket/PackageKey`
bound → **active, healthy in 85 s** → keyed endpoint 403 without key →
teardown → torn_down. Maker-checker loop, asset staging, custody: all live.

**The beta tester's project — READY** (started server-side via
`runner.start_build` attributed to the tester with an explicit admin audit
row `build_started_on_behalf`; admins hold no implicit project access). Trace: inline plan → aor_handler/chat_handler oversized →
compaction 4694/… still over → **escalation** → packaged re-plan (3 handlers
**10,460 / 13,266 / 13,284** chars — the app's real size) → empty manifest,
`packages: []` → CodeBuild → 37 KB zip → gate 0 findings → conformance
8 met / 0 violated → ready. No spec change. Two truths from the trace:
(a) `size_escalation.resolved` stayed False on the READY build — marker
was set pre-pass and never flipped; fixed in `_finalize` (True only when
the gate passes) + pin; (b) escalated handlers exceeded the prompt's
"under 11000" guidance — that number is guidance, not a gate (Lambda's real
limit is the zip), harmless, noted.

**Validation.** Backend 579 (+4 escalation pins: rebuilds-as-packaged with
artifact replacement + planner told the profile/posture; refuses
third-party imports by name without CodeBuild spend; unavailable without
runner infra → 13.5Z behavior; mixed findings never escalate). Docs:
codegen.md + troubleshooting.md teach the escalation; finding copy names it.
Five deploys across 13.5Z/AA; ui-smoke 89/89 on the final image.

### 13.5Z Live defect chain: over-ceiling inline builds pointed at an impossible exit; the packaged escape hatch had never actually run (beta-tester report: "build tab still not working … 6358 chars — over the 4000-char inline ceiling", 23–24 Sep 2026)

**Symptom → root causes (four layers, each found by running the previous fix
live).** After 13.5Y cleared token starvation, a beta tester's project
reached the validation gate and failed `inline_packaging` three
times: 6 handler generations at **4675–6358 chars** against CloudFormation's
4096-byte `ZipFile` limit, the one-pass compaction rescue fired every time
(manifests prove it) and came back 6357/5092 against a 3400 target. The app
is genuinely too large for inline — the advice mattered, and it was wrong:

1. **Finding copy pointed at cdk-app**, which on this installation
   (`provider=internal`) is a 409 wall (needs the workspace runner). The
   packaged profile — internal-provider, 11k-char handlers, S3 assets, no
   ceiling — was never mentioned. Fixed: copy distinguishes "slightly over →
   rebuild" from "genuinely large → declare *SHALL use packaged
   dependencies*", names cdk-app's provider requirement; troubleshooting doc
   aligned; pins updated.
2. **Probe of that advice failed: `requirements.txt was not generated`.**
   The in-process runner called `provider.plan(ctx)` WITHOUT the resolved
   profile, so every packaged build planned with the INLINE prompt
   (3200-char handlers, stdlib-only) and never force-inserted the manifest.
   The external runner passed it; the `CodegenProvider` Protocol omitted the
   kwarg, so the call site and every test stub conformed while being wrong.
   Fixed: call site passes `build.artifact_profile`; Protocol carries
   `profile`; stubs updated; pipeline pin asserts what the planner was told.
   13.5V had unit-tested resolution, gate and packaging separately — never
   the in-process plan call with a resolved profile.
3. **Next probe: the model narrated prose into `requirements.txt`**
   ("Looking at the handlers described…") and the contract refused an EMPTY
   manifest — yet the tester's app (and most packaged-for-SIZE apps) is
   stdlib + boto3 and needs exactly that. Fixed: empty manifest valid
   (`pip install -r` of an empty file is a no-op); `requirements.txt` is now
   **derived** — `third_party_imports()` over the generated handlers via
   `sys.stdlib_module_names` + runtime-provided boto3/botocore; zero model
   calls when nothing is third-party; otherwise one 300-token pin-only call
   whose answer is filtered by `pin_lines_only()` (prose can never become a
   deliverable).
4. **Ride-on: Bedrock `InternalServerException` crashed a build with zero
   retries** minutes after a `ServiceUnavailableException` exhausted three —
   same outage, two codes, one unhandled. Added to `RETRYABLE` in both seams.

**Live proof (fourth deploy).** A same-shaped project WITH the declaration →
`packaged-cfn` resolved → handlers **4549 / 8375 / 7655 chars** (all far past
4000, all accepted) → `requirements.txt` 0 bytes, no manifest call → CodeBuild
packaging `11656e49…` → **package.zip 20,879 B** (sha256 `fcfcde52…`,
`packages: []`) → gate 0 findings → conformance 12/12 met → **READY** in
165 s, 6 codegen calls. Backend 575 tests (+7: planner-profile pin,
empty-manifest contract, import derivation, pin filtering, no-call
manifest, ISE retry ×2). ui-smoke 89/89 on deploys 1 and 4.

**Instruction to the tester:** add "The system SHALL use packaged dependencies"
to the requirements document and rebuild. The finding they see now says so.

### 13.5Y Live defect: fixed codegen budgets starved by sonnet-5 adaptive reasoning — seam-wide suppression; chat→build handoff CTA (beta-tester report: "build failed … template.json assembly hit the token cap", 18 Sep 2026)

**Root cause (evidence-first).** `us.anthropic.claude-sonnet-5` emits
`reasoningContent` blocks BY DEFAULT; they bill as outputTokens while both
seam paths extract text blocks only and no marshal surface renders them.
Fixed-budget calls therefore starve nondeterministically: the tester's six
failed builds were five plan-phase deaths (2000-token budget; one live row
burned all 2000 producing **zero text characters**) and one assembly death
(8192 tokens holding 9020 chars ≈ 3k tokens of JSON — the rest was invisible
reasoning). The same spec passed planning once at 810 tokens — adaptive
means per-request, which is why "retry the build" sometimes worked. Proven
by container probe: baseline → `[[reasoningContent],[text]]`;
`additionalModelRequestFields={"thinking":{"type":"disabled"}}` → `[[text]]`.

**Fix.** `additional_request_fields()` in `services/bedrock.py` pins
thinking OFF for `anthropic.claude*` in BOTH `converse` and
`stream_converse`, with `_thinking_field_rejected` fallback (families that
refuse the field get one retry without it — they never reasoned anyway);
`runner/main.py` `HeadlessBedrock` mirrors the policy (same generation
code, R2.3 hard budget now pays for code, not deliberation). This also
stops silently billing reasoning on EVERY purpose (chat, specgen, risk,
title). **Ride-on discovery during round-1 verify:** a transient Bedrock
`ValidationException` "The provided model identifier is invalid" killed a
build on call 3 of 5 after two clean calls with the same id (16-call
hammer: no repro) → `_transient_invalid_model` retry with backoff in both
seams; genuinely wrong ids still fail after MAX_ATTEMPTS pre-billing.

**Handoff CTA (tester feedback: "can't proceed to build through chat").**
Intended flow confirmed (chat authors specs; builds start on the workbench
Build tab, S8-04) but no authoring surface SAID so: guided wizard's
done-screen now shows "start your build" → `?tab=build` after Approve &
Save (or on revisit of a saved session); SpecPanel and Studio
ArtifactColumn gain "Start a build →"; approve-button emerald-600→700
(white-on-600 at 12–14px is 3.69:1, surfaced by axe once the spec panel
rendered inside the /chat scan).

**Deploy + verify.** Two rounds (round 1 exposed the transient flake, a
build-state-dependent smoke assertion, and the contrast defect). Live:
seam probe `end_turn` @1012 tokens on the deployed image; real s8 build
READY with 4/4 codegen calls `end_turn`, chars/token 1.7–2.7 (pre-fix
assembly: 1.10); ui-smoke **89 checks PASSED** (+3 handoff pins); axe
/chat clean; backend 569 tests (+11: thinking pins, rejection fallback,
transient-retry bounds, runner mirrors). Images stamped
`cf34bc31e039-dirty`; the next clean SHA supersedes.

### 13.5X Live defect: nav "Deployments" was a dead label — real page shipped (report: "the deployment tab in the UI is not working even for admin", 17 Sep 2026)

**Root cause (S15-era).** The shell nav's "Deployments" item pointed at
`/projects` — a tour-anchor label with NO page behind it, plus an
active-state exemption hack (`item.label !== "Deployments"`) that kept it
from ever highlighting, which is what masked it: clicking it landed on My
Projects and looked like a no-op. Reproduced headlessly before fixing:
`/deployments` was a hard 404 for every persona; the PROJECT-page
deployment tab was healthy for both power and admin (zero page/console/API
errors), so the report was the nav surface. Neither the smoke nor axe ever
visited the nav target — the b14 check covered the workbench ENDPOINT only.

**Fix (commit `39a5b59`).** (1) `GET /users/me/deployments` — cross-project
list under the workbench/projects-list visibility clause (owned ∪
explicitly shared ∪ team; the aggregate shows nothing the project pages
would not), newest-first, capped 50, name-joined, admin-security-gated
(the S14 shared-path class). (2) `/deployments` page — fleet view: running
/in-flight + history tables, status/health chips, mode, lifetime
(hours-left on active rows), app link, "shared" badge; deploy/teardown
ACTIONS deliberately stay on the project's own deployment tab. (3) Nav
repointed to the real page; the exemption hack removed. **Regression
pins:** ui-smoke now loads the page (heading, HTTP 200) AND the endpoint —
the dead-link class cannot return quietly. Tests appended to
test_workbench.py (visibility incl. stranger-invisible, ordering, empty).

**Deploy + verify.** Clean SHA `39a5b59011b4`; all seven steps green;
healthz attempt 1; backend 558 tests; frontend 24 pages (`/deployments` in
the route table); full ui-smoke **86 checks PASSED** — the new page served
23 real history rows live.

### 13.5W User-docs milestone pass, specification/build-log split, docs deploy (17 Sep 2026)

**Docs tab.** Seven user-facing pages now carry the September milestones:
substrate grounding section (chat-and-specs), `packaged-cfn` profile row +
spec-declared-only rule (codegen), the two computed reach factors named in
the risk-gate description (governance), Integrations registry + template
capability-rail bullets (admin), capabilities-rail resolution item
(marketplace), three named-refusal entries (troubleshooting:
capability_not_allowed, PDF/Word import refusals, dependents-block-
teardown). The internal release-notes digest gained its mid-September
section; the page stays OFF the registry (13.5C) and the smoke's 404 pin
held.

**The delivery log.** The dated 13.5A–V entries and the Sprint-1 evidence
moved out of the internal specification document into the dedicated
delivery log (identifiers stable — "13.5X" references across code comments
and commits resolve to that log; newest first; new entries prepend at the
top). The sanity pass also refreshed the backlog table (none of its open
rows shipped in the September wave; triggers stand) and the operational
cross-references.

**Deploy + verify (commit `53e8b8d`, the first clean-SHA stamp:
`GIT_SHA=53e8b8d2d8ad`).** All seven script steps green; rollouts on fresh
digests (backend `sha256:ccaaffbe…`, frontend `sha256:a023276c…` matching
ECR); ten alarms 0 non-OK; healthz attempt 1; no migration (smoke's
migrations-in-sync check green). ui-smoke gained FOUR permanent
docs-content pins (one phrase per updated page — a docs page that renders
but lost its content now fails the bar): **84 checks, SMOKE PASSED** —
the pins verified the milestone content live behind auth, where curl
cannot reach (docs pages 307 to sign-in unauthenticated).

### 13.5V Triple build wave: PDF/DOCX ingestion, agent-substance ladder complete, SharePoint adapter code, memory drill (17 Sep 2026)

**PDF/DOCX ingestion SHIPPED** (spec 13.5U, all tasks; the go covered the
dependency gate — `pypdf==6.19.0` exact-pinned, advisory-clean, zero
transitive deps). One magic-byte dispatcher (`docs_from_bytes`) serves
import upload (`document_b64` XOR member), import URL (Content-Type still
IGNORED by design) and chat substrate (`content` XOR `document_b64`);
DOCX rides a stdlib zipfile+expat walk (spec-set zips win the PK
disambiguation; only `word/document.xml` is ever decompressed), PDF rides
pypdf with encrypted/page-cap/no-text-OCR/corrupt refusals named exactly;
extraction early-stops at the existing 400k/200k ceilings; binary ceiling
1.5MB everywhere. Frontend: "Upload file" tab (.zip/.pdf/.docx),
SubstrateBar pending-chip binary flow (deliberate Attach preserved — no
client-side preview for binary, recorded UX note). Tests appended to
test_marketplace.py (the import tests' real home — the spec named
test_integrations.py; corrected, not both) + test_studio_backend.py.
ui-smoke gained the AC-6 drill: an in-script stored-zip .docx imports
through the API and the extracted phrase lands as requirements; the
unsupported-format refusal is pinned by name.

**Agent-substance ladder COMPLETE (tasks 2–6; task 1 was 13.5T, task 7 was
13.5Q).** *Packaged profile (R1):* `packaged-cfn` joins the artifact
profiles, selected ONLY by "SHALL use packaged dependencies" (template
defaults excluded; request conflicts refuse loudly — R1.2 "never chosen
silently"). Generation stays in-process; ONE packaging CodeBuild job rides
the EXISTING S9 runner (`MARSHAL_MODE=package` — **pip never runs on the
control plane**): exact-pin requirements (`name==version`, ≤8, PIN_RE),
deterministic zip (sorted entries, fixed timestamps, 45MB cap), SBOM-lite
manifest (name/version/license via importlib.metadata — the S14-03
discipline on generated agents) stored at `manifest.package` + as a
browsable artifact; the binary zip lands at `artifacts/<id>/package.zip`
(S3-backed artifact row). Deploy stages it under the two fixed
PackageBucket/PackageKey parameters (plain key — the CDK '||' convention
stays cdk-only via a per-asset flag); teardown deletes staged assets;
structural validation runs BEFORE the packaging spend. Deviation: v1 is
internal-provider-only (cdk-app's inverse). *Tools rung (R2.2):* "SHALL
use tools: <a>, <b>" (≤6) → a platform-ASSEMBLED handler — imports + the
exact TOOLS_JSON line + the verbatim Converse tool-loop driver are fixed
bytes (byte-prefix gate), the model writes ONLY the local tool function
bodies; v1 tools take one {"input": string} argument. *Planning rung
(R2.3):* "SHALL plan multi-step responses" → fully verbatim
plan-then-execute scaffold (byte equality). Both loops carry
template-rail-capped iteration envs (`loop_caps` findings) +
bedrock:InvokeModel IAM checks + symmetric undeclared refusals; MCP/tools/
planning are mutually exclusive v1 ("one conversation loop per agent" —
compose agents to combine); the MCP gate now names inline-cfn explicitly.
*Template rail (R3.2, AC-4):* `guardrails.capabilities.
allowed_capabilities` (absent = all) + `max_loop_iterations` (1–8);
enforced at ALL FOUR requirements write seams beside the composition sync
(named 422 `capability_not_allowed`) AND at deploy preflight against the
CURRENT rail (a rail tightened after a build refuses the stale build by
name); admin create/update validates the blob. *Risk (R3.1, AC-5):*
`capability_reach` seventh computed factor, RUBRIC_VERSION 3 (one bump
for the whole ladder, as deferred in 13.5T), weights fill generalized
(COMPUTED_OPTIONAL_FACTORS); score table 0 none / 2 memory / 3 packaged /
5 loop / 6 loop+packaged / +1 memory-combo, cap 8. Pinned arithmetic
rebased HONESTLY: 25→23 and 62→57 with the fixture reband high→medium —
the dilution is real and admins tune bands, not the code. Found+fixed en
route: the policy no-op detector compared normalized weights raw — two
proportional fills disagree at 1e-17, so a no-op PUT would have bumped
policy_version and re-scored the world; weights now compare rounded (9dp).

**SharePoint adapter CODE COMPLETE** (spec 13.5S tasks 1–3;
registration/drill need an operator-hosted adapter, an Entra app with
Sites.Selected grants and a test tenant). `adapters/sharepoint/`: FastAPI MCP surface speaking
exactly the platform's dialect (initialize 2025-03-26 /
notifications/initialized 202 / tools/list / tools/call, mcp-session-id
issued+echoed, plain-JSON responses the SSE-tolerant clients accept);
static-key auth on every request (configurable header, Bearer-tolerant);
Graph client-credentials token cache (single-flight, 5-min refresh
margin); four read-only tools with the spec'd caps (2MB fetch / 40k chars,
50/20 listing caps) and honest refusals (no-OCR boundary; 403 mapped to a
Sites.Selected hint); per-call site allowlisting with a drive→site trace
for direct fetches (defense in depth above the grants). Dockerfile
(slim + Lambda Web Adapter — one image, any host), template.yaml (Lambda
container + function URL, secrets via dynamic references from
`marshal/adapters/sharepoint`; `AuthType: NONE` is deliberate — the
adapter enforces its own key, which IS the registered credential;
cfn validate-template green), README (gates, registration, rotation).
Deviation: the adapter gets its own NEW test file (6 green, mocked Graph)
— a standalone component has no existing suite to append to.

**Memory-rung live drill EXECUTED (the ride-on-event; AC-1 closed).**
Through the platform API end-to-end on the freshly deployed images:
project `fa067db9…` ("SHALL keep conversation memory" + web console) →
build `9ed03f8d…` ready with `manifest.memory=true` → deployed ACTIVE to
lease account `<account-id>` → behavior proven live: turn 2's reply counted
turn 1 (`note_count: 2`), recall returned both turns newest-first,
`other-session` recalled NOTHING (session scoping), `/app` console served;
the revealed key exercised the keyed contract → teardown `torn_down`,
endpoint dead (connection refused post-delete). Two drill-path notes:
Cognito access tokens live 60 min (first attempt expired mid-prep), and
the frontend `/api/backend` proxy accepts only browser sessions or `mat_`
service tokens BY DESIGN — the drill drove the backend from inside the
container via ECS exec (localhost + JWT), phase-scripted.

**Deploy + verify (all three images).** CodeBuild SUCCEEDED stamping
`GIT_SHA=6d5a1e075fc3-dirty`; rollouts COMPLETED (backend
`sha256:a81bccf3…`, frontend `sha256:b668cf9b…` matching ECR `:latest`;
runner `sha256:a79e24d4…` fresh — the packaging mode ships); the deploy
script ran ALL SEVEN steps this time (no step-6 stall; landing +
healthz passed attempt 1); alembic head UNCHANGED `e9f0a1b2c3d4` (zero
migrations across the whole wave — confirmed in-database); ten alarms 0
non-OK. Backend suite 530→556 green; ui-smoke 77-check run PASSED on the
new images mid-wave; the final run adds the AC-6 import checks. Rubric v3
re-scores each project's content once on its next assessment (determinism
key includes the version — by construction, not a stampede).

### 13.5U PDF/DOCX ingestion spec written (17 Sep 2026)

**Deliverable (writing only, build gated).**
The ingestion spec closes the I3 "separate OPEN decision"
— today every document seam is UTF-8-only and the URL-import refusal
string already name-drops the gap. v1 = deterministic LOCAL extraction
(no model calls, no OCR): PDF via `pypdf` (pure Python, exact-pinned, the
single new dependency), DOCX via a STDLIB zipfile+etree walk of
`word/document.xml` (python-docx rejected — lxml native dep against the
slim image; the C3/TOTP hand-roll precedent). One magic-byte dispatcher
serves all three existing entry seams: import upload (new `document_b64`
XOR member, base64-in-JSON house pattern — no multipart), import URL
(Content-Type stays ignored; a .docx's PK header is disambiguated from a
marshal spec-set zip before the archive branch refuses it), and chat
substrate (`content` XOR `document_b64`, extracted text stored under the
existing 200k cap). Binary ceiling 1.5MB everywhere (2,000,000 base64
chars = the URL fetch cap exactly); extracted-text ceilings are the
existing 400k/200k with early-stop; hostile-input guards named per format
(encrypted/page-cap/no-text→OCR-refusal for PDF; single-member
decompression + 16MiB cap + expat amplification protection for DOCX).
Governance is provenance-blind by construction — extracted text lands
exactly where pasted text lands (same risk trigger, composition sync,
gates) — and the spec RECORDS that redaction posture is unchanged
(at-rest verbatim, egress-time only), with the SharePoint clause repeated:
any future ingest-time platform call carrying content makes redaction
parity blocking. No migration, no infra change, no flag. Gates: build go
+ pypdf dependency approval (~1 session).

### 13.5T Ready-to-go wave: smoke-admin TOTP, memory rung, installation portability (17 Sep 2026)

**Smoke-admin TOTP.** smoke-admin@marshal.demo now carries a
PLATFORM-HELD TOTP seed: associate-software-token issued the secret straight
into Secrets Manager `marshal/smoke/admin-totp` (never printed),
verify-software-token accepted a locally computed code (proving the stdlib
RFC-6238 implementation against Cognito itself), and MFA preference is
SOFTWARE_TOKEN_MFA. ui-smoke computes codes (`totpCode()`, stdlib HMAC-SHA1)
and `answerTotpChallenge()` hooks every hosted-UI sign-in, firing only for
the smoke admin. **Lesson (first full run, one failure):** Cognito TOTP
codes are SINGLE-USE — the template-admin block's sign-in and s18's landed
in the same 30-second window, so Cognito rejected the second code as reuse
and the challenge page never redirected (waitForURL timeout). Fix:
`freshTotpCode()` tracks the last-used window and sleeps into the next one
before answering. **Full re-run: 77 checks, SMOKE PASSED** — the same bar
as 13.5R's green run, now exercising the live hosted-UI MFA challenge on
every admin sign-in. The browser acceptance survives the
`admin_mfa_required` flip by construction.

**Memory rung shipped (agent-substance task 1; first agent-substance
build).** Spec phrase "SHALL keep conversation memory" → `requires_memory`;
generated stacks gain `AgentMemoryTable` (keys `session_id` + `sk`, TTL
`expires_at`) with least-privilege IAM; handlers carry VERBATIM
remember/recall helpers (byte-checked, the C1/C3 conformance idiom);
`_memory_findings` gate holds the contract BOTH ways (table+env+helpers iff
declared, absent iff not); the web console passes a stable session id
through so memory works from the demo surface; docs phrase line + tests
appended (`test_codegen.py`). **Deviation (recorded):** no per-rung risk
bump — one `capability_reach` factor lands when the substance ladder
completes (RUBRIC_VERSION stays 2; avoids re-scoring the world per rung).

**Installation portability shipped; this wave's deploy IS the acceptance test.**
`deploy-env.sh` rewritten generic: sources a GITIGNORED
`scripts/deploy-env.local.sh` overlay first (installation pins live
there; the tracked file carries none), derives Cognito/CloudFront from live stack
outputs when unpinned, generates `.origin-verify` under
`MARSHAL_BOOTSTRAP=1`, warns-not-fails on `OPS_ALERT_EMAIL`.
`deploy-cloud.sh`: `--bootstrap` first-run mode (creates `.auth-secret` +
the frontend auth secret, defers the identity cross-check),
`EXPECTED_ACCOUNT_ID` REQUIRED from env (no baked account), ISB values
required only when `SANDBOX_PROVIDER=isb`. `app-stack.ts`: domainless mode
(no cert/domain → CloudFront default domain end-to-end incl. AUTH_URL);
SANDBOX_PROVIDER/COST_EXPLORER_ENABLED/COST_READER_ROLE_ARN env-driven.
`bin/marshal.ts` gates the marketing stack on `MARKETING_CERT_ARN`.
Support mailto rides `NEXT_PUBLIC_SUPPORT_EMAIL` (build-arg → env chain).
Blueprint `ControlPlaneAccountId` lost its baked Default (12-digit
AllowedPattern; **next StackSet op must pass UsePreviousValue=true**).
`.env.example` files for both halves (frontend/.gitignore gained a
`!.env.example` negation — the `.env*` rule was silently swallowing the
example). README replaced install-oriented; installation-specific operations notes
moved to a maintainer-internal document. **Acceptance evidence (identical
behavior through the refactored path):** preflight derived `context
verified: account=<account-id> region=us-east-1 pool=<cognito-pool-id>`; every stack synthesized to **"(no changes)"**
against the installation's live CloudFormation — the overlay-driven path is
byte-equivalent where it must be; domainless zero-env `cdk synth` and the
marketing-stack gate verified pre-deploy. Validation: backend 530 green,
frontend 23 pages, infra tsc, shell syntax.

**Deploy + live-verify (images carry all three tracks).** CodeBuild
SUCCEEDED stamping `GIT_SHA=f905fe0c9c9f-dirty`; both rollouts COMPLETED on
fresh digests (backend `sha256:57336a14…`, frontend `sha256:60787666…`,
each matching ECR `:latest`); alembic head UNCHANGED `e9f0a1b2c3d4` (no
migration this wave; confirmed in-database via ECS exec); ten alarms 0
non-OK; landing 200; healthz status=ok database=ok. The deploy script's
step-6 stability poll stalled (pre-existing behavior, observed on earlier
waves too) — stability, digests and the step-7 probes were verified
directly (`aws ecs wait services-stable` + identical curl checks by hand).
