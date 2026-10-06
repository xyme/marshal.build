# Governance

Every model call and every deployment on the platform passes through the same
controls, for every persona — governance keys on *content and usage*, never on
who you are. This page explains the controls you will actually encounter:
the risk gate, cost caps, rate limits, and the audit trail.

## The risk gate

Deployment is gated on an AI risk assessment of your specification's
**content**. When you deploy, the platform scores the current documents
across weighted factors — data sensitivity, model capability, user-facing
exposure, deployment scope, expected request volume, plus two computed
reach factors: how many deployed agents this one calls (composition), and
which capability rungs it declares (memory, packaged dependencies, tools,
planning) — and bands the result **low**, **medium** or **high**. You see every factor and its
rationale on the assessment; the exact weights and band boundaries are
platform policy and deliberately not published — an assessment you can
word your way around is not an assessment.

- **Low** auto-approves — you deploy without waiting.
- **Medium and high** hold the deployment and route to reviewers: medium to
  the managers group, high to the governance board. Reviewers see the
  specification and the factor-by-factor assessment together.

The assessment also **displays** operational signals it does not score —
whether the selected intent is key-protected by the platform default, keyed
explicitly, or deliberately public, plus the deployment mode — so reviewers
decide with the whole picture. Endpoint posture does not lower the risk score;
a strict endpoint-auth policy can refuse public intent before this risk gate.

Assessments are keyed on a **content hash**: identical content is scored
exactly once, an approval survives redeploys of the same content, and *any*
edit to the documents voids the approval and re-scores. Nobody — including
admins — can approve content that has since changed.

Reviewer outcomes: **approve** (gate opens), **reject** (notes required —
you see why), or **request changes** (you get one reply, revise, and the
resubmission links to its predecessor so reviewers see the lineage). Reviews
that sit too long escalate automatically — first up the reviewer chain, then
to administrators. If a reviewer group is empty, the item skips to the next
link rather than stalling, and administrators are alerted to the
configuration gap.

## Gate stance and deployment modes

How firmly the gate holds depends on the **deployment mode** you choose at
deploy time (see [Deployment & Enclaves](/docs/deployment)):

- **Full Governance** — the gate **enforces**. A pending, rejected or
  changes-requested verdict blocks the deploy. This is the default, and its
  behavior is fixed: the name is the contract.
- **Testbed** — the gate is **advisory** by default: your content is still
  scored, still routed to reviewers, and the verdict still shows on the
  project — but it does not block the deploy, and every advisory bypass is
  recorded in the audit trail. Administrators can set Testbed's stance to
  **off** (no scoring at deploy) for event use, or disable Testbed mode
  entirely.

Whatever the stance, the assessment record is identical — reviewers see the
same queue and the same factors. Advisory changes *when* a deploy can
proceed, never *what is known* about it.

## Cost caps

Model spend is metered in real time — every call is recorded with its token
counts and priced cost, including calls to admin-registered external
endpoints (priced at the admin-entered rates). Three cap scopes: per user,
per project, and a platform budget.

- Alerts fire as a cap fills (once per threshold per month), so overspend
  is never a surprise.
- At the cap, behavior follows the admin's setting: **Alert** keeps serving,
  **Block** refuses new model calls with a structured message telling you the
  cap and who can raise it. Refusals happen *before* the call spends
  anything.
- Platform-internal governance work (risk scoring, titling) is exempt at
  user scope — the platform never lets its own controls starve.

Your own month-to-date usage is on your profile; project budgets sit on the
project card and tint as they fill.

## Rate limits

Requests per minute are limited per user, per project, and platform-wide.
Short bursts queue briefly and succeed; a hard refusal is an honest **429**
carrying the seconds to wait. Chat refusals happen before the reply stream
opens, so you never watch a reply die mid-sentence to a limit.

## The audit trail

Every mutating action on the platform — session created, spec saved, build
started, deployment approved, setting changed, member added — lands in the
audit log with actor, resource, timestamp, and before/after detail where it
matters. Model invocations are recorded alongside: prompt and response
hashes, tokens, cost, latency, and the purpose of the call.

Administrators can filter, export, and archive the trail; history is
archived durably before anything is pruned, on a platform-policy schedule.
The practical guarantee for you: decisions about your work are
reconstructible — who approved what, when, and against which version of the
content.

## Where the controls live

Users meet the controls as refusal messages and badges; administrators tune
them centrally — see the short [administration overview](/docs/admin) for
what is governed where. Control changes take effect quickly and are
themselves audited.
