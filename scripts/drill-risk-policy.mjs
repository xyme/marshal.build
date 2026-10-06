// B17 configurable-rubrics drill (configurable-rubrics spec R4.1): live
// proof that the risk policy is versioned, the gate is strict across policy
// changes, auto-approve-off queues LOW, every change is audited, and the
// policy restores cleanly. No Enclave is ever leased — a held gate refuses
// BEFORE leasing, and the happy-path re-score rides the on-save trigger.
//
//   DEMO_USER_PASSWORD='...' node scripts/drill-risk-policy.mjs
import { chromium } from "playwright";

if (!process.env.APP_URL) {
  console.error("APP_URL is required (the installation's public URL, e.g. https://<your-domain>)");
  process.exit(2);
}
const APP = process.env.APP_URL;
const PASSWORD = process.env.DEMO_USER_PASSWORD;
if (!PASSWORD) {
  console.error("DEMO_USER_PASSWORD required");
  process.exit(1);
}
const checks = [];
const check = (name, ok, detail = "") => {
  checks.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
};
const api = (p) => `${APP}/api/backend/v1${p}`;

const REQUIREMENTS = `# Requirements — Meeting Notes Tagger (policy drill)

## Requirement 1: Tag a note
**User story:** As a team member, I want short meeting notes tagged by topic.
#### Acceptance criteria
1. WHEN a client POSTs /notes with {text} THEN the API SHALL store the note and return {id, text, tag} where tag is computed from simple keyword lists.
2. Internal tool for a small team; generic non-personal content only; ad-hoc usage.
`;
const DESIGN = `# Design — Meeting Notes Tagger
API Gateway REST + one python Lambda + DynamoDB (TABLE_NAME). Keyword-list
tagging computed in code; no model calls; internal-only, low volume.
`;
const TASKS = `# Tasks — Meeting Notes Tagger
- [ ] 1. Table + role
- [ ] 2. Handler + tagging
`;

const browser = await chromium.launch();
const signIn = async (email) => {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(APP, { waitUntil: "networkidle", timeout: 45000 });
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL(/amazoncognito\.com/, { timeout: 30000 });
  await page.locator('input[name="username"]:visible, input[id="signInFormUsername"]:visible').first().fill(email);
  await page.locator('input[name="password"]:visible, input[id="signInFormPassword"]:visible').first().fill(PASSWORD);
  await page.locator('input[name="signInSubmitButton"]:visible, button[type="submit"]:visible, input[type="submit"]:visible').first().click();
  await page.waitForURL(/\/(home|onboarding|projects)/, { timeout: 45000 });
  return { ctx, page };
};

const power = await signIn("power@marshal.demo");
const admin = await signIn("admin@marshal.demo");
check("sessions established", true);

let pid = null;
let baseline = null;
const putPolicy = (data) =>
  admin.ctx.request.put(api("/admin/governance/risk-policy"), { data });

const riskOf = async () =>
  await (await power.ctx.request.get(api(`/projects/${pid}/risk`))).json();

const waitAssessed = async (predicate, tries = 24) => {
  for (let i = 0; i < tries; i++) {
    const r = await riskOf();
    if (r.assessed && predicate(r)) return r;
    await power.page.waitForTimeout(5000);
  }
  return await riskOf();
};

