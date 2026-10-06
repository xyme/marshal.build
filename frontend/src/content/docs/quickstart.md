# Quickstart

marshal turns a conversation into a specification, a specification into a
working AI agent, and an approved agent into a live deployment. Three
stages: **Build, Govern, Deploy.**

## 1. Start a project

From **Projects → New project**, either start from scratch or pick a template.
A template narrows which models and parameters you can use, and pre-fills the
questions worth answering for that kind of agent — pick one if it matches
what you are building. Power users have a third path: **Import** an existing
specification set (pasted markdown, a marshal-layout zip, a PDF or a Word
document) — see [Chat & specifications](/docs/chat-and-specs).

You can also start from the **Marketplace**: open a sample that resembles your
idea and fork it. You get its specification as a starting point rather than a
blank page.

## 2. Describe what you need (Build)

Open the project's **Chat** tab and describe the agent in plain language.
Useful things to say early:

- who uses it and what they are trying to get done
- where the data comes from and how sensitive it is
- what must happen when something goes wrong

When the conversation has enough substance, choose **Generate specification**.
marshal writes three documents:

| Document | What it holds |
|---|---|
| `requirements.md` | User stories and acceptance criteria |
| `design.md` | Architecture, components, data handling |
| `tasks.md` | An implementation checklist |

Review them in the **Spec** tab. Power users can edit the documents directly;
every save creates a version you can diff and roll back.

Power users can also work in the **Studio** — the same session with the
specification evolving beside the conversation and the model configuration in
a side rail, instead of behind tabs. Choose it when creating a session, or
**Open in Studio** from any chat. See [The Studio](/docs/studio) for the tour.

## 3. Generate the agent

In the **Build** tab, choose **Start build**. marshal generates the actual
agent service from your specification and validates it before anything can be
deployed — resource types, packaging rules, forbidden patterns, and the
deployment contract are all checked. Findings appear together so you can fix
them in one pass.

Two build profiles:

- **inline-cfn** (default) — a CloudFormation template with inline code. No
  toolchain, fastest path.
- **cdk-app** — a full TypeScript CDK application you could take away and run
  yourself (experimental: requires the workspace-runner codegen provider
  enabled by an admin).

You can download the result as a repository bundle at any time.

## 4. Get approval (Govern)

Deployment is gated on a risk assessment of your specification's *content*.
Low-risk projects pass straight through. Medium and high risk go to an approver,
who sees the specification and the assessment together. You will be notified
when the decision lands.

Changing your specification re-triggers the check; redeploying unchanged content
does not.

## 5. Deploy to an Enclave (Deploy)

Choose **Deploy** on the project. marshal leases a dedicated AWS account (an
**Enclave**), deploys the generated stack into it, and streams the progress live.
When it finishes you get a URL.

Deployments run in **Full Governance** mode by default. A **Testbed** mode —
same pipeline, plus hands-on access to the deployment's account for
sandbox-style work — may be available depending on platform policy; see
[Deployment & Enclaves](/docs/deployment).

Things worth knowing:

- Deployments carry a **time limit**. You will be warned before expiry and can
  extend within policy.
- A **health indicator** shows whether the deployed agent is still answering.
- Deploying again after a specification change **updates in place** rather than
  tearing down. **Preview changes** first shows exactly which resources the
  update would add, modify or replace — evaluated against the running stack
  without touching it — so you apply updates knowing what they do.
- **Tear down** removes everything and stops the spend.

## What if something is refused?

Refusals are deliberate and each one tells you what to do next:

| You see | What it means |
|---|---|
| Pending risk review | An approver has it; you will be notified |
| Concurrency limit | You or the platform is at the deployment limit — tear one down |
| Monthly cost cap | Your spend cap is reached; an admin can raise it |
| Too many requests | Brief rate limit; the message says when to retry |
| Build findings | The generated agent failed validation; fix the listed items |
| External endpoint failure | A custom (non-Bedrock) model failed mid-call; the platform never silently reroutes — retry, or pick another model |

One more badge worth knowing: models marked **External** are custom endpoints
hosted outside Amazon Bedrock. Choosing one sends your prompts to that
provider — the badge follows the model everywhere it appears.

## Going deeper

This page is the tour. Each stage has a full guide:

- [Chat & specifications](/docs/chat-and-specs) — sessions, guided mode,
  generation mechanics, versions and rollback
- [The Studio](/docs/studio) — the power-user build workspace
- [Code generation](/docs/codegen) — build profiles, the validation gate,
  bundles
- [Governance](/docs/governance) — the risk gate, caps, limits, audit
- [Deployment & Enclaves](/docs/deployment) — the full lifecycle, updates,
  preview, expiry
- [Templates & Marketplace](/docs/marketplace) — guardrails, lifecycle,
  forking and contributing
