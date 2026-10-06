# The Studio

The studio puts your specification beside the conversation instead of behind a
tab, with the session's model and parameters visible where you work. It is the
power-user build surface; the guided wizard for Business users is unchanged.

## Opening it

Three ways in:

- **New session** → choose **Studio** instead of Classic chat (power and admin
  accounts see the choice; your last pick is remembered).
- From an existing chat session, **Open in Studio ↗** above the thread.
- Finishing the guided wizard offers **Open in Studio** alongside the usual
  outcome.

**Open in Chat** in the studio header takes you back — the same session opens
in either surface, nothing is duplicated or converted.

## The layout

Chat on the left; on the right an artifact pane with three tabs, and the
configuration rail.

### Spec tab

The three documents (`requirements.md`, `design.md`, `tasks.md`) with a
switcher, per-document generation progress, and the same regenerate and
approve actions as chat. When a generation runs, documents fill in live as
they finish.

The pane refreshes when the platform actually changes the spec — generation
completing, a save, a rollback. It deliberately does **not** re-derive the
spec after every chat message: that would slow chat and spend your budget
on every turn of conversation. Instead the pane is
honest about staleness: *"spec generated 14:02 · 3 messages since"* means the
conversation has moved since the documents were written. **Refresh** re-reads
them on demand; **Generate spec** brings them up to date with the
conversation.

### Prompt context tab

Exactly what the platform will send with your next message: the assistant's
persona framing, the template's guardrail block if the session has one, the
clamped parameters, and the model id. Read-only, never calls a model. If a
generation behaves unexpectedly, look here first — the answer is usually a
template constraint you could not otherwise see.

### Export tab

The existing export actions in one place: the specification zip, per-document
downloads, and — once a CDK build is ready — the repository handoff bundle.
Nothing new to learn — they do exactly what they do on the project page.

## The configuration rail

- **Model** — pick from what the platform and your template allow. Models
  hosted outside Amazon Bedrock carry an **External** badge; selecting one
  means your prompts leave AWS for that provider (the chat context bar shows
  the same badge as a reminder).
- **Temperature and max tokens** — set what you want; the rail shows what the
  platform will actually use. When a bound narrows your request you see both
  values and *which layer* clamped it, for example
  *"0.9 → clamped to 0.7 by template 'FS Regulated'"*. The values shown are
  the values used.
- **Guardrail chips** — 🔒 marks what your admin or template fixed (you can
  see it, not loosen it); the rest is yours to adjust.

Changes persist to the session immediately and are audited like any other
session update. The context bar and rail always agree — they read the same
state.

## What the studio does not change

Governance is identical to chat: same risk gate, same cost caps, same rate
limits, same audit trail. The studio re-surfaces existing capabilities; it
grants none. Business personas keep the guided wizard and do not see the
studio — that separation is a product decision, not a missing feature.
