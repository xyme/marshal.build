# Changelog

All notable changes to marshal are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); entries are
newest first. Before v0.1.0 the project was developed privately in sprints
(S1–S18) and dated waves; those sections are summarized from the in-app
release notes. Entry identifiers such as **13.5AA** refer to
[docs/ENGINEERING_LOG.md](docs/ENGINEERING_LOG.md).

## [Unreleased]

## [0.1.0] — 2026-10-07 — first public source release

### Added
- Apache License 2.0 (`LICENSE`, `NOTICE`), `SECURITY.md`, `CONTRIBUTING.md`,
  Contributor License Agreement (`CLA.md` + CLA bot), Code of Conduct, issue
  and pull-request templates, `CODEOWNERS`, `.editorconfig`, `.nvmrc`.
- README rewritten for self-hosting: prerequisites, Bedrock model access, the
  two install tiers (`direct` / `isb`), first-admin creation, first-run notes,
  idle cost floor. `scripts/deploy-env.local.sh.example` lists every overlay
  variable.
- Public documentation set: `docs/ARCHITECTURE.md` (engineering walkthrough),
  `docs/ENGINEERING_LOG.md` (delivery-log excerpt), `ROADMAP.md`,
  `docs/innovation-sandbox-guide.md` (generic `isb` setup guide).
- `scripts/export-public-snapshot.sh`: builds the fresh-history public
  snapshot and runs an identifier gate (account ids, Cognito ids, hostnames,
  certificate ARNs, personal data) before anything is committed.
- First-token deadline on Bedrock streaming calls: a stalled model call no
  longer freezes chat or generation for the full 180 s read timeout.
- `direct`-mode deployments from a cloud install: deployment admission is
  provider-aware. With `SANDBOX_PROVIDER=direct` the policy concurrency
  limits are the capacity and a Deploy is admitted under the same
  transaction lock; the `isb` provider keeps its fail-closed Innovation
  Sandbox pool check. Previously every cloud install without `isb` refused
  Deploy with `provider_capacity_unavailable` (install drill G14). The
  "admissions paused until an admin saves the deployment policy once"
  first-run rule applies to both providers.
- `MARSHAL_TIER` sizing knob (overlay key / CDK context `marshalTier`):
  `production` (default, unchanged sizing) or `evaluate` — single-AZ RDS,
  one-task autoscaling floors and `desiredCount`, Container Insights off.
  Idle floor ≈ $120–125/month instead of ≈ $190–195 (install drill G18).
- Dedicated direct-mode deployment role `MarshalDirectDeployRole`: the
  backend task assumes it once per deployment (CloudTrail session
  `marshal-deploy-<deployment id>`), with `cloudformation:*` scoped to the
  generated stacks and explicit Denies on the control plane's data
  (`ProtectControlPlaneData`) and on the platform's own IAM roles
  (`ProtectControlPlaneRoles`).
- Size escalation (13.5AA): an inline build that fails only on
  CloudFormation's 4 KB `ZipFile` ceiling is re-planned as `packaged-cfn`
  with an empty dependency manifest (stdlib + boto3 only; no third-party
  code enters). Packaged deploys drilled live.

### Changed
- One edition: every capability in the repository is available to every
  installation; nothing is license-gated or entitlement-checked.
- Codegen provider `kiro` renamed `runner` (the S3/CodeBuild workspace
  runner behind `docs/codegen-workspace-contract.md`); `kiro` remains a
  legacy alias. The runner is a generic external-engine contract, not an
  integration with any vendor API.
- The `cdk-app` artifact profile is marked **experimental**: it requires the
  `runner` codegen provider and is not enabled by default.
- Spec-generation budget 300 → 420 s; the project-name call has its own 12 s
  cap with a deterministic fallback (13.5AB).

### Fixed
- 2026-10-05 — The deterministic spec-conformance gate no longer derives
  literal contracts from non-normative requirements text: bullets under
  Assumptions / Out of scope / Not in scope / Non-goals / Exclusions /
  Limitations / Open questions / Future work / Deferred / Risks headings
  (level 2 and deeper; the document title and FR-n headings are exempt) and
  negated sentences ("No …", "does not", "will not", "not in scope", …) are
  skipped. A demo build of a correct Workflow Automation app failed on
  `'notifications'` quoted in an Assumptions bullet. Quoted literals are
  masked before the negation check, so values such as `not-found` still
  produce their contract.
