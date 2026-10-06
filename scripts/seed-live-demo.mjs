// Live showcase deployment (owner request, 5 Aug 2026).
//
// Makes "Mortgage Pre-Qualification Advisor" GENUINELY live: saves real
// FS-flavored specs through the product, builds, passes the risk gate the
// honest way (admin approval if held), deploys into a real Enclave, verifies
// the deployed API answers, and extends the TTL toward the policy maximum.
// NO teardown — the deployment is the demo asset.
//
// Why through the product rather than seeded rows: the platform treats
// deployment rows as real (health probes, expiry sweeps), so a faked "active"
// deployment decays into failed/degraded with a dead URL — exactly what a
// live demo must not click into. Re-runnable: a healthy active deployment
// short-circuits to verification.
//
//   DEMO_USER_PASSWORD='...' node scripts/seed-live-demo.mjs
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

// Architecture mirrors the proven-buildable shape (API GW REST + one python
// Lambda + DynamoDB) with genuine FS content. The app computes decision
// factors deterministically in code — no model calls inside the deployed app.
const REQUIREMENTS = `# Requirements — Mortgage Pre-Qualification Advisor

## Introduction
Guides applicants through pre-qualification and explains the decision factors
in plain language. This service exposes the pre-qualification API the advisor
UI calls.

## Requirement 1: Submit a pre-qualification
**User story:** As an applicant, I want to submit my financials and receive a
pre-qualification band with the deciding factor explained, so that I
understand the outcome.
#### Acceptance criteria
1. WHEN a client POSTs /prequalify with {applicant_ref, annual_income, monthly_debt, loan_amount} THEN the API SHALL store the application and return {id, dti, decision_band, factor}.
2. The ONLY accepted input fields are applicant_ref, annual_income, monthly_debt, loan_amount. WHEN the request body contains any other field THEN the API SHALL reject it with HTTP 400 and a message naming the offending field — no names, no contact details, no PII of any kind (compliance requirement).
3. dti SHALL be monthly_debt divided by (annual_income / 12), rounded to 3 decimals.
4. decision_band SHALL be EXACTLY one of these three literal strings, chosen by dti alone:
   - dti <= 0.36 -> decision_band = "likely"
   - dti > 0.36 AND dti <= 0.43 -> decision_band = "possible"
   - dti > 0.43 -> decision_band = "refer"
   No other band value may ever be returned. Do NOT invent alternative
   vocabulary such as "review", "approve", "decline" or "pending".
5. factor SHALL be a human-readable ENGLISH SENTENCE (a string of words, never a number) naming the threshold that decided the band. For example, for dti 0.263 the API returns factor = "DTI 0.263 is at or under the 0.36 likely threshold." and for dti 0.525 it returns factor = "DTI 0.525 exceeds the 0.43 referral threshold."

## Requirement 2: Review submissions
**User story:** As an advisor, I want to list recent pre-qualifications, so
that I can walk a client through their result.
#### Acceptance criteria
1. WHEN a client GETs /prequalify THEN the API SHALL return stored applications with their decision bands.

## Requirement 3: Protected endpoints
**User story:** As the service owner, I want the API closed to anonymous
callers, so that a demo endpoint on the public internet is not an open
write surface.
#### Acceptance criteria
1. Every endpoint SHALL require an API key.

## Non-functional requirements
- HARD CONSTRAINT: the entire Lambda handler MUST be under 3200 characters of
  python — terse code, single file, no helper classes, no docstrings, no
  comments, minimal error handling (validation may be a single try/except).
- All computation is deterministic code — no model calls at request time.
- Data stays in the deployment account's DynamoDB table.
`;

const DESIGN = `# Design — Mortgage Pre-Qualification Advisor

## Architecture
API Gateway REST + ONE python Lambda + a DynamoDB table (name via env var
TABLE_NAME). The single handler serves POST /prequalify and GET /prequalify.
Keep the handler minimal and compact — terse code, no helper layers.

## Decision logic (computed in code)
- dti = monthly_debt / (annual_income / 12), rounded to 3 decimals
- decision_band is one of exactly three literal strings — no others:
  - dti <= 0.36 -> "likely"
  - 0.36 < dti <= 0.43 -> "possible"
  - dti > 0.43 -> "refer"
- factor is an English sentence naming the threshold applied, never a
  number. Example: "DTI 0.263 is at or under the 0.36 likely threshold."
- loan_amount is stored and echoed but does NOT affect dti or the band.

## Input validation
Unknown request fields are rejected with HTTP 400 naming the field; only
applicant_ref, annual_income, monthly_debt and loan_amount are accepted.

## Data handling
Items keyed by generated id; applicant_ref is caller-supplied opaque text.
No third-party calls; no model calls.

## Access
Endpoints require an API key (API Gateway key + usage plan); the key value
lives only in the deployment account and is revealed on demand to the owner.

## Implementation constraint
Entire handler under 3200 characters: terse, no docstrings or comments.
`;

