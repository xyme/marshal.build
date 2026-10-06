# Release notes

What changed, by sprint. This page is the user-facing digest; the
repository's `CHANGELOG.md` carries the release history and
`docs/ENGINEERING_LOG.md` the engineering detail. The build you are on is
shown beside the Alpha badge on your profile and in the admin footer.

## Mid-September 2026 — connectors, composition, and the capability ladder

**Agents can reach registered systems.** Administrators register external
connectors and MCP servers (health-probed, credentials platform-held);
specs then declare "SHALL use connector <slug>" or "SHALL use MCP tools
from connector <slug>" and the generated agent is wired with the
credential delivered at deploy time. Declarations stay inert until the
installation enables connector consumption.

**Agents can call each other.** "SHALL call agent <slug>" / "SHALL
orchestrate agents <a>, <b>" build governed agent graphs: cycles, depth
and unresolved references are refused at save; deployment refuses when a
dependency has no live endpoint; tearing down an agent that others call is
refused naming the dependents.

**The capability ladder.** Spec phrases opt agents into conversation
memory (an expiring per-agent store, torn down with the agent), local
tools (a bounded Converse tool loop over locally implemented functions),
planning (a bounded plan-then-execute loop), and packaged dependencies
(real Lambda packages, exact-pinned, with a recorded name/version/license
manifest — built on the isolated runner, never the platform). Templates
can pin which rungs their projects may use; declared rungs are visible to
risk scoring as a computed factor.

**Bring existing documents.** Projects import from pasted markdown, a
spec-set zip, a public URL, or a **PDF / Word document** (text extracted
verbatim, no OCR, honest refusals); the chat substrate bar grounds a
session on an existing agent's definition with the same formats.

**Hardening and operations.** MFA enrollment produces a scannable QR; the
MFA session gate reads enrollment recency rather than a claim Cognito
never issues; the browser acceptance suite signs in through the real
hosted-UI TOTP challenge; installation portability separated owner pins
from the generic deploy path.

## Early September 2026 — safer builds by default

**Endpoints are protected by default.** Newly generated APIs require an API
key unless requirements deliberately contain `Endpoint authentication:
PUBLIC`. Administrators can forbid public opt-outs entirely. The build and
deployment cards show whether protection came from the platform default, an
explicit requirement, or an explicit public decision. API-key behavior now
works for both inline CloudFormation and full CDK application builds.

**Specification fidelity now has teeth.** Exact field names and literal
status/enum values declared in positive acceptance criteria are carried into
generation and checked before a build can become ready. A deterministic
mismatch blocks the build with the missing names and original criterion;
automated prose-review observations remain advisory because they can be
wrong. Failed builds keep their artifacts and complete report for diagnosis.

## Late August 2026 — deployment modes, integrations, and a workbench

**Deployment modes.** Every deployment now runs as **Full Governance** (the
classic Enclave: platform custody, enforcing risk gate) or **Testbed**: same
pipeline, advisory gate, and the ability to mint limited, time-boxed AWS
credentials for the deployment's account — accept EULAs, raise
configurations, explore your resources — with every issuance security-audited
and everything reclaimed at teardown. Hackathons and sandbox programs run on
Testbed. ([Deployment & Enclaves](/docs/deployment))

**Authenticated endpoints + live key reveal.** A spec can require an API key
on every endpoint; the deployed API enforces it, and the owner can reveal the
key live from the deployment card — the platform never stores a copy.

**A face for your agent.** A spec can ask for a **web test page**: your
deployment serves its own interactive console, so testing (and demoing)
stops being a curl exercise. On keyed APIs the page asks for the key at
runtime and holds it in memory only. ([Code generation](/docs/codegen))

**Integrations.** Service accounts (shown-once tokens, act-only, instant
revocation), webhooks for lifecycle events (signed, retried, loud when
dead), and an org-level Slack/Teams hookup — all under Admin → Integrations.

**Home workbench + global search.** Home now opens with what needs *you* —
reviews owed, deployments expiring, failed builds, changes requested — and
**Cmd/Ctrl-K** searches projects, sessions, samples and docs from anywhere.

**Removed: the in-app Feedback widget** and its admin triage queue. Past
submissions remain in the audit history; talk to your administrator through
your usual channels.

## After Alpha 5 (August 2026)

**marshal.build.** The product dropped the ".AI": it is **marshal.build**,
shortened to **marshal**, always lowercase. Same domains, same tagline —
*Build · Govern · Deploy* — same everything else.