try {
  // ---- baseline policy
  let r = await admin.ctx.request.get(api("/admin/governance/risk-policy"));
  baseline = await r.json();
  check("policy readable", r.status() === 200, `v${baseline.policy_version}`);
  const v0 = baseline.policy_version;

  // ---- low-risk project scores LOW + auto-approves under current policy
  r = await power.ctx.request.post(api("/projects"), { data: { name: "Risk Policy Drill (B17)" } });
  const project = await r.json();
  pid = project.id;
  check("drill project created", r.status() === 201, pid);
  for (const [doc, content] of [["requirements", REQUIREMENTS], ["design", DESIGN], ["tasks", TASKS]]) {
    await power.ctx.request.put(api(`/projects/${pid}/specs/${doc}`), { data: { content } });
  }
  let risk = await waitAssessed((x) => x.decision != null);
  check(
    "scored LOW + auto-approved under baseline",
    risk.level === "low" && risk.decision === "auto_approved" && risk.policy_version === v0,
    `level=${risk.level} decision=${risk.decision} policy=v${risk.policy_version}`
  );

  // ---- tighten the low band below this content's score -> version bumps
  r = await putPolicy({ band_low_max: Math.max(1, (risk.score ?? 25) - 5) });
  const tightened = await r.json();
  check("tighten accepted + version bumped", r.status() === 200 && tightened.policy_version === v0 + 1,
    `v${tightened.policy_version}`);

  // ---- deploy attempt re-scores under the new policy and is HELD pre-lease
  r = await power.ctx.request.post(api(`/projects/${pid}/deploy`), { data: {} });
  const heldBody = r.status() === 403 ? (await r.json()).detail : {};
  check("deploy HELD after tightening (403 risk_pending)", r.status() === 403 && heldBody.code === "risk_pending",
    `HTTP ${r.status()} code=${heldBody.code}`);
  risk = await riskOf();
  check(
    "fresh assessment under the new policy (medium+, pending)",
    risk.policy_version === v0 + 1 && risk.level !== "low" && risk.decision === "pending",
    `level=${risk.level} policy=v${risk.policy_version} current=v${risk.current_policy_version}`
  );

  // ---- superseded chip data: old approval no longer opens anything
  check("payload exposes current_policy_version", risk.current_policy_version === v0 + 1, "");

  // ---- auto-approve OFF queues LOW: restore the band, disable auto-approve
  r = await putPolicy({ band_low_max: baseline.band_low_max, auto_approve_low: false });
  const noAuto = await r.json();
  check("auto-approve off accepted + version bumped", r.status() === 200 && noAuto.policy_version === v0 + 2,
    `v${noAuto.policy_version}`);
  r = await power.ctx.request.post(api(`/projects/${pid}/deploy`), { data: {} });
  const queued = r.status() === 403 ? (await r.json()).detail : {};
  check("LOW queues when auto-approve is off (403 risk_pending)",
    r.status() === 403 && queued.code === "risk_pending", `HTTP ${r.status()} code=${queued.code}`);
  risk = await riskOf();
  check("queued row is LOW + pending under new version",
    risk.level === "low" && risk.decision === "pending" && risk.policy_version === v0 + 2,
    `level=${risk.level} decision=${risk.decision} policy=v${risk.policy_version}`);

  // ---- no-op PUT must not bump
  r = await putPolicy({ auto_approve_low: false });
  check("no-op PUT does not bump", (await r.json()).policy_version === v0 + 2, "");

  // ---- restore baseline knobs -> one more bump; on-save trigger re-scores
  r = await putPolicy({
    auto_approve_low: baseline.auto_approve_low,
    band_low_max: baseline.band_low_max,
    band_medium_max: baseline.band_medium_max,
    weights: baseline.weights,
    anchors: baseline.anchors,
  });
  const restored = await r.json();
  check("policy restored (version monotonic, knobs baseline)",
    r.status() === 200 && restored.policy_version === v0 + 3 &&
    restored.auto_approve_low === baseline.auto_approve_low &&
    restored.band_low_max === baseline.band_low_max,
    `v${restored.policy_version}`);
  // same content re-saved -> background re-score under restored policy
  await power.ctx.request.put(api(`/projects/${pid}/specs/requirements`), { data: { content: REQUIREMENTS } });
  risk = await waitAssessed((x) => x.policy_version === v0 + 3);
  check("re-scored under restored policy -> auto-approved again",
    risk.policy_version === v0 + 3 && risk.level === "low" && risk.decision === "auto_approved",
    `level=${risk.level} decision=${risk.decision} policy=v${risk.policy_version}`);

  // ---- audit evidence for every change
  const audit = await (await admin.ctx.request.get(api("/admin/audit-logs?q=risk_policy&page_size=10"))).json();
  const rows = (audit.items ?? []).filter((e) => e.action === "risk_policy_updated");
  check("audit rows for policy changes (>=4 incl. no-op)", rows.length >= 4, `${rows.length} rows`);

  // ---- validation refused loudly
  r = await putPolicy({ band_low_max: 90, band_medium_max: 40 });
  check("invalid policy refused (422)", r.status() === 422, `HTTP ${r.status()}`);
} catch (err) {
  check("drill aborted", false, String(err).split("\n")[0]);
} finally {
  if (pid) {
    const del = await power.ctx.request.delete(api(`/projects/${pid}`)).catch(() => null);
    console.log(`cleanup: project delete HTTP ${del?.status?.() ?? "n/a"}`);
  }
  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}
