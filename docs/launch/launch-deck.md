---
marp: true
theme: default
class: invert
paginate: true
size: 16:9
title: marshal.build — Build · Govern · Deploy
description: Launch deck for the open-source release of marshal.build
style: |
  section.invert { background-color: #0f172a; color: #e2e8f0; }
  section.invert h1, section.invert h2 { color: #f8fafc; }
  section.invert h3 { color: #818cf8; }
  section.invert code { color: #c7d2fe; }
  section.invert a { color: #818cf8; }
  section.cover { display: flex; flex-flow: column nowrap; justify-content: center; text-align: center; }
  section.cover img { border-radius: 28px; }
  section.hero { display: flex; flex-flow: column nowrap; justify-content: center; text-align: center; }
  section.hero p { font-size: 1.3em; }
  section.dense { font-size: 22px; }
  section.dense pre { font-size: 0.8em; }
---

<!-- _class: invert cover -->
<!-- _paginate: false -->

![w:170 h:170](../../Marshalogo.png)

# marshal.build
### Build · Govern · Deploy

**Describe an AI agent in plain English. Get a governed specification, a validated serverless agent, and a live deployment in an isolated AWS account — with every decision on the record.**

Open source · self-hostable · built in the open since July 2026

<!-- Thirty seconds. The tagline is the product: three stages, one platform, nothing hidden. -->

---

## 2 · The gap

- Everyone can prompt a model into writing an agent. Almost nobody can **deploy** one their security team will sign off on.
- What's missing between the prompt and production: a specification someone approved, code that was *checked* rather than *trusted*, a blast-radius boundary, a risk decision with a name on it, an audit trail of every model call and every click.
- Today that gap is filled by shadow AI, or by six weeks of platform work per team.

<!-- Name the audience's pain before the product. The security reviewer is the real customer. -->

---

<!-- _class: invert dense -->

## 3 · What marshal does — one picture

```
 Chat / Guided wizard / Studio
        │  (PDF · DOCX · pasted specs as grounding)
        ▼
 Spec set: requirements.md · design.md · tasks.md   ──►  versions · diff · rollback · comments
        │
        ▼
 Code generation ──► deterministic validation gate ──► spec-conformance review
        │                 (allowlist · packaging · auth contract · guardrail patterns)
        ▼
 Risk scoring (7-factor rubric) ──► auto-approve / maker-checker review
        │
        ▼
 Enclave: a vended AWS account per deployment · TTL · budget · health · teardown
```

<!-- Build is the top half, Govern is the middle, Deploy is the bottom. Every arrow is a seam with a test behind it. -->

---

<!-- _class: invert dense -->

## 4 · Build — from conversation to specification

- **Three ways in:** guided wizard (business users — server-owned state machine, ≤3 clarification rounds), freeform chat, and the **Studio** for power users (shows the exact prompt context and *effective* parameters after clamps — display cannot drift from enforcement).
- **Brownfield grounding:** attach a PDF, a Word document, or an existing spec set; extraction is deterministic and local (no model, no OCR).
- **Output:** requirements, design (with a Mermaid architecture), implementation plan — structurally validated, one model repair pass, then append-only versions with diff and rollback.
- **Templates with rails:** model allowlists, token ceilings, temperature bounds, architecture rules, allowed capabilities — enforced server-side on every call.

<!-- Demo beat #2 lives here. Emphasise "rails, not suggestions". -->

---

<!-- _class: invert dense -->

## 5 · Build — from specification to a validated agent

- **Profiles:** `inline-cfn` (zero toolchain), `packaged-cfn` (real Lambda package, S3 asset, exact-pinned deps with an SBOM), `cdk-app` (a TypeScript CDK repository you can take away — experimental).
- **The gate is deterministic and blocking:** AWS service allowlist, packaging shape, API-key contract on every route, forbidden patterns (hard-coded credentials, public DB exposure), deploy contract. Any finding fails the build — no "deploy anyway".
- **Spec conformance:** literal/field contracts extracted from the requirements are checked in the generated code; violations block, model review is advisory.
- **Self-correcting:** compaction retry for oversized handlers; automatic escalation to packaged when code outgrows CloudFormation's 4 KB inline limit — *without* adding dependencies the spec never declared.
- **Handoff bundle:** specs + generated code + build manifest — run it outside marshal.

<!-- The gate is the thing competitors don't have. "Checked, not trusted." -->

---

<!-- _class: invert dense -->

## 6 · Govern — the risk gate and maker-checker

- Every deployable build is scored against the **current** content hash: 7 weighted factors (data sensitivity, external reach, capability reach, …), rubric v3, deterministic per content — scored once, reread forever.
- **Low auto-approves. Medium/high route to reviewers** — groups, escalation, SLA queue, resubmission lineage; the decision is recorded with the reviewer's identity and comment.
- Tightened policy means the next deploy re-scores; approvals under a superseded policy don't open the gate. Running deployments are never retro-blocked.
- Admins tune weights, bands and auto-approve policy — **versioned**, every change audited.

<!-- Demo beat #5: show a medium verdict, switch to the admin, approve with a comment, show it in the audit log. -->

---

<!-- _class: invert dense -->

## 7 · Govern — policies, controls, evidence

- **Deployment policy:** concurrency per user / platform, TTL default and max, per-deployment budget, region, endpoint-auth posture (`always`). Enforced *before* scoring — refusals never spend a model call.
- **Model controls:** allowlist from a governed catalog, parameter bounds, per-user / per-project / platform monthly caps (block or alert), rate limits with queue-then-429.
- **Audit:** every action (before/after, request id, IP) **and every model invocation** (prompt/response hashes, tokens, cost, stop reason) in one timeline; 180-day hot, then S3 archive. Reasoning tokens suppressed so spend is honest.
- **Security:** Cognito + TOTP MFA, admin-MFA gate (fail-closed), PII-redaction guardrail in synchronous streaming mode, WAF, body/rate guards, supply-chain gate on every image build.

<!-- Don't claim certifications — there are none. Describe the controls; let the reviewer map them. -->

---

<!-- _class: invert dense -->

## 8 · Deploy — Enclaves

- A deployment gets **its own AWS account**, vended by AWS Innovation Sandbox: lease, budget cap, recycling. The account boundary *is* the blast-radius control for model-written code.
- Lifecycle on the record: pre-flight → lease → stack → health probes → active; TTL with 48 h/24 h warnings; owner-extend within policy; automatic teardown through the standard path; in-place updates with CloudFormation rollback semantics.
- **Keyed by default:** API key + usage plan on every route; optional self-contained web test console; "Reveal key" for the owner only.
- Fleet view across projects; dependents block teardown; connector and dependency secrets copied *into* the Enclave and deleted with it.

<!-- Demo beat #6. If the live deploy is slow, narrate the lease phases — they're the point. -->

---

<!-- _class: invert dense -->

## 9 · The capability ladder (each rung spec-declared, gate-enforced)

| Phrase in the requirements | What the platform builds |
|---|---|
| "SHALL keep conversation memory" | per-agent DynamoDB memory table + verbatim helpers |
| "SHALL use tools: a, b" | platform-assembled tool loop; model writes only tool bodies |
| "SHALL plan multi-step responses" | bounded planning loop, rail-capped iterations |
| "SHALL use connector \<slug\>" / MCP tools | registered connectors, copy custody, MCP tool loop |
| "SHALL call agent \<slug\>" / orchestrate | composable agents with a resolved dependency graph |
| "SHALL use packaged dependencies" | exact-pinned package with SBOM |

Templates decide which rungs a project may declare. Nothing is chosen silently.

---

<!-- _class: invert dense -->

## 10 · Architecture

Browser → CloudFront + WAF → Next.js → FastAPI. FastAPI ↔ Postgres; ↔ DynamoDB (chat, shared state); → Bedrock seam (clamps · redaction · recording) → Amazon Bedrock; → Codegen (in-process, or the CodeBuild workspace runner) ↔ S3 workspace/artifacts; → Risk gate + review; → Innovation Sandbox (Enclave vending) → Enclave account running the CloudFormation stack.

- Two backend tasks, shared state in DynamoDB, Postgres LISTEN/NOTIFY event relay, elected schedulers.
- Every enforcement point is a **seam**: `resolve_models`, the Bedrock seam, the audit route registry, the deployment pre-flight, `SandboxProvider`, `CodegenProvider`.

<!-- Render this as boxes and arrows. -->

---

<!-- _class: invert dense -->

## 11 · Built in the open — engineering discipline

- **616** backend tests · **95**-check headless-browser smoke against production on every deploy · TypeScript strict · `cdk synth` in CI.
- **Supply-chain gate** on every image build (pip-audit + npm audit, SBOM evidence) — it refused a deploy this week over 14 fixable advisories; we upgraded instead of allowlisting.
- **29 recorded delivery waves**: every defect, root cause, drill and decision written down with the evidence — the recent entries ship publicly as `docs/ENGINEERING_LOG.md`.
- Live since **25 July 2026**; Multi-AZ Postgres with PITR; zero-downtime Alembic migrations; every wave live-verified before it's called done.

<!-- This slide earns trust with engineers. Read one ENGINEERING_LOG.md entry aloud if time allows. -->

---

<!-- _class: invert dense -->

## 12 · What's open today and how to run it

- **One edition, everything included** under Apache-2.0. A hosted/commercial edition is planned; nothing here is crippled.
- **You need:** an AWS account, Bedrock model access for the default models, Node.js 22 + Python 3.12 + uv + AWS CLI v2 + CDK. Two tiers:
  - **Evaluate** — `direct` mode: deploys agents into the installation account. One account, no Innovation Sandbox.
  - **Operate** — Enclave mode: install AWS Innovation Sandbox (guide included), get an account per deployment.
- `./scripts/deploy-cloud.sh` — seven steps, fail-closed smoke at the end. Honest monthly floor in the README: **≈ $120–125/month** (evaluate) · **≈ $190–195/month** (production).
- Docs: in-app user guide (7 pages), `ARCHITECTURE.md`, `ROADMAP.md`, `CHANGELOG.md`, `docs/ENGINEERING_LOG.md`.

<!-- Say the price. Say the prerequisites. Nobody forgives a surprise NAT bill. -->

---

<!-- _class: invert dense -->

## 13 · Honest limits and roadmap

- **Not a KIRO API integration.** The external codegen engine is our own S3/CodeBuild workspace runner behind a published workspace contract; a real engine can replace the image.
- **Single organisation per installation** today (multi-org tenancy groundwork exists; activation is roadmap).
- Chat stalls are bounded to ~45 s with automatic retry when the model provider stalls.
- `cdk-app` profile needs the runner provider enabled; treat as experimental.
- Roadmap: multi-org, event/hackathon mode, container artifact profile, commercial edition, KIRO API when one exists.

<!-- Limits stated by us beat limits discovered by them. -->

---

<!-- _class: invert hero -->

## 14 · Live demo

**Expense Receipt Triage Agent** — 12 minutes, Build → Govern → Deploy, ending with a real `curl` against a real Enclave.

---

## 15 · Get involved

- Repo: `github.com/xyme/marshal.build` · `CONTRIBUTING.md` · `SECURITY.md`
- Good first contributions: a template, a marketplace sample, a connector adapter (the SharePoint adapter is the reference shape), a doc fix found during your install.
- Hosted beta: private, by invitation — the code is the product today.
