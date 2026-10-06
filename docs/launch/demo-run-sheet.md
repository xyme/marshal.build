# Live demo run-sheet (12–15 min)

## Demo agent
**"Expense Receipt Triage Agent"** — small (two routes, one table, keyed API, web test page), rich enough to show every stage. The build starts *inline*; if the generated handler exceeds CloudFormation's 4 KB inline ceiling the platform **auto-escalates to packaged-cfn** (observed in staging, 5 Oct) — that is a feature, worth one sentence if it happens, not a fallback trigger. Opening message (paste-ready). Do **not** type "synthetic data only" and do not attach a policy PDF that says "no real data": the risk rubric reads such disclaimers as low data sensitivity (ds 3 → score 30 → LOW, auto-approved, no reviewer beat). The receipts you send during the demo are fictional regardless; the spec just describes the real intended use (employee reimbursement claims with employee ID and name → ds ≥ 4 → medium):

> Build an expense receipt triage agent that handles employee reimbursement claims. Staff POST a receipt (employee ID, employee name, merchant, amount, category, free-text note); the agent classifies it as auto-approve, needs-manager, or policy-violation using these rules: under $50 auto-approve; alcohol or gifts always needs-manager; anything over $500 policy-violation unless category is travel. GET returns a receipt's decision with the rule that fired. The system SHALL provide a web test page.

(The "SHALL provide a web test page" phrase triggers the web console so the audience sees a UI, not just curl.)

## Pre-stage checklist
**T-24 h**
- [ ] Deploy the tagged clean SHA; full smoke green.
- [ ] Project **A** (the one you'll build live): created, spec generated and approved, build READY, risk decided — the **fallback** if live generation or build misbehaves.
- [ ] Project **B**: identical agent, **deployed and active** (extend TTL to 72 h) — the **fallback** for the curl finale.
- [ ] One template active with visible rails (model allowlist + token ceiling + allowed capabilities), one published marketplace sample.
- [ ] Admin account TOTP ready on your phone; power account signed in on a second browser profile.
- [ ] Risk policy: default bands, so the demo agent lands **medium** (it processes financial data). Sonnet 5 scores without temperature pinning, so the score varies by a few points between runs; the opening message above names employee ID / employee name / reimbursement claims and carries no synthetic-data disclaimer, which keeps the score clear of the 30-point low/medium boundary — confirmed in staging with three fresh generations (figures in the staging report).

**T-1 h**
- [ ] Dry run end to end once. Note the generation time (expect ~2 min) and build time (expect 2–3 min inline; longer when auto-escalated to packaged-cfn — staging report has the measured figure).
- [ ] Open tabs: Home, Chat (new session), Project B's Deployment tab, Admin → Review queue, Admin → Audit, Admin → Costs, Deployments fleet page.
- [ ] Terminal with BASE_URL/API_KEY env prepared for Project B as fallback.
- [ ] Screen-record the dry run — a 3-minute clip of the deploy phases is the last-resort fallback.

**T-10 min**
- [ ] Health endpoint green; CloudWatch: no ALARM except the two scale-in lows.
- [ ] Phone unlocked, authenticator open.

## Beat sheet
| Time | Beat | Show | Say |
|---|---|---|---|
| 0:00 | Home | Workbench: "what needs me" cards | "Everything in marshal asks for a decision somewhere; home collects them." |
| 0:45 | New session | Pick the template; paste the opening message; **attach a one-page PDF** of the expense policy via *Upload file* | "Grounding on your real documents — extracted locally, no model, no OCR." |
| 1:30 | Generate | Click Generate; **while it runs** open Studio → prompt context + effective params (show a clamped value) | "The model never sees anything you can't see here. Rails are enforced, not suggested." |
| 4:00 | Spec set | Three docs appear; open design (Mermaid); click **Approve**; the **Start your build** CTA | "Append-only versions, diff, rollback, comments. Approval is a decision, so it's recorded." |
| 4:45 | Build | Start build; narrate phases: plan → files → template → **validation gate** → conformance. If the status shows **packaged-cfn**: "the handler outgrew the 4 KB inline ceiling, so it packaged itself — no decision needed from me" | "Deterministic gate. Any finding fails. Here's the conformance report mapping requirements to code." |
| 7:30 | Artifacts | File browser; handoff bundle download | "Take it with you — specs plus code plus manifest." |
| 8:00 | Govern | Risk badge **medium → pending**. Switch browser: admin → review queue → open → approve with comment (TOTP on the way in) | "Maker-checker. The reviewer's name is on it. Tightened policy re-scores the next deploy." |
| 9:30 | Deploy | Back to owner: **Deploy this build** (full governance). Narrate: pre-flight → **lease an account** → stack → health. Meanwhile show the **Deployments fleet** page and the Audit timeline (your approval + every model call with cost) | "A fresh AWS account per deployment. The boundary is the control." |
| 11:30 | Prove it | Active: **Reveal key** → wait ~30 s (API Gateway key propagation lags activation; a 403 `Forbidden` in the first half-minute is normal — show the audit timeline meanwhile) → curl POST …/receipts with a $620 "team dinner" → `policy-violation`, rule cited → open the **web test page** | "Real endpoint, real account, keyed by default." |
| 12:30 | Lifecycle | Show TTL, extend, teardown button (don't tear down unless time allows) | "It will reap itself at the TTL; the sweep is automatic." |
| 13:00 | Close | `docs/ENGINEERING_LOG.md` in the public repo, open on the latest entry: "here is this week's defect, root cause and fix" → repo URL | "Built in the open. Come break it." |

## Fallbacks (decide in <10 s, keep talking)
- **Generation slow/stalled (>3 min)** → switch to Project A's approved spec: "I pre-generated this one earlier; same prompt." Continue from the Build beat.
- **Build fails or exceeds 4 min** → Project A's READY build; show its conformance report instead.
- **Deploy slow (>4 min at leasing)** → narrate what Innovation Sandbox is doing, then cut to Project B's active deployment for the curl; let the live one finish in the background and show it active at the close.
- **Model-provider outage** → the recorded dry-run clip for the deploy segment; live for everything that needs no model (artifacts, audit, admin, fleet, curl on B).

## Do not do live
- Flip the codegen provider or edit model controls.
- Delete the demo session mid-demo.
- Use a real document or real data — synthetic only, say so on screen.
- Show owner-internal operations docs or any admin page with tester emails.
