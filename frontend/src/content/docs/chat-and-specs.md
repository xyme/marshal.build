# Chat & specifications

The specification is the platform's central artifact: everything downstream —
code generation, risk assessment, deployment — works from it. This page covers
how conversations become specifications and how specifications are managed.

## Sessions

A session is one conversation with the assistant. Sessions are listed in the
left sidebar on **Chat**; each remembers its full history, its template (if
any), and its model configuration. Sessions attached to a shared project are
visible to that project's editors — with the creator's name — so a colleague
can pick up where you stopped.

Two modes:

- **Freeform** (power and admin) — open conversation. You steer.
- **Guided** (everyone; the only mode for Business personas) — a structured
  wizard: three form steps (use case, context, behavior), then the assistant
  asks clarifying questions in capped rounds — at most **3 rounds and 15
  questions total** — so the wizard always lands. Questions you skip become
  explicit *assumptions* in the requirements document rather than silent
  guesses. Power users can switch a guided session to freeform mid-way;
  finishing the wizard offers **Open in Studio**.

Starting from a **template** narrows the session before the first message:
the template's allowed models and parameter bounds apply, and its starter
prompt pre-fills the composer. The context bar at the bottom of the chat
shows the governing template and the model in use.

## What the assistant knows and does

The assistant works to understand your agent — the problem, users,
data, key actions, constraints — and asks targeted clarifying questions
rather than monologuing. Two honesty rules hold throughout: assumptions are
stated and labelled as assumptions, and architecture discussion only ever
references real AWS services the platform allows.

Every reply streams token by token. The context bar tracks the approximate
token size of the conversation. If a reply is cut off by a rate limit or cap,
the partial text is kept and marked truncated — nothing disappears.

## Grounding on an existing agent (substrate)

Migrating something that already exists? The **substrate bar** above the
chat attaches your current agent's definition — system prompt, tool list,
config, sample transcripts — as ground truth for the whole session. Paste
it, or read it from a file: text formats load directly, and a **PDF or Word
.docx** has its text extracted server-side (no OCR — scanned documents are
refused honestly). With a substrate attached, the assistant treats it as
the current state to rebuild from, and every generated document is grounded
on it, not just the chat turns. The session shows only the substrate's
presence, source label and size; replace or remove it at any time. One
substrate per session, up to 200KB of text.

## Generating the specification

**Generate spec** produces three documents in sequence — each draws on the
ones before it, so the design implements the requirements and the tasks
implement the design:

| Document | Contents | Draws on |
|---|---|---|
| `requirements.md` | User stories, acceptance criteria, and a mandatory **Assumptions** section | the conversation |
| `design.md` | Architecture, components, data handling, Mermaid diagrams where useful | conversation + requirements |
| `tasks.md` | Ordered, dependency-aware implementation checklist | conversation + requirements + design |

Generation is asynchronous: per-document progress streams live, and documents
appear as they finish. Each document can be **regenerated individually** —
useful when the conversation moved on but only the design needs to change.
The first generation also creates the session's **project** (named
automatically from the conversation), which is where builds, deployments and
sharing live.

Generation respects your template's bounds; the output budget is sized for
complete documents, and a template that pins `max_tokens` very low will
truncate long designs — the template editor warns authors when a value is
risky.

## Versions, diffs, rollback

Every save — generated, hand-edited, or rolled back — creates an immutable
version with author attribution. In the project's **Spec** tab (a full code
editor, power users):

- **Rendered / Edit / Split** views, with draft autosave while you type.
- **History** lists versions; any two can be diffed side by side.
- **Rollback** is non-destructive: it creates a *new* version whose content
  is the old one, so the trail never rewrites.

Approving a spec from chat (**✓ Approve**) marks the session saved and the
documents current. Editing content after an approval matters downstream: the
risk gate keys on content, so changed content re-scores (see
[Governance](/docs/governance)).

## Session model configuration

Each session can carry its own requested model, temperature and max-tokens —
set from the [Studio](/docs/studio) rail. Ask for what you want; platform
bounds and template rails may narrow it, and the studio shows both the
requested and the effective value, naming whichever layer narrowed it. Models hosted outside Bedrock carry an **External** badge — see
the [quickstart note](/docs/quickstart) on what that means for your data.

## Exporting

The specification set exports as a zip in exactly the `.kiro/specs/<slug>/`
layout (plus a manifest), from the project page or the studio's Export tab —
a portable layout that spec-driven tooling consumes, so the handoff is
copy-free. Individual documents download from the Spec panel.

## Importing

The reverse direction works too: **Projects → Import** creates a project
from an existing specification set — paste the documents, upload a file
(a zip in the same `.kiro/specs/<slug>/` layout re-imports a marshal
export losslessly; a **PDF or Word .docx** has its text extracted verbatim
into the requirements document), or point at a public https URL (markdown,
PDF or Word becomes the requirements document; a zip imports the full
set — public sources only, no sign-in, and private networks are refused).
Extraction is deterministic and local: no OCR (scanned/image-only PDFs are
refused honestly), no model rewriting, password-protected documents are
refused by name. Requirements is the only mandatory document. Import is
Power-User surface area, like spec editing, and imported content is governed
exactly like authored content: risk scoring runs on arrival, and the deploy
gate treats it identically — where a spec came from never changes what
governs it. The chat's substrate bar accepts the same document formats when
you ground a session on an existing agent.
