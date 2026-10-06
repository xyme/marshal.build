# Code generation

The **Build** stage turns an approved specification into a deployable
AI agent — an LLM-powered service behind an API. Builds run from the
project's **Build** tab and never require a local toolchain.

## Starting a build

**Start build** generates the agent from the current specification and
assembles the deployable artifact. Progress streams file by file; a running
build can be cancelled. One build runs per project at a time.

## Artifact profiles

| Profile | What you get | When to choose it |
|---|---|---|
| `inline-cfn` (default) | A single CloudFormation template with the agent code embedded inline | Fastest path; focused agents; zero toolchain |
| `packaged-cfn` | A CloudFormation template whose agent code ships as a real Lambda deployment package (S3 asset), optionally with third-party Python dependencies | When handlers outgrow the inline size ceiling, or the agent needs libraries beyond stdlib + boto3 — the dependency manifest is derived from the generated code and may be empty |
| `cdk-app` | A complete TypeScript CDK v2 application — `bin/`, `lib/`, handler sources, lockfile — synthesized to a template with packaged assets **(experimental: requires the workspace-runner codegen provider enabled by an admin)** | Larger agents (no inline size ceiling); when you want a repository you could maintain yourself |

Templates can set a project's default profile between `inline-cfn` and
`cdk-app`. Third-party dependencies are unlocked only by the spec itself
("*SHALL use packaged dependencies*" — see the capability phrases below),
never silently. One automatic exception exists for *size alone*: when an
inline build fails on nothing but the inline code ceiling, the platform
re-plans it as a packaged deployment with an **empty** dependency manifest
— the same stdlib + boto3 code, shipped as an S3 asset. The build console
announces it and the manifest records `size_escalation`. Dependency freedom stays bounded either way: `cdk-app`
builds work against a **pinned dependency set** (a build that reaches
outside fails with a `dependency_closure` finding), and `packaged-cfn`
requires exact pins with every build recording a name/version/license
dependency manifest.

## The validation gate

Nothing reaches deployment without passing validation, and the gate always
reports **all findings at once** so you fix in one pass, not one error per
attempt. What it checks:

- **Template integrity** — the artifact parses as valid CloudFormation.
- **Resource allowlist** — every resource type must be on the platform's
  allowed list. A generated agent that reached for an unapproved service names
  the resource and the offending type.
- **Packaging rules** — inline-profile size ceilings, correct handler/runtime
  shapes; the `cdk-app` profile has its own packaging checks.
- **Deployment contract** — the stack must expose an `ApiUrl` output; that is
  how the platform health-checks and smoke-tests what it deploys.
- **Guardrail patterns** — hard-coded credentials and publicly exposed
  database access are rejected wherever they appear in the generated source.
- **Dependency closure** (`cdk-app`) — only the pinned dependency set may be
  used.
- **Spec fidelity** — positive acceptance-criteria field names and literal
  values are preserved exactly. Missing deterministic contracts block the
  build; automated prose review remains advisory and is labelled as such.

Findings appear on the build card with the artifact path and reason. Typical
fixes are specification-level: state that secrets come from a secret store,
steer the architecture toward allowed services, or switch profile when inline
code outgrows its ceiling.

## Endpoint authentication and the test console

Every newly generated API is **key-protected by default** in both artifact
profiles. You do not need to add an authentication sentence to get the safe
posture. If—and only if—the agent is deliberately public, put this
exact line in requirements:

> `Endpoint authentication: PUBLIC`

A casual mention of a public audience does not opt out. Administrators may
set an organization policy that refuses public builds entirely. Contradictory
public and key-required statements fail the build until the author chooses
one intent.

The deployment owner reveals the generated API key from the deployment card
(owner-only and audit-logged); send it as the `x-api-key` header. The key
lives only in the deployment account, and teardown is revocation.

To add a **web test console**, write "*the app SHALL provide a web test page*".

To call a **registered connector** from the generated agent, write "*SHALL use
connector <slug>*" (using the slug from the administrator's connector
registry). The build wires the call and the deployment receives the
credential automatically. This signal is active only when your installation
has connector consumption enabled — otherwise the phrase is ignored.