**The Studio opened to power users.** Introduced quietly in Sprint 18,
verified in production, and switched on: power and admin accounts can now
choose the studio when starting a session ([The Studio](/docs/studio)).

**Preview changes before redeploying.** In-place updates gained a changeset
preview: see exactly which resources an update would add, modify or replace —
evaluated against the running stack without touching it.

**Sensitive-data masking got a scope.** Masking measurably slows the first
token in chat, which is why it shipped off. Admins can now apply it to
*everything except live chat* — spec generation, risk scoring, titling — where
the delay is invisible, keeping chat at full speed.

**Fixed:** the admin template editor crashed on open (a dependency upgrade
regression); repaired, and the editor is now checked on every release so it
cannot break silently again.

## Alpha 5 — model flexibility + build experience (S17–S18)

**Sprint 17 — custom model endpoints.** Administrators can register
OpenAI-compatible models hosted outside Amazon Bedrock — on-premise or a
third-party provider — with a connection test, required per-token pricing so
cost caps keep working, and write-only API keys. External models carry an
**External** badge on every picker and in the chat context bar, because
choosing one sends prompts off AWS. If sensitive-data masking is on, it is
applied *before* anything leaves the platform. Failures are loud: an external
endpoint that dies mid-call produces a clear error naming the endpoint, never
a silent fallback to another model. Risk scoring and code generation always
stay on Bedrock regardless of settings.

**Sprint 18 — the studio.** A new build workspace for power users: chat on
the left, the specification evolving in an artifact pane on the right, and a
configuration rail showing the session's model and parameters — including
what the platform will *actually* use after guardrails clamp your request,
with the clamping layer named. A **Prompt context** tab shows exactly what the
platform sends with your next message. Launched quietly, verified in
production, then opened (see above). The guided wizard for Business users
is untouched.

## Alpha 4 — enterprise readiness (S13–S16)

**Sprint 16 — operability & value reporting.** Post-deploy smoke probes now
exercise the routes a generated app declares, not just its front door — a
deploy that comes up but misbehaves is marked *degraded* with a per-route
report on the build card. Administrators get usage analytics (sign-in →
chat → spec → build → deploy funnel, per team, no third-party trackers), a
chargeback report (model + Enclave infrastructure spend by user, project and
team, exportable), and an operations dashboard with alarms. This page and the
build stamp are new, too.

**Sprint 15 — enterprise identity & teams.** Team workspaces arrived: admins
create teams, leads manage what the team can edit, members can read; projects
can be filed into a team without losing personal projects' behavior.
Offboarding now revokes live sessions, strips shared access, and reports owned
projects for transfer instead of touching them. Access reviews export who has
what. In-app documentation (this site), contextual help links, clearer error
messages, an accessibility pass, and email bounce handling round it out.
Enterprise single sign-on (Entra ID / Okta) ships config-gated, pending
identity-provider credentials.

**Sprint 14 — security hardening + compliance evidence.** Web application
firewall and strict content-security policy; optional two-factor
authentication with an admin-enforceable requirement; supply-chain audit gate
and software bill of materials; PII-redaction guardrail (available, off by
default); audit archive + export; written threat model with control mapping.

**Sprint 13 — reliability.** Measured service-level objectives under 5x load;
multi-AZ database with 7-day point-in-time recovery (restore drill: 32
minutes); autoscaling on CPU and request count; an intermittent
error class was root-caused and fixed.

## Alpha 3 — deployment maturity & scale (S9–S12)

**Sprint 12** made scaled-out operation safe and added deployment policies
(concurrency, region, TTL, budget) enforced before any model spend. **Sprint 11** brought
in-place updates with automatic rollback, health probes, auto-expiry with
extend, and deployment history. **Sprint 10** added the CDK artifact profile
with asset-based deploys. **Sprint 9** added support for an
external code-generation engine.

## Alpha 2 — governance & collaboration (S5–S8)

Cost caps with real-time tracking and alerts (S5); the risk review workflow —
score, queue, approve/reject with evidence (S6); collaboration: sharing,
roles, comments, presence, plus marketplace submissions (S7); code generation
from specs with a validation gate and deployable artifacts (S8).

## Alpha 1 — foundation (S1–S4)

Sign-in, projects, and the first deploy path (S1); AI chat that produces
requirements/design/tasks specs with versioned editing (S2); templates with
guardrails, the marketplace, and admin user management (S3); audit logging,
model controls, risk assessment, and guided mode (S4).