- 2026-10-05 — Claude Sonnet 5 now rejects `temperature` as "deprecated for
  this model"; the Bedrock client treats that like the other optional
  parameter rejections, drops the parameter and retries once. This restores
  risk scoring (and therefore Deploy, which returned `403 risk_error`) on
  templates whose allowed models are Sonnet 5 only.
- Packaged profile (13.5Z): the in-process planner now receives the resolved
  artifact profile (packaged builds were planned with the inline prompt and
  never produced `requirements.txt`); an empty manifest is valid;
  `requirements.txt` is derived from the generated handlers' third-party
  imports instead of model prose; Bedrock `InternalServerException` is
  retried.
- Deleting a chat session that had produced a spec version or run a
  generation returned 500 and left a headless session (13.5AB): both foreign
  keys are now `ON DELETE SET NULL` (migration `a0b1c2d3e4f5`) and the route
  deletes the row before the transcript.
- Generation budgets starved by Sonnet-5 adaptive reasoning (13.5Y):
  `thinking` is disabled at the Bedrock seam for `anthropic.claude*` models
  in both the platform and the runner.
- The test suite needs no AWS credentials (two integration tests stubbed
  Secrets Manager).

### Security
- Supply-chain gate upgraded `pyjwt` and `urllib3` rather than allowlisting
  14 new advisories (13.5AB). gitleaks secret scanning and Dependabot added
  to CI.

## Mid-September 2026 — connectors, composition, and the capability ladder

### Added
- Agents can reach registered systems: administrators register external
  connectors and MCP servers (health-probed, credentials platform-held);
  specs declare "SHALL use connector <slug>" / "SHALL use MCP tools from
  connector <slug>" and the generated agent is wired with the credential at
  deploy time. Declarations stay inert until the installation enables
  connector consumption.
- Agents can call each other: "SHALL call agent <slug>" / "SHALL orchestrate
  agents <a>, <b>" build governed agent graphs; cycles, depth and unresolved
  references are refused at save; deployment refuses when a dependency has no
  live endpoint; teardown of an agent others call is refused naming the
  dependents.
- The capability ladder: conversation memory (an expiring per-agent store),
  local tools (a bounded Converse tool loop), planning (a bounded
  plan-then-execute loop) and packaged dependencies (real Lambda packages,
  exact-pinned, with a recorded name/version/license manifest built on the
  isolated runner). Templates can pin which rungs their projects may use;
  declared rungs feed risk scoring as a computed factor.
- Import from a PDF or Word document (text extracted verbatim, no OCR,
  honest refusals), in addition to pasted markdown, spec-set zip and public
  URL; the chat substrate bar accepts the same formats.
- A fleet-wide **Deployments** page (the nav item previously pointed at a
  page that did not exist).

### Changed
- MFA enrollment produces a scannable QR; the MFA session gate reads
  enrollment recency rather than a claim Cognito never issues; the browser
  acceptance suite signs in through the real hosted-UI TOTP challenge.
- Installation portability: owner pins separated from the generic deploy
  path (`scripts/deploy-env.local.sh` overlay, `--bootstrap` first run,
  domainless mode on the CloudFront default domain).

## Early September 2026 — safer builds by default

### Changed
- Endpoints are protected by default: newly generated APIs require an API key
  unless requirements deliberately contain `Endpoint authentication: PUBLIC`;
  administrators can forbid public opt-outs entirely. Build and deployment
  cards show where the decision came from. Works for inline CloudFormation
  and full CDK application builds.
- Specification fidelity has teeth: exact field names and literal status/enum
  values in positive acceptance criteria are carried into generation and
  checked before a build can become ready; a deterministic mismatch blocks
  the build naming the missing names and the original criterion. Failed
  builds keep their artifacts and complete report.

## Late August 2026 — deployment modes, integrations, and a workbench

