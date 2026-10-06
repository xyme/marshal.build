# Templates & Marketplace

Two libraries with two different jobs. **Templates** are governance you start
*inside*: they constrain and scaffold a session before the first message.
**Marketplace samples** are finished examples you start *from*: complete
specifications you fork and make your own. This page covers both in depth.

## Templates

A template packages three things for a class of agent:

- **Model guardrails** — which models a session may use, a per-call token
  ceiling, and temperature bounds. These are *rails*, not suggestions: they
  are enforced server-side on every model call the session makes.
- **Scaffolding** — a starter prompt that pre-fills the composer, and a list
  of sections the requirements document must include (a compliance template
  can insist on "Data retention" and "Audit requirements", for example).
- **Identity** — name, category, description, and a version number.

### How guardrails actually apply

When a templated session calls a model, the platform resolves the *effective*
configuration in one place:

1. **Models** — the platform allowlist and the template's allowed models are
   intersected. A model must pass both. If the intersection is empty, the
   session refuses with a message naming which layer emptied it — the
   platform side, or the template — so the fix is obvious.
2. **Token ceiling** — the lower of the platform's ceiling and the
   template's. A template pinning `max_tokens` very low will truncate long
   design documents; the editor warns about values under the generation
   budget.
3. **Temperature** — the template's band narrows first, then platform
   bounds. In the [Studio](/docs/studio) rail you see requested and
   effective values with the clamping layer named — for example
   *"0.9 → clamped to 0.7 by template 'FS Regulated'"*.
4. **Capabilities** — a template may pin which agent capability rungs its
   projects can declare (`memory`, `packaged`, `tools`, `planning` — see
   [Code generation](/docs/codegen)) and cap the tool/planning loop
   iterations (1–8). A rail left unset allows everything. A forbidden
   declaration is refused **when the requirements document is saved,
   imported or generated** — by name, naming the template — never as a
   deploy-time surprise; deployment re-checks the current rail so a
   tightened template also holds against older builds.

The template also speaks to the assistant: its name, intent and required
sections are injected into the system prompt, and the assistant is
instructed to explain constraints politely when you ask for something the
template forbids — refusal with a reason, not a mystery.

You can see all of this live in the studio's **Prompt context** tab: the
template block appears verbatim as the platform sends it.

### The template lifecycle

| Status | Meaning | What is allowed |
|---|---|---|
| **Draft** | Being written | Edit freely; delete; not visible to users; cannot start sessions |
| **Active** | Published | Users see it in the new-session picker; sessions enforce it; editing guardrails **bumps the version** |
| **Deprecated** | Retired | No new sessions; existing work keeps functioning; cannot be republished |

Publishing has a gate of its own: a template must configure at least one
guardrail category (model guardrails with one or more allowed models) before
it can go active — an empty template would be scaffolding pretending to be
governance.

Two consequences of the lifecycle worth knowing:

- **Version bumps are automatic and honest.** Editing an *active* template's
  guardrails increments its version, so "which rules was this built under?"
  always has an answer. Name and description edits don't bump.
- **Deprecation is not destruction.** Existing sessions and forked projects
  keep working; the template simply leaves the picker. Deleting is only
  possible for drafts — a template that ever governed real work is kept as a
  record.

### Templates and the model allowlist

Template rails intersect with the *current* platform allowlist at call time,
not at authoring time. If an admin later removes a model platform-wide, a
template naming it simply loses that option — and if that empties the
template's choices, sessions say so explicitly. Admin-registered **External**
endpoints can appear in template rails like any other model id, badge and
all.

## Marketplace

The marketplace is the platform's library of working examples: curated sample
agents with full specifications you can read, learn from, and fork.

### Browsing

Samples carry a category, complexity, the models they use, keywords and a
description; search and filters narrow the catalog. Every published sample
shows its complete specification set — requirements, design, tasks — in a
read-only viewer, so you judge the substance before forking. Contributed
samples credit their author.

### Forking

**Fork** copies a sample's specification into a brand-new project of your
own — independent, fully editable, and governed like anything you wrote
yourself (the risk gate scores it on first deploy; forking itself is free of
approvals).

Fork-time warnings surface staleness before it costs you anything:

- the sample was built with a **deprecated template** — your fork still
  works, but new sessions cannot adopt that template;
- the sample references **models outside the current allowlist** — you will
  be steered to allowed ones when you generate.

The fork is a starting point, not a subscription: later changes to the
sample never touch your project, and your edits never flow back.

### Contributing your own

Power users can submit a project to the marketplace from the project page:

- A submission **snapshots** your specification at submit time — edits you
  make afterwards provably do not leak into what reviewers or future
  readers see.
- **One open submission per project** at a time; withdraw while it waits if
  you change your mind.
- An admin reviews the queue: approval moves the snapshot into the curation
  pipeline (metadata, then publication); rejection comes with written
  feedback, delivered verbatim.
- Resubmissions link to their predecessor, so the review reads as a lineage.
- You are notified at each step, and **Contributed by** credits you on the
  published sample.

Unpublished submissions are never publicly visible — drafts, queue items and
rejected snapshots exist only for you and the reviewing admins.

## For administrators

Both libraries are curated under **Admin**:

- **Templates** — the editor's three tabs (General, Model Guardrails, YAML —
  the YAML view is the whole template as configuration, handy for review),
  draft → activate → deprecate lifecycle, and per-template usage counts.
- **Marketplace** — draft → published → archived lifecycle, metadata
  editing, spec import from any project, and the submissions queue.

The [administration overview](/docs/admin) sketches the operational side; the
[governance guide](/docs/governance) explains how template rails relate to
platform-wide controls.
