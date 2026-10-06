# Troubleshooting

## "Deployment is pending risk review"

Your specification scored medium or high risk, so it needs an approver. You will
be notified when they decide. Nothing is lost — the build stays ready.

## "You already have N live deployments"

A platform policy limits concurrent Enclaves per person. Tear down one you no
longer need, or ask an admin to raise the limit under Model Controls →
Deployment policies. Updating an existing deployment does not count against the
limit.

## "The platform is at its concurrent-deployment limit"

Someone else's deployments are occupying the pool. Retry shortly. If this
happens often, the Enclave account pool needs more accounts registered.

## "Monthly cost cap reached"

Your user or project spend hit its cap. An admin can raise it under Model
Controls → Cost, or per user under Admin → Users. Caps reset monthly.

## "Too many model requests"

A brief rate limit. The message includes the number of seconds to wait — the
platform queues short waits automatically before refusing.

## "The model request failed" on a session using an External model

The custom endpoint behind that model failed or became unreachable mid-call.
This is deliberate fail-closed behavior: the platform never silently reroutes
a prompt aimed at an external provider to a different model. Retry — brief
provider blips are retried automatically before you ever see the error — or
switch the session to another model. If it persists, an admin can run **Test
connection** on the endpoint under Model Controls → Custom endpoints, which
reports the provider's own error verbatim.

## I cannot see the Studio

The studio is a power-user surface. Business personas keep the guided wizard
by design — there is nothing to enable. If you are a power user and `/studio`
shows "not found", the platform flag may be off in your environment; an admin
controls it under Model Controls → Feature flags.

## "Preview changes" says there are no changes

The running stack already matches the build you selected — an update would be
a no-op. Pick a different ready build, or revise the specification and build
again. The preview asks the running stack itself, so "no changes" is the
stack's own answer, not a guess.

## "Generation exceeded … budget"

The full specification set has a wall-clock budget (seven minutes). Healthy
runs finish in two to three; hitting the budget almost always means the model
provider stalled on one call rather than that your spec is too large. The
documents that completed **are saved** — check the spec panel tabs, then
click ↻ on the missing document to regenerate only that one (about a minute)
instead of the whole set.

## My build failed validation

The findings list every problem at once. Common ones:

- **Resource type not allowed** — the generated agent used a service outside
  the platform's allowlist. Rephrase the specification toward the allowed
  architecture.
- **Inline code too large** — the inline profile caps embedded code at
  CloudFormation's own limit. When that is the *only* problem, the build
  re-plans itself as a packaged deployment (code ships as an S3 asset, no
  inline ceiling, no dependencies added) — you'll see "re-planning as a
  packaged deployment" in the build console and `size_escalation` on the
  manifest. If you still see this finding, the packaged path wasn't
  available: rebuild (generation variance usually fits) or add "*SHALL use
  packaged dependencies*" to the requirements document. The `cdk-app`
  profile also lifts the ceiling, but needs the workspace-runner codegen
  provider (an admin setting).
- **Missing ApiUrl output** — the deploy contract needs one; regenerate.
- **Forbidden pattern** — for example hard-coded credentials. Remove the
  requirement or state that secrets come from a secret store.

## My deployment says "degraded"

The platform probed the deployed agent and it failed repeated checks. The
stack is still there. Open the deployment URL to see what it returns, check the
logs in the timeline, then redeploy after fixing the specification. Health flips
back on its own when the agent answers again.

## My deployment expired

Deployments have a TTL and are torn down when it elapses; you were warned at 48
and 24 hours. Redeploy the ready build whenever you need it back — nothing else
was lost. Extend before expiry next time from the deployment card.

## The spec editor will not open

The editor is a heavyweight component and can fail to load on a slow
connection. Reload the page. If it persists it is worth reporting — include
the browser console output.

## I cannot see a project someone mentioned

Access comes from ownership, an explicit share, or a team the project is filed
in. Ask the owner to share it directly or to file it into a team you belong to.
The platform does not disclose the existence of projects you cannot access, so
the page shows "not found" rather than "forbidden".

## Two-factor is required and I have not set it up

Profile → Two-factor authentication → Set up, scan the QR code with an
authenticator app (or enter the key manually), enter the code, then sign out
and back in. The requirement applies at sign-in, so an existing session will
not satisfy it.

## Email notifications never arrive

In-app notifications always work. Email delivery is a separate switch that
requires production sending access and verified DNS; if it has not been enabled
in your environment, email stays off by design and nothing is queued.

If email IS enabled and one person stops receiving mail, their address has
likely bounced or they marked a message as spam: the mail service then
suppresses that address automatically. Administrators can check suppressed
addresses and overall channel health from the email status readout in the
admin area.


## "Template … does not allow the declared capability"

Your requirements declare a capability rung — conversation memory, packaged
dependencies, tools, or planning — that the project's template forbids. The
refusal names both the capability and the template, and the save/import is
aborted (nothing half-lands). Remove the declaring sentence, or ask an
administrator whether the template's capability rail should widen. The same
check re-runs at deploy time, so a template tightened after a build refuses
that build too.

## My PDF or Word import was refused

Import extraction is deterministic and local, so refusals are exact:
**password-protected** documents must be decrypted first;
**scanned/image-only PDFs** have no extractable text (the platform does not
run OCR); a **corrupt file** or an unsupported format (only spec-set zips,
PDF, Word `.docx`, and plain text import) is named as such. Nothing is
mangled into a bad import — fix the file and retry. The same rules apply to
the chat substrate bar.

## "Dependents block teardown"

Another agent declares yours as a dependency ("SHALL call agent …"), so
tearing yours down would break a live caller — the refusal names the
dependent projects. Tear the dependents down first, or ask their owners to.
Deployment expiry is the one exception: TTL enforcement outranks
composition, and the dependents' own health probes surface the dead edge.
