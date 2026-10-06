# Business and Power experiences

marshal adapts to how much control you want. Your persona is set during
onboarding and changeable from your profile.

## Business

Guided, opinionated, fewer decisions.

- The chat asks you structured questions rather than expecting you to know what
  to specify.
- Specifications are generated and shown read-only — you review rather than edit.
- Templates and marketplace samples are the normal starting point.
- Deployment is a single action; the details are handled for you.

Use this if your interest is the outcome rather than the architecture.

## Power

Full control over the specification and the build.

- Direct editing of `requirements.md`, `design.md` and `tasks.md`, with
  versioning, diffs and rollback.
- The **Studio**: chat with the specification evolving alongside and the
  session's model, temperature and token budget adjustable in a rail that
  shows what the platform will *actually* use after guardrails apply
  ([The Studio](/docs/studio)).
- Model and parameter choice within the platform's allowlist and any template
  bounds — including admin-registered models hosted outside Bedrock, badged
  **External**.
- Build profile choice, including the full CDK application profile.
- Changeset preview before in-place redeploys.
- Repository bundle download to continue outside the platform.

Use this if you would otherwise write the specification yourself.

## Switching

Profile → persona. Moving from Business to Power may require admin approval
depending on your organisation's configuration; you keep the Business
experience until it is approved.

## What does not change

Governance applies identically to both. The same risk gate, the same cost caps,
the same audit trail, the same deployment policies. A Power user cannot bypass
an approval a Business user would have needed — the gate keys on the
specification's content, not on who wrote it.
