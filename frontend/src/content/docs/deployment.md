# Deployment & Enclaves

The **Deploy** stage runs your generated agent as its own CloudFormation
stack. Where that stack lands depends on the installation's sandbox provider:
with the `isb` provider (AWS Innovation Sandbox) each deployment gets a
dedicated AWS account leased for the deployment, isolated from the platform
and from every other deployment; with the default `direct` provider the stack
deploys into the installation's own AWS account, under a dedicated deployment
role, isolated by stack and tags rather than by account. In direct mode the
deployment role can create IAM roles and the allow-listed services inside the
installation account; run evaluations in a dedicated account, or use Enclave
mode for per-deployment isolation. There is deliberately no test/production
tier — every deployment is treated as a serious deployment, and the risk gate
is the promotion control.

## Deployment modes

Every deployment runs in one of two modes, chosen on the deploy panel and
**fixed for the life of the deployment** (updates inherit it — custody is
not a toggle):

- **Full Governance** (default) — the account is an **Enclave**: the
  platform holds sole custody end to end, and the risk gate enforces. This
  is the classic marshal deployment.
- **Testbed** — the account is a **Testbed**: marshal still plans, builds
  and deploys, but you can mint **limited, time-boxed AWS credentials** for
  the account to do the things the pipeline cannot do for you — accept
  EULAs and marketplace agreements, configure additional AWS services, and
  explore your agent's resources directly. The risk gate runs in advisory
  stance by default ([Governance](/docs/governance)). Budget and lease
  expiry enforce exactly as in Full Governance.

If you don't see the mode picker, your administrator has disabled Testbed
mode, or a deployment is already running (updates inherit).

### Testbed access

On an active Testbed deployment, the **Testbed access** card mints
credentials on demand:

- You get a **CLI export block** (shown once — the platform stores no copy)
  and a **federated AWS console link** that signs you straight into the
  account.
- Sessions are **time-boxed** and expire on their own; re-mint whenever you
  need a fresh one while the deployment lives. Sessions may be capped at one
  hour by AWS session rules — the card always shows the real expiry.
- The credentials are **limited**: identity and billing changes, audit-trail
  tampering, and touching marshal's own deployed stack are refused. Enough
  to configure services and accept agreements; not enough to change who is
  in control.
- Every action you take in the account is **attributed to you** in its audit
  history, and every credential issuance is a security-audited event on the
  platform.

Teardown reclaims the whole account regardless of anything configured by
hand — a Testbed is still a leased, disposable account.

## What happens when you deploy

1. **Policy pre-flight** — concurrency, region and endpoint-authentication
   policy are checked first, before risk scoring or account leasing. A strict
   installation refuses an explicitly public or legacy-unverified build and
   tells you to remove the opt-out/rebuild.
2. **Risk gate** — the current specification content must be approved (see
   [Governance](/docs/governance)). Low risk auto-approves; medium/high wait
   for a reviewer.
3. **Lease** — with the `isb` provider the platform leases an AWS account
   from the pool (an Enclave or a Testbed, per your mode) and the deployment
   card shows lease economics: budget consumed against cap, and the lease's
   own expiry. With the default `direct` provider the "lease" is the
   installation's own account and no pool economics are shown.
4. **Stack creation** — the build's CloudFormation template deploys into the
   leased account; `cdk-app` builds stage their packaged assets into the
   Enclave first. Progress streams live, phase by phase.
5. **Post-deploy smoke** — the platform probes the routes the generated agent
   actually declares. An agent that comes up but misbehaves is marked
   **degraded** with a per-route report on the build card, not silently
   called healthy.

When it finishes you get a URL, and the deployment card becomes the control
surface for everything below.

## Keys, reveals, and the test page

Newly generated APIs require a key by default. The only open posture is an
explicit public opt-out in requirements, and an administrator may forbid that
opt-out. The deployment card tells you whether the selected build is keyed by
default, keyed explicitly, or deliberately public.

For a keyed deployment, the owner uses **Reveal key**: the value is fetched
live from deployed infrastructure at the moment you ask—the platform stores
no copy—and every reveal is security-audited. Key-to-stage access can take
several minutes to propagate on a fresh deployment; smoke reports that as
inconclusive instead of running an expected-key build without its key.

If your specification asked for a **web test page**, the card shows an
**Open test console** link: an interactive page served by your deployment
itself, where you (or anyone you demo to) can exercise the agent. On a keyed
API the page asks for the key at runtime and holds it in memory only — it is
never embedded anywhere.

## Policies and limits

Concurrency (per user and platform-wide), region, time-to-live, and a
per-deployment budget are all set by platform policy — the deploy panel and
any refusal message show the limits that apply to you. Policy checks run
before the risk gate — the cheapest refusal always comes first. In-place
updates never count against concurrency.

## Health, expiry, extend

- **Health** — active deployments are probed regularly; repeated failures
  mark the deployment **degraded** and notify you once per incident.
  Recovery clears the badge silently. Degraded means "not answering", not
  "torn down" — the stack is still there for diagnosis.
- **Expiry** — every deployment carries a TTL, warned at 48 and 24 hours,
  then torn down through the standard path. Nothing but the running stack is
  lost: redeploying the ready build restores it.
- **Extend** — owners extend within the policy maximum and the lease bound;
  extensions are audited.

## Updating a running deployment

Deploying again while a deployment is active takes the **update path**: the
running stack is updated in place — no teardown, no URL change, no
concurrency charge. The deployment history records every attempt:

- a successful update **supersedes** the prior record;
- a failed update **rolls back automatically** and the prior deployment
  remains active — the record always mirrors what the stack is actually
  running;
- deploying a build identical to what runs fails fast with "no changes".

**Preview changes** takes the guesswork out: it evaluates a real
CloudFormation change set for the selected build against the running stack —
without modifying it — and lists every resource that would be added, modified
or removed, flagging replacements. "No changes" from the preview is the
stack's own answer.

## Teardown

**Tear down** deletes the stack, releases the lease, and stops the spend.
The project, its specification, builds and history all remain — teardown is
about the running infrastructure, not your work. Deployment history keeps
the full record of every attempt, supersession and teardown.

## Access and attribution

Deploy, extend and teardown belong to the project **owner**. Editors and
viewers see status; reviewers see what the gate shows them. Every Enclave
resource is tagged to its project, user and deployment, so infrastructure
spend lands in the chargeback report against the right project — model spend
is attributed to whoever made the call, infrastructure to the project owner.
