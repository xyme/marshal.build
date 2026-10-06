// Integration-wave drill (B13/B9/B10/B11) — run AFTER the wave is deployed.
//
// Order matters: the webhook + chat-ops are configured FIRST so the B13
// build/deploy cycle fires REAL platform events through them. The receiver
// URL comes from the caller (RECEIVER_URL — a disposable 200-responder the
// runner creates and tears down; never a third-party service).
//
//   RECEIVER_URL=https://... DEMO_USER_PASSWORD='...' node scripts/drill-integrations.mjs
import { chromium } from "playwright";

if (!process.env.APP_URL) {
  console.error("APP_URL is required (the installation's public URL, e.g. https://<your-domain>)");
  process.exit(2);
}
const APP = process.env.APP_URL;
const PASSWORD = process.env.DEMO_USER_PASSWORD;
const RECEIVER = process.env.RECEIVER_URL;
if (!PASSWORD || !RECEIVER) {
  console.error("DEMO_USER_PASSWORD and RECEIVER_URL required");
  process.exit(1);
}
const checks = [];
const check = (name, ok, detail = "") => {
  checks.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
};
const api = (p) => `${APP}/api/backend/v1${p}`;

const REQUIREMENTS = `# Requirements — Keyed Notes API (integration drill)

## Requirement 1: Authenticated note storage
**User story:** As an integrator, I want a key-protected notes API.
#### Acceptance criteria
1. WHEN a client POSTs /notes with {ref, text} THEN the API SHALL store the note and return {id, ref, text}.
2. WHEN a client GETs /notes THEN the API SHALL return stored notes.
3. Every endpoint SHALL require an API key — requests without a valid key are rejected.

## Non-functional requirements
- HARD CONSTRAINT: the entire Lambda handler MUST be under 3000 characters of python — terse code, single file, no docstrings, no comments.
- One python Lambda + one DynamoDB table (TABLE_NAME env var) + API Gateway REST.
`;
const DESIGN = `# Design — Keyed Notes API

API Gateway REST + ONE python Lambda + DynamoDB (TABLE_NAME). POST /notes and
GET /notes on the same handler. API key required on every method (ApiKey +
UsagePlan + UsagePlanKey; ApiKeyRequired true). Terse handler, under 3000 chars.
`;
const TASKS = `# Tasks — Keyed Notes API

- [ ] 1. DynamoDB table + role
- [ ] 2. Handler: POST validation + storage, GET list
- [ ] 3. API Gateway wiring, API key + usage plan, ApiUrl + ApiKeyId outputs
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

let pid = null;
let webhookId = null;
let keepStack = false;
const power = await signIn("power@marshal.demo");
const admin = await signIn("admin@marshal.demo");
check("sessions established", true);

try {
  // ---- B10/B11 config FIRST (real events flow through them below)
  let r = await admin.ctx.request.post(api("/admin/integrations/webhooks"), {
    data: { url: RECEIVER, event_types: ["build_ready", "deploy_succeeded", "risk_decided"], description: "integration drill" },
  });
  const hook = await r.json();
  webhookId = hook.id;
  check("b10 webhook registered (secret shown once)", r.status() === 201 && hook.secret?.startsWith("whsec_"), hook.id);
  r = await admin.ctx.request.post(api(`/admin/integrations/webhooks/${webhookId}/test`));
  const testResult = await r.json();
  check("b10 test delivery", testResult.ok === true, `HTTP ${testResult.status_code} in ${testResult.rtt_ms}ms`);

  r = await admin.ctx.request.put(api("/admin/integrations/chat-ops"), {
    data: { enabled: true, provider: "slack", events: ["deploy_succeeded"], url: RECEIVER },
  });
  check("b11 chat-ops configured (url write-only)", r.status() === 200 && (await r.json()).has_url === true);
  r = await admin.ctx.request.post(api("/admin/integrations/chat-ops/test"));
  const chatTest = await r.json();
  check("b11 test post", chatTest.ok === true, `HTTP ${chatTest.status_code}`);

  // ---- B9 service-account round trip (direct fetch, no browser session)
  r = await admin.ctx.request.post(api("/admin/service-accounts"), {
    data: { name: "drill-runner", role: "power" },
  });
  const account = await r.json();
  check("b9 account created", r.status() === 201, account.id);
  r = await admin.ctx.request.post(api(`/admin/service-accounts/${account.id}/tokens`), {
    data: { name: "drill", expires_in_days: 1 },
  });
  const minted = await r.json();
  check("b9 token minted (shown once)", r.status() === 201 && minted.token?.startsWith("mat_"));

  const direct = await fetch(api("/users/me"), {
    headers: { authorization: `Bearer ${minted.token}` },
  });
  const me = direct.ok ? await direct.json() : {};
  check("b9 token authenticates directly (no session)", direct.ok && (me.email ?? "").includes("@service.marshal.local"), `HTTP ${direct.status} ${me.email ?? ""}`);
  const actOnly = await fetch(api("/projects"), {
    method: "POST",
    headers: { authorization: `Bearer ${minted.token}`, "content-type": "application/json" },
    body: JSON.stringify({ name: "Bot project" }),
  });
  check("b9 act-only: project create 403", actOnly.status === 403);
  await admin.ctx.request.delete(api(`/admin/service-accounts/${account.id}/tokens/${minted.id}`));
  const revoked = await fetch(api("/users/me"), { headers: { authorization: `Bearer ${minted.token}` } });
  check("b9 revoked token 401", revoked.status === 401, `HTTP ${revoked.status}`);

  // ---- B13 keyed deploy end-to-end
  r = await power.ctx.request.post(api("/projects"), { data: { name: "Keyed Notes API (drill)" } });
  const project = await r.json();
  pid = project.id;
  check("b13 drill project created", r.status() === 201, pid);
  for (const [doc, content] of [["requirements", REQUIREMENTS], ["design", DESIGN], ["tasks", TASKS]]) {
    await power.ctx.request.put(api(`/projects/${pid}/specs/${doc}`), { data: { content } });
  }
  let build = null;
  let final = null;
  for (let attempt = 1; attempt <= 3 && final?.status !== "ready"; attempt++) {
    const r0 = await power.ctx.request.post(api(`/projects/${pid}/builds`));
    build = await r0.json();
    final = null;
    for (let i = 0; i < 70; i++) {
      await power.page.waitForTimeout(5000);
      const poll = await (await power.ctx.request.get(api(`/builds/${build.id}`))).json();
      if (["ready", "failed", "cancelled"].includes(poll.status)) { final = poll; break; }
    }
    const findings = final?.error?.findings ?? [];
    console.log(`  build attempt ${attempt}: ${final?.status}${findings.length ? " " + JSON.stringify(findings.map((f) => f.check)) : ""}`);
    if (final?.status !== "ready" && !findings.some((f) => f.check === "inline_packaging")) break;
  }
  check("b13 build ready (auth_contract gate passed)", final?.status === "ready",
    final?.status ?? "timeout");
  if (final?.status !== "ready") throw new Error("keyed build did not become ready");

  const template = await (await power.ctx.request.get(api(`/builds/${build.id}/artifacts/template.json`))).json();
  check("b13 template carries key infrastructure",
    template.content.includes("AWS::ApiGateway::ApiKey") && template.content.includes("ApiKeyRequired"));
  // evidence: the exact auth wiring the generator produced
  try {
    const doc = JSON.parse(template.content);
    for (const [lid, res] of Object.entries(doc.Resources)) {
      if (["AWS::ApiGateway::ApiKey", "AWS::ApiGateway::UsagePlan", "AWS::ApiGateway::UsagePlanKey", "AWS::ApiGateway::Deployment", "AWS::ApiGateway::Stage"].includes(res.Type)) {
        console.log(`  [template] ${lid} (${res.Type}) DependsOn=${JSON.stringify(res.DependsOn ?? null)} Props=${JSON.stringify(res.Properties ?? {})}`);
      }
    }
    console.log(`  [template] Outputs.ApiKeyId=${JSON.stringify(doc.Outputs?.ApiKeyId)}`);
  } catch { /* dump is best-effort */ }

  const tryDeploy = () => power.ctx.request.post(api(`/projects/${pid}/deploy`), { data: { build_id: build.id } });
  r = await tryDeploy();
  if (r.status() === 403) {
    const risk = await (await power.ctx.request.get(api(`/projects/${pid}/risk`))).json();
    const assessmentId = (risk.timeline ?? [])[0]?.id;
    check("b13 risk display shows key-required", risk.endpoint_auth === "key_required", risk.endpoint_auth);
    await admin.ctx.request.post(api(`/admin/risk-assessments/${assessmentId}/decide`), {
      data: { outcome: "approve", notes: "Integration drill: keyed notes API, no PII, disposable." },
    });
    r = await tryDeploy();
  }
  check("b13 deploy accepted", r.status() === 202, `HTTP ${r.status()}`);
  let dep = null;
  for (let i = 0; i < 140; i++) {
    await power.page.waitForTimeout(5000);
    dep = await (await power.ctx.request.get(api(`/projects/${pid}/deployment`))).json();
    if (["active", "failed"].includes(dep.status)) break;
  }
  check("b13 deployment active", dep?.status === "active", `${dep?.status} ${dep?.app_url ?? ""}`);
  if (dep?.status !== "active") throw new Error("deployment did not become active");

  const keyPhase = (dep.timeline ?? []).find((t) => (t.detail ?? "").includes("API key active"));
  check("b13 timeline notes key active", !!keyPhase);
  const smokePhase = (dep.timeline ?? []).filter((t) => t.phase === "smoke").pop();
  // passed = key propagated inside the smoke budget; inconclusive = propagation
  // outran it (the keyed-request check below is the authoritative proof either
  // way); only FAILED is a real failure
  check(
    "b13 keyed smoke verdict honest (passed or inconclusive)",
    /passed|inconclusive/.test(smokePhase?.detail ?? ""),
    smokePhase?.detail
  );

  // reveal (owner, audited) then prove 403-without / 200-with
  r = await power.ctx.request.post(api(`/projects/${pid}/deployment/api-key/reveal`));
  const revealed = await r.json();
  check("b13 owner reveal", r.status() === 200 && !!revealed.value, revealed.api_key_id);
  const base = dep.app_url.replace(/\/$/, "");
  const noKey = await fetch(`${base}/notes`);
  check("b13 unkeyed request refused", noKey.status === 403, `HTTP ${noKey.status}`);
  let withKey = await fetch(`${base}/notes`, { headers: { "x-api-key": revealed.value } });
  // key-association propagation on fresh stacks: measured >2min live — poll
  // up to 8 minutes before calling it broken
  for (let i = 0; i < 16 && withKey.status === 403; i++) {
    await power.page.waitForTimeout(30000);
    withKey = await fetch(`${base}/notes`, { headers: { "x-api-key": revealed.value } });
  }
  check("b13 keyed request answers", withKey.status === 200, `HTTP ${withKey.status} ${(await withKey.text()).slice(0, 120)}`);
  if (withKey.status !== 200 && process.env.KEEP_ON_FAILURE === "1") {
    console.log(`  KEEPING stack for inspection: project ${pid}, url ${base}, key id ${revealed.api_key_id}`);
    keepStack = true;
  }

  // ---- B10 deliveries carried the real lifecycle events
  await power.page.waitForTimeout(3000);
  r = await admin.ctx.request.get(api(`/admin/integrations/webhooks/${webhookId}/deliveries`));
  const deliveries = (await r.json()).items;
  const delivered = deliveries.filter((d) => d.status === "delivered").map((d) => d.event_type);
  check("b10 real events delivered", delivered.includes("build_ready") && delivered.includes("deploy_succeeded"),
    delivered.join(", ") || "(none)");
} catch (err) {
  check("integration drill flow", false, String(err).split("\n")[0]);
} finally {
  // hygiene: teardown drill deployment + project; deactivate webhook; disable chat-ops
  try {
    if (pid && !keepStack) {
      await power.ctx.request.post(api(`/projects/${pid}/deployment/teardown`)).catch(() => {});
      for (let i = 0; i < 60; i++) {
        await power.page.waitForTimeout(5000);
        const d = await (await power.ctx.request.get(api(`/projects/${pid}/deployment`))).json().catch(() => null);
        if (!d || ["torn_down", "failed"].includes(d.status)) break;
      }
      await power.ctx.request.delete(api(`/projects/${pid}`)).catch(() => {});
      check("drill project cleaned up", true);
    }
    if (webhookId) {
      await admin.ctx.request.put(api(`/admin/integrations/webhooks/${webhookId}`), { data: { active: false } });
    }
    await admin.ctx.request.put(api("/admin/integrations/chat-ops"), { data: { enabled: false } });
    check("integrations deactivated post-drill", true);
  } catch (err) {
    check("cleanup", false, String(err).split("\n")[0]);
  }
  await browser.close();
}

const failed = checks.filter((c) => !c.ok);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