### Added
- Deployment modes: **Full Governance** (platform custody, enforcing risk
  gate) and **Testbed** (same pipeline, advisory gate, time-boxed limited AWS
  credentials for the deployment's account, every issuance audited,
  everything reclaimed at teardown).
- Authenticated endpoints with live key reveal from the deployment card; the
  platform never stores a copy of the key.
- Web test page: a spec can ask for an interactive console served by the
  deployment itself; on keyed APIs it asks for the key at runtime and holds
  it in memory only.
- Integrations: service accounts (shown-once tokens, act-only, instant
  revocation), signed and retried webhooks for lifecycle events, org-level
  Slack/Teams hookup.
- Home workbench (reviews owed, deployments expiring, failed builds, changes
  requested) and global search (Cmd/Ctrl-K).

### Removed
- The in-app Feedback widget and its admin triage queue; past submissions
  remain in the audit history.

## August 2026 — after Alpha 5

### Changed
- The product is **marshal.build**, shortened to **marshal**, always
  lowercase (previously "marshal.AI").
- The Studio opened to power and admin users.
- In-place updates gained a changeset preview evaluated against the running
  stack without touching it.
- Sensitive-data masking got a scope: admins can apply it to everything
  except live chat, keeping chat at full speed.

### Fixed
- The admin template editor crashed on open after a dependency upgrade; the
  editor is now checked on every release.

## Alpha 5 — model flexibility and build experience (S17–S18)

### Added
- Custom model endpoints (S17): administrators register OpenAI-compatible
  models hosted outside Amazon Bedrock with a connection test, required
  per-token pricing and write-only API keys. External models carry an
  **External** badge everywhere; masking, if on, is applied before anything
  leaves the platform; failures are loud and never fall back silently. Risk
  scoring and code generation always stay on Bedrock.
- The Studio (S18): chat on the left, the specification evolving in an
  artifact pane on the right, and a configuration rail showing the model and
  parameters the platform will actually use after guardrails clamp the
  request, plus a Prompt-context tab.

## Alpha 4 — readiness (S13–S16)

### Added
- S16: post-deploy smoke probes exercise the routes a generated app declares;
  usage analytics (sign-in → chat → spec → build → deploy funnel, per team,
  no third-party trackers); chargeback report by user, project and team;
  operations dashboard with alarms; release notes page and build stamp.
- S15: team workspaces with leads and members; offboarding that revokes live
  sessions, strips shared access and reports owned projects for transfer;
  access-review export; in-app documentation and contextual help; an
  accessibility pass; e-mail bounce handling. Enterprise single sign-on
  (Entra ID / Okta) ships config-gated.
- S14: web application firewall and content-security policy; optional
  two-factor authentication with an admin-enforceable requirement;
  supply-chain audit gate and SBOM; PII-redaction guardrail (available, off
  by default); audit archive and export; written threat model with control
  mapping.
- S13: measured service-level objectives under 5× load; Multi-AZ database
  with 7-day point-in-time recovery (restore drill: 32 minutes); autoscaling
  on CPU and request count; an intermittent error class root-caused and
  fixed.

## Alpha 3 — deployment maturity and scale (S9–S12)

### Added
- S12: safe scaled-out operation; deployment policies (concurrency, region,
  TTL, budget) enforced before any model spend.
- S11: in-place updates with automatic rollback, health probes, auto-expiry
  with extend, deployment history.
- S10: the CDK artifact profile with asset-based deploys.
- S9: support for an external code-generation engine (the workspace runner).

## Alpha 2 — governance and collaboration (S5–S8)

### Added
- Cost caps with real-time tracking and alerts (S5); the risk review
  workflow — score, queue, approve/reject with evidence (S6); collaboration:
  sharing, roles, comments, presence, marketplace submissions (S7); code
  generation from specs with a validation gate and deployable artifacts (S8).

## Alpha 1 — foundation (S1–S4)

### Added
- Sign-in, projects and the first deploy path (S1); AI chat that produces
  requirements/design/tasks specs with versioned editing (S2); templates with
  guardrails, the marketplace and admin user management (S3); audit logging,
  model controls, risk assessment and guided mode (S4).

[0.1.0]: https://github.com/xyme/marshal.build/releases/tag/v0.1.0
