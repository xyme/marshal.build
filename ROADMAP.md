# Roadmap

What is planned for marshal after v0.1.0, and what triggers each item. This
is a projection of the maintainers' backlog, not a commitment or a schedule.
Most items are **evidence-triggered**: they start when a real installation
produces the need, not before. Open an issue with the evidence if you have
it; that is what moves an item up.

Items are grouped by how close they are to being built. Nothing listed here
is gated behind a license or edition: a commercially supported edition may
be offered in future; nothing in this repository is restricted today.

## Near term (release hygiene and the self-hosting path)

| Item | What exists today | What remains |
| --- | --- | --- |
| **Direct-mode IAM permissions boundary for the deployment role** | `MarshalDirectDeployRole` scopes `cloudformation:*` to the generated stacks and denies the control plane's data and its own roles, but `IamForAppRoles` stays blueprint-equivalent: roles a generated template creates carry no permissions boundary or path. | Inject a permissions boundary (and a path condition on `iam:CreateRole`/`iam:PassRole`) for roles created by agent stacks, either deployer-side or in codegen, so an agent's own execution role cannot exceed the deployer. Needed before `direct` is advertised beyond evaluation. |
| **Admin guide publication** | `docs/admin-guide.md` is kept out of the public snapshot (export denylist); the in-app docs cover administration at user level. | Redact the internal banner and installation references, then publish it alongside `docs/ARCHITECTURE.md`. |
| **Finer sizing knobs** | `MARSHAL_TIER=production|evaluate` (v0.1.0) switches RDS Multi-AZ, the one- vs two-task service floors and Container Insights together. | Per-knob overlay overrides (for example Multi-AZ with a one-task floor) if installations ask for a mix. |
| **RDS storage encryption for new installs** | `data-stack.ts` creates the instance without `storageEncrypted`; existing installations need a snapshot-copy cutover to change it. | Default `storageEncrypted: true` for fresh installations; document the cutover for existing ones. |
| **`cdk-app` profile re-drill and enablement** | The CDK application artifact profile is built and was last drilled live on 2 Sep 2026. It requires the `runner` codegen provider, which installations pin to `internal` by default, so the profile is marked experimental. | Re-drill with the current runner image, document enabling the provider per installation, then drop the experimental label. |
| **SPDX license headers** | The license is declared in `LICENSE` and every manifest; source files carry no headers. | Add `SPDX-License-Identifier: Apache-2.0` headers across `backend/app`, `frontend/src`, `infra/lib`, `runner/`, `scripts/` in one change. A welcome first contribution. |
| **CSP nonce plumbing** | `unsafe-eval` is gone; `unsafe-inline` for scripts remains because Next.js hydration and Monaco need it without a nonce pipeline. | Per-request nonce propagation through Next.js and every inline consumer, with browser regression coverage. |
| **First-token deadline follow-through** | v0.1.0 adds a first-token deadline on Bedrock streaming. | Expose the deadline and retry budget as platform settings; surface slow-model state in the UI consistently. |

## When an installation needs it (evidence-triggered)

| Item | What exists today | Trigger and remaining work |
| --- | --- | --- |
| **Multi-organization tenancy** | Tenant columns, a platform-tenant default, a claim seam and selected isolation filters. | Becomes the top priority **before a second organization is placed in one installation**. Needs tenant lifecycle and administration, a complete read/write isolation guard, settings/marketplace/operator-boundary decisions, chargeback roll-up and a two-live-tenant drill. Until then: one organization per installation (run a separate installation per company). |
| **Event / hackathon mode** | Testbed deployments, TTLs, budgets and credential vending are live. | Build only against a booked event: event-code or open registration, preset application, one-click end-of-event sweep, optional gallery/leaderboard. |
| **Quorum review decisions** | Reviewer groups and escalation are live; one reviewer decides. | N-of-M votes with aggregation, partial state, uniqueness, audit and export, and escalation behavior — when a regulated customer requires shared authorization. Open questions: void-on-escalation; high-only vs medium risk. |
| **Model evaluation view** | The endpoint probe checks availability, not quality. | Saved evaluation sets, 2–4 model runs side by side, estimate/budget, latency/cost and human notes — once at least two real model sources are in use. |
| **Semantic marketplace search** | Global search and the marketplace use database `ILIKE`. | pgvector + embeddings write/query paths with a checked-in query quality set — when discovery quality or catalog scale justifies it. |
| **Spec conflict resolution** | Versions, presence and diff make last-save-wins recoverable. | Instrument base-vs-head collisions first; build optimistic 409 + a reconcile UI only if telemetry shows real collisions. |
| **Audit stream to a SIEM** | Postgres + CloudWatch mirror + S3 archive with retention and export. | Firehose → S3 parity and alarms for a named SIEM/evidence requirement; OpenSearch only for a proven cold-search need. Format decision: OCSF vs NDJSON. |
| **E-mail invitations for non-users** | Project sharing targets existing users. | Invitation lifecycle, identity linkage, abuse controls and SES — for demonstrated external collaboration demand. |
| **Container artifact profile (Lambda image, v1)** | Three artifact profiles: `inline-cfn`, `packaged-cfn`, `cdk-app`. The Enclave (account boundary) is the isolation claim; a container is a packaging format. | A fourth profile through the existing seam: Lambda OCI image built on the runner, pushed to an ECR repository inside the Enclave, same gates/IAM/API-key/teardown shape; local `docker run` and image export. Triggers: users asking to run an agent locally, specs needing > 15-minute execution or persistent connections. A Fargate service target only on demand. Cheaper first move: surface the existing handoff bundle ("take this to your account") on the Build tab. |

## On hold until an external dependency exists

| Item | Why it waits |
| --- | --- |
| **GitHub push of generated artifacts** | Needs an organization credential/auth decision (GitHub App vs PAT) before a create/commit/push workflow is built. |
| **CloudFront/OAC hosting for the agent web console** | The same-origin console served by the deployment is sufficient today; a custom-domain/CDN path only when URL/hosting demand outweighs the slower stack lifecycle. |
| **PrivateLink / VPN path for custom model endpoints** | Public authenticated HTTPS endpoints work today; private networking needs a real customer topology and ownership model. |
| **KIRO API integration** | marshal does not integrate with a KIRO product API. The `runner` codegen provider is a generic S3/CodeBuild external-engine contract ([docs/codegen-workspace-contract.md](docs/codegen-workspace-contract.md)) with a reference runner. An adapter and a quality/cost/latency bake-off would follow **when a stable external API with enterprise authentication exists**. |
| **Deploying into arbitrary third-party accounts** | Deferred product-model change. `direct` means the installation account; `isb` means accounts the installation's own Innovation Sandbox vends. A target registry, trust bootstrap and lifecycle/cost/approval model would be a different product. |

## Configuration, not code

These are built and ship in this repository; each needs configuration in
your installation before it does anything. See the README and `docs/`.

- Enterprise SSO federation (Entra ID SAML, Okta OIDC) — `docs/sso.md`.
- E-mail notifications over SES (`EMAIL_ENABLED`, verified identity, DKIM).
- Enclave spend surfacing via Cost Explorer (`COST_EXPLORER_ENABLED`, reader role).
- Slack / Teams chat-ops (incoming webhook under Admin → Integrations).
- Custom OpenAI-compatible model endpoints (Admin → Model Controls).
- Pooled Enclaves via AWS Innovation Sandbox (`SANDBOX_PROVIDER=isb`) — `docs/innovation-sandbox-guide.md`.
- The PII-redaction guardrail and the admin MFA requirement (platform settings, off by default).