To generate a **tool-using agent**, write "*SHALL use MCP tools from
connector <slug>*" where the slug names an MCP-server connector. The agent
discovers that server's tools at startup and reasons over them in a bounded
loop (one MCP connector per agent). The same enablement switch applies.

To **compose agents**, write "*SHALL call agent <slug>*" (the slug shown on
the target project's page) — the generated agent calls that deployed agent,
and the platform wires the endpoint and key at deploy time. For a pipeline
over several agents, write "*SHALL orchestrate agents <a>, <b>, <c>*".
Dependencies must be deployed before the caller deploys; cycles and chains
deeper than two levels are refused at save.

To give the agent **conversation memory**, write "*SHALL keep conversation
memory*". The agent gets its own expiring memory store, recalls earlier
turns for the same session, and its web test page keeps a session going
automatically. Memory is torn down with the agent.

To give the agent **local tools**, write "*SHALL use tools: <a>, <b>*"
(snake_case names, up to six). The agent gets a bounded Converse tool loop
over exactly those tools — each implemented as a local function in the
generated code, reviewed like any other artifact. External reach stays
connector/MCP territory; tool code that only computes locally needs none.

To make the agent **plan multi-step responses**, write "*SHALL plan
multi-step responses*". The agent breaks a request into a bounded number of
steps, executes them in sequence, and composes the final answer. One
conversation loop per agent: MCP tools, local tools and planning are
mutually exclusive (compose agents to combine them).

To ship **third-party Python dependencies**, write "*SHALL use packaged
dependencies*". The build produces a real Lambda deployment package on the
build runner (never on the platform itself): dependencies must be exact
pins (`name==version`, up to eight), and every build records an auditable
dependency manifest — name, version, license — alongside the artifacts.
Without this declaration builds stay inline: small, dependency-free,
instantly readable.

Cost and risk stay honest across the ladder: memory tables, packages and
loop invocations bill inside the deployment's sandbox account under the
same per-deployment budget as every other resource, and declared rungs are
visible to risk scoring (the `capability_reach` factor) — a tool-using
agent scores differently from a static one under your organization's
policy.

Organization templates may **pin the capability ladder**: a template's
`guardrails.capabilities.allowed_capabilities` names which of `memory`,
`tools`, `planning`, `packaged` its projects may declare (absent = all
allowed), and `max_loop_iterations` caps the tool/planning loops (1–8).
A forbidden declaration is refused when the requirements document is saved,
imported or generated — by name, never as a deploy-time surprise.
The build ships a self-contained interactive page at **`/app` on the same URL
as your API**. The page itself loads without a key so you can paste one; it
holds the value in page memory and sends it only to business routes. Keys are
never embedded in artifacts.

Both postures are validation contracts: keyed builds must include a working
key/usage-plan/stage binding and protect every business method; explicit
public builds must not accidentally retain key protection. Console builds
must keep storage private and may exempt only the deliberate `/app` loader.

## Artifacts, bundles, and where code lives

Ready builds expose their full artifact set in the build tab's browser —
template, source files, README — with per-file and build-level content
hashes. Two downloads:

- **Handoff bundle** — a working repository: the agent service at the root,
  your `.kiro/specs/<slug>/` documents alongside, synth outputs for
  `cdk-app`, and a README with the exact commands (`npm ci && npx cdk
  deploy`) to run it entirely outside the platform.
- **Individual artifacts** — from the browser, including binary assets.

Generated artifacts are never hand-edited on the platform: the specification
is the source of truth, and changes flow spec → regenerate → new build. This
keeps every deployed artifact reproducible from its documents.

## Cost attribution

Build-time model usage is metered and priced like all other usage, attributed
to the user who started the build and to the project budget. Builds refused
by a cost cap or rate limit fail before spending anything — the cheapest
check always runs first.

## External generation engine

Platform administrators can switch which engine generates code (Model
Controls → Code generation). Two providers exist: `internal`, which runs
Bedrock synthesis in-process, and `runner`, an out-of-process reference
engine that runs on CodeBuild behind the published S3 workspace contract —
any engine that speaks the contract can replace it. `kiro` is accepted as a
legacy alias for `runner`. The build experience, validation gate and
artifacts are identical from your side; the build card shows the engine that
produced each build.
