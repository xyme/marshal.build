# Administration

A short orientation to how the platform is governed — what administrators
control, and what that means for your work. Operational detail lives with
the administrators themselves.

## What administrators govern

- **Models** — which models anyone may use, sensible parameter bounds, and
  which (if any) admin-registered **External** endpoints exist. The badge
  follows an external model everywhere; choosing one sends your prompts to
  that provider.
- **Code generation engine** — `internal` (in-process) or `runner`, the
  reference workspace runner behind the published S3 workspace contract;
  `kiro` is accepted as a legacy alias for `runner`. Cloud installations pin
  this by environment.
- **Spend** — caps per user and project and a platform budget. You'll meet
  these as early warnings and, at the cap, clear refusals that name who can
  raise it. Your own usage is on your profile.
- **Deployment policy** — concurrency, deployment lifetime, budgets, and
  endpoint-authentication posture. The deploy panel and refusal messages
  always show the limit that applied.
- **The risk gate** — reviewer groups and the review queue for medium and
  high-risk specifications (see [Governance](/docs/governance)).
- **Templates and the marketplace** — which templates are published and
  which samples are curated (see
  [Templates & Marketplace](/docs/marketplace)). Templates can also pin
  the **capability ladder**: which of memory, packaged dependencies,
  tools and planning their projects may declare, and how many loop
  iterations an agent may run.
- **Integrations** — the connector registry: which external systems and
  MCP servers generated agents may declare, each with its credential held
  platform-side and health-probed. Connector consumption is enabled per
  installation; declarations in specs are inert until it is.
- **Teams and users** — workspaces, membership, roles, personas, and
  suspension/offboarding.

## Transparency guarantees that protect you

- Every administrative change is audited, with before/after detail.
- Nobody — administrators included — can approve specification content that
  has changed since it was reviewed: approvals bind to content.
- Control changes never rewrite history: running deployments keep the
  policies they launched with.
- If sensitive-data masking is enabled on your installation, it is applied
  before model calls — including calls to external endpoints — under a
  scope the administrator chooses.

## If you need something changed

Model access, a raised cap, a longer deployment, a template, or a persona
upgrade all go through your platform administrator — refusal messages name
the control involved so the request is concrete.