const TASKS = `# Tasks — Mortgage Pre-Qualification Advisor

- [ ] 1. DynamoDB table + IAM role
- [ ] 2. Lambda handler: POST validation, factor computation, storage
- [ ] 3. GET list + GET by id
- [ ] 4. API Gateway wiring with ApiUrl output
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
check("power signed in", true);

try {
  const projects = await (await power.ctx.request.get(api("/projects"))).json();
  const project = (projects.items ?? projects).find((p) => p.name.includes("Mortgage Pre-Qualification"));
  check("demo project found", !!project, project?.id);
  if (!project) throw new Error("mortgage-advisor project missing — run seed_demo first");
  const pid = project.id;

  // Re-run friendliness: already live and healthy → verify only.
  // FORCE_REDEPLOY=1 rebuilds and updates in place regardless (used to
  // refresh the running stack after platform fixes; exercises the real
  // update path + changeset semantics).
  const force = process.env.FORCE_REDEPLOY === "1";
  let dep = await (await power.ctx.request.get(api(`/projects/${pid}/deployment`))).json();
  if (!force && dep.status === "active" && dep.health !== "degraded" && dep.app_url) {
    console.log("already active + healthy — verifying only");
  } else {
    // ---- specs (new versions through the product; versioning keeps history)
    for (const [doc, content] of [["requirements", REQUIREMENTS], ["design", DESIGN], ["tasks", TASKS]]) {
      const r = await power.ctx.request.put(api(`/projects/${pid}/specs/${doc}`), { data: { content } });
      check(`spec saved: ${doc}`, r.status() === 200, `HTTP ${r.status()}`);
    }

    // ---- build, with retry: generation length varies run to run, and the
    // inline profile has a hard 4KB handler ceiling — a validation failure on
    // inline_packaging is a coin the next generation usually flips (observed
    // 5 Aug 2026: 2 of 3 runs fit). Rebuilding on findings is exactly what
    // the product tells users to do; three attempts keeps it honest.
    let final = null;
    let build = null;
    for (let attempt = 1; attempt <= 3 && final?.status !== "ready"; attempt++) {
      const r0 = await power.ctx.request.post(api(`/projects/${pid}/builds`));
      build = await r0.json();
      check(`build attempt ${attempt} started`, r0.status() === 202, build.id ?? `HTTP ${r0.status()}`);
      final = null;
      for (let i = 0; i < 70; i++) {
        await power.page.waitForTimeout(5000);
        const poll = await (await power.ctx.request.get(api(`/builds/${build.id}`))).json();
        if (["ready", "failed", "cancelled"].includes(poll.status)) { final = poll; break; }
      }
      const findings = final?.error?.findings ?? [];
      const inlineOverflow = findings.some((f) => f.check === "inline_packaging");
      console.log(`  attempt ${attempt}: ${final?.status ?? "timeout"}${inlineOverflow ? " (inline ceiling — retrying)" : ""}`);
      if (final?.status !== "ready" && !inlineOverflow) break; // only retry the variance class
    }
    check("build ready", final?.status === "ready",
      final ? `${final.status}${final.error ? " " + JSON.stringify(final.error).slice(0, 160) : ""}` : "timeout");
    if (final?.status !== "ready") throw new Error("build did not become ready");

    // ---- CONFORMANCE GATE (added 28 Aug after a build shipped a DIFFERENT
    // contract than its approved spec — invented applicant_name (the PII the
    // spec forbids), monthly_income for annual_income, and four bands of its
    // own. The platform's own B7 conformance report had already flagged 11
    // violations; the seeder deployed anyway because it only asked "is the
    // build ready?". A governance demo must not contradict its own spec, and
    // the product was already shouting — so ask IT, then rebuild if needed.
    const conformanceOf = (b) => b?.manifest?.conformance ?? b?.conformance ?? null;
    let report = conformanceOf(final);
    for (let attempt = 1; attempt <= 4 && (report?.summary?.violated ?? 0) > 0; attempt++) {
      const violated = report.summary.violated;
      const worst = (report.verdicts ?? [])
        .filter((v) => v.verdict === "violated")
        .slice(0, 2)
        .map((v) => (v.evidence ?? "").slice(0, 110));
      console.log(`  conformance attempt ${attempt}: ${violated} violation(s) — rebuilding`);
      for (const w of worst) console.log(`    · ${w}`);
      const r0 = await power.ctx.request.post(api(`/projects/${pid}/builds`));
      const next = await r0.json();
      let polled = null;
      for (let i = 0; i < 70; i++) {
        await power.page.waitForTimeout(5000);
        polled = await (await power.ctx.request.get(api(`/builds/${next.id}`))).json();
        if (["ready", "failed", "cancelled"].includes(polled.status)) break;
      }
      if (polled?.status === "ready") {
        build = next;
        final = polled;
        report = conformanceOf(polled);
      }
    }
    check(
      "build conforms to its approved spec (B7 report clean)",
      (report?.summary?.violated ?? 0) === 0,
      report?.summary
        ? `met=${report.summary.met} violated=${report.summary.violated} unverifiable=${report.summary.unverifiable}`
        : "no conformance report on this build"
    );
    if ((report?.summary?.violated ?? 0) > 0) {
      throw new Error(
        "generation kept diverging from the spec — refusing to deploy a demo " +
        "whose endpoint contradicts its own approved specification"
      );
    }

    // ---- deploy through the risk gate
    const tryDeploy = () => power.ctx.request.post(api(`/projects/${pid}/deploy`), { data: { build_id: build.id } });
    let r = await tryDeploy();
    if (r.status() === 403) {
      const code = (await r.json().catch(() => ({})))?.detail?.code;
      check("risk gate held the deploy (expected)", code === "risk_pending", code);
      const admin = await signIn("admin@marshal.demo");
      const risk = await (await power.ctx.request.get(api(`/projects/${pid}/risk`))).json();
      const assessmentId = (risk.timeline ?? [])[0]?.id;
      const dec = await admin.ctx.request.post(api(`/admin/risk-assessments/${assessmentId}/decide`), {
        data: { outcome: "approve", notes: "Showcase deployment: pre-qualification API, deterministic factors, no PII beyond applicant_ref. Approved for demo." },
      });
      check("admin approved assessment", dec.ok(), `HTTP ${dec.status()}`);
      await admin.ctx.close();
      r = await tryDeploy();
    }
    check("deploy accepted", r.status() === 202, `HTTP ${r.status()}`);
    if (r.status() !== 202) throw new Error(`deploy refused: ${(await r.text()).slice(0, 200)}`);

    dep = null;
    for (let i = 0; i < 140; i++) {
      await power.page.waitForTimeout(5000);
      dep = await (await power.ctx.request.get(api(`/projects/${pid}/deployment`))).json();
      if (["active", "failed"].includes(dep.status)) break;
    }
    check("deployment active", dep?.status === "active", `${dep?.status} ${dep?.app_url ?? ""}`);
    if (dep?.status !== "active") throw new Error("deployment did not become active");

    // ---- extend TTL toward the policy maximum (168h total; deploy stamps
    // 72h, so +48h reaches 120h — the max the policy allows in one step
    // without exceeding 168h from deployment, live-verified 5 Aug 2026)
    r = await power.ctx.request.post(api(`/projects/${pid}/deployment/extend`), { data: { hours: 48 } });
    check("TTL extended (+48h)", r.ok(), r.ok() ? `expires ${(await r.json()).expires_at}` : `HTTP ${r.status()}`);
  }

  // ---- prove the LIVE app answers like a real service. The demo API is
  // KEYED (owner directive 26 Aug: no open-to-world endpoints), so the
  // verification reveals the key the same way a human would — from the
  // deployment card — and refuses to pass on an unauthenticated 403.
  const base = dep.app_url.replace(/\/$/, "");
  const revealed = await power.ctx.request.post(api(`/projects/${pid}/deployment/api-key/reveal`));
  const key = revealed.ok() ? (await revealed.json()).value : null;
  check("API key revealed for the demo", !!key, revealed.ok() ? "" : `HTTP ${revealed.status()}`);

  const anon = await power.ctx.request.post(`${base}/prequalify`, {
    data: { applicant_ref: "ANON-PROBE", annual_income: 1, monthly_debt: 1, loan_amount: 1 },
    timeout: 30000,
  });
  check("keyless calls are refused (403)", anon.status() === 403, `HTTP ${anon.status()}`);

  const keyed = { "x-api-key": key ?? "" };
  let created = {};
  let res = null;
  // API Gateway key→plan association propagates for minutes on a fresh
  // stack; retry inside one budget rather than declaring a live demo broken.
  for (let i = 0; i < 24; i++) {
    res = await power.ctx.request.post(`${base}/prequalify`, {
      headers: keyed,
      data: { applicant_ref: "DEMO-0001", annual_income: 96000, monthly_debt: 2100, loan_amount: 420000 },
      timeout: 30000,
    });
    if (res.ok()) { created = await res.json(); break; }
    await power.page.waitForTimeout(15000);
  }
  check("live API accepts a keyed submission", !!created.decision_band,
    `HTTP ${res?.status()} band=${created.decision_band} dti=${created.dti}`);
  res = await power.ctx.request.get(`${base}/prequalify`, { headers: keyed, timeout: 30000 });
  check("live API lists submissions", res.ok(), `HTTP ${res.status()}`);

  // ---- CONFORMANCE to the approved spec (added 26 Aug after a generation
  // shipped "review"/"decline" bands and a NUMERIC factor — a governance
  // demo whose endpoint contradicts its own approved spec is worse than no
  // demo, and "it answered 200" is the check that cannot fail).
  const band = async (income, debt) => {
    const r = await power.ctx.request.post(`${base}/prequalify`, {
      headers: keyed,
      data: { applicant_ref: `CONF-${debt}`, annual_income: income, monthly_debt: debt, loan_amount: 420000 },
      timeout: 30000,
    });
    return r.ok() ? await r.json() : {};
  };
  const low = await band(96000, 2100);    // dti 0.2625 -> likely
  const mid = await band(96000, 3200);    // dti 0.4000 -> possible
  const high = await band(96000, 4200);   // dti 0.5250 -> refer
  check("bands match the spec (likely/possible/refer)",
    low.decision_band === "likely" && mid.decision_band === "possible" &&
    high.decision_band === "refer",
    `${low.decision_band}/${mid.decision_band}/${high.decision_band}`);
  check("factor explains rather than counts",
    typeof low.factor === "string" && /\s/.test(low.factor) &&
    Number.isNaN(Number(low.factor)),
    JSON.stringify(low.factor));
  const rejected = await power.ctx.request.post(`${base}/prequalify`, {
    headers: keyed,
    data: { applicant_ref: "CONF-PII", annual_income: 96000, monthly_debt: 2100, loan_amount: 420000, full_name: "Jane Doe" },
    timeout: 30000,
  });
  check("unknown fields rejected (PII compliance claim)", rejected.status() === 400,
    `HTTP ${rejected.status()}`);

  dep = await (await power.ctx.request.get(api(`/projects/${pid}/deployment`))).json();
  console.log(`\nLIVE DEMO URL: ${dep.app_url}`);
  console.log(`expires: ${dep.expires_at ?? "(see deployment card)"} — extend from the deployment card before demos`);
  console.log("API key: reveal from the deployment card (owner-only, audit-logged)");
  if (key) {
    console.log("\n--- curl (key redacted; substitute your revealed key) ---");
    console.log(`curl -sS -X POST '${base}/prequalify' \\\n  -H 'x-api-key: <KEY>' -H 'content-type: application/json' \\\n  -d '{"applicant_ref":"DEMO-0001","annual_income":96000,"monthly_debt":2100,"loan_amount":420000}'`);
    console.log(`\ncurl -sS '${base}/prequalify' -H 'x-api-key: <KEY>'`);
  }
} catch (err) {
  check("live-demo flow", false, String(err).split("\n")[0]);
} finally {
  await browser.close();
}

const failed = checks.filter((c) => !c.ok);
console.log(failed.length ? `\nLIVE DEMO SEED FAILED (${failed.length})` : "\nLIVE DEMO SEED COMPLETE");
process.exit(failed.length ? 1 : 0);
