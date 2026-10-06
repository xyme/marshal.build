// S17-7 live drill — custom model endpoints against a live installation (APP_URL).
//
// Proves the governed external-model contract end to end against a real public
// OpenAI-compatible HTTPS endpoint (API GW + Lambda mock; no real LLM needed):
//   register → probe (rtt/model/streaming) → key-flow proof (wrong key → 401)
//   → allowlist → session pinned to ext/ → SSE stream → priced invocation row
//   → 429 at clamped rate cap → endpoint dies → fail-closed error, NO fallback
//   → disable → gone from registry/allowlist, session falls back with clamp note.
//
// Usage:
//   DEMO_USER_PASSWORD='...' DRILL_BASE_URL='https://xyz.execute-api...' \
//     node scripts/drill-s17.mjs
//
// Leaves behind: the endpoint row DISABLED (pricing history stays — by design),
// everything else restored. Exit 0 = all checks passed.
import { chromium } from "playwright";
import os from "node:os";
import path from "node:path";

if (!process.env.APP_URL) {
  console.error("APP_URL is required (the installation's public URL, e.g. https://<your-domain>)");
  process.exit(2);
}
const APP = process.env.APP_URL;
const PASSWORD = process.env.DEMO_USER_PASSWORD;
const BASE_URL = process.env.DRILL_BASE_URL; // e.g. https://xyz.execute-api.us-east-1.amazonaws.com/v1
const GOOD_KEY = "drill-key-2026";
const SLUG = "drill-mock";
const EXT_ID = `ext/${SLUG}`;

if (!PASSWORD || !BASE_URL) {
  console.error("DEMO_USER_PASSWORD and DRILL_BASE_URL are required");
  process.exit(1);
}

const checks = [];
const check = (name, ok, detail = "") => {
  checks.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
};
const api = (p) => `${APP}/api/backend/v1${p}`;

const browser = await chromium.launch();
const ctx = await browser.newContext();
const page = await ctx.newPage();

// sign in as admin via the hosted UI
await page.goto(APP, { waitUntil: "networkidle", timeout: 45000 });
await page.getByRole("button", { name: /sign in/i }).click();
await page.waitForURL(/amazoncognito\.com/, { timeout: 30000 });
await page.locator('input[name="username"]:visible, input[id="signInFormUsername"]:visible').first().fill("admin@marshal.demo");
await page.locator('input[name="password"]:visible, input[id="signInFormPassword"]:visible').first().fill(PASSWORD);
await page.locator('input[name="signInSubmitButton"]:visible, button[type="submit"]:visible, input[type="submit"]:visible').first().click();
await page.waitForURL(/\/(home|onboarding|projects)/, { timeout: 45000 });
check("admin signed in", true);

const req = ctx.request;
let controlsBefore = null;
let sessionId = null;

try {
  // ---------- 1. register (idempotent: PUT config if the slug survives a rerun)
  const endpointPayload = {
    label: "Drill Mock (on-prem stand-in)",
    base_url: BASE_URL,
    model_name: "drill-model",
    tier: "standard",
    usd_per_1k_input: 0.5,
    usd_per_1k_output: 1.0,
    max_context_tokens: 8192,
    timeout_s: 30,
    api_key: GOOD_KEY,
  };
  let res = await req.post(api("/admin/model-endpoints"), { data: { slug: SLUG, ...endpointPayload } });
  if (res.status() === 422 && (await res.text()).includes("already exists")) {
    res = await req.put(api(`/admin/model-endpoints/${SLUG}`), { data: { ...endpointPayload, enabled: true } });
    check("register endpoint (rerun: re-enabled)", res.ok(), `HTTP ${res.status()}`);
  } else {
    check("register endpoint", res.status() === 201, `HTTP ${res.status()}`);
  }

  // ---------- 2. probe: rtt + model echo + streaming
  res = await req.post(api(`/admin/model-endpoints/${SLUG}/probe`));
  let probe = res.ok() ? await res.json() : {};
  check(
    "probe ok (rtt/model/streaming)",
    probe.ok === true && probe.streaming === true && probe.model_echo === "drill-model",
    `rtt=${probe.rtt_ms}ms model=${probe.model_echo} streaming=${probe.streaming}`
  );
  const probeRtt = probe.rtt_ms;

  // ---------- 3. key-flow proof: wrong key → endpoint 401s → restore
  res = await req.put(api(`/admin/model-endpoints/${SLUG}`), { data: { api_key: "wrong-key-999" } });
  check("wrong key stored", res.ok(), `HTTP ${res.status()}`);
  // Key cache is 300s per task — probe reads through api_key_for's cache, so
  // ask until the new key is live on the task we hit (2 tasks, ≤60s settings).
  let wrongKeyProbe = {};
  for (let i = 0; i < 8; i++) {
    res = await req.post(api(`/admin/model-endpoints/${SLUG}/probe`));
    wrongKeyProbe = res.ok() ? await res.json() : { httpStatus: res.status(), body: (await res.text()).slice(0, 80) };
    if (wrongKeyProbe.ok === false) break;
    await page.waitForTimeout(5000);
  }
  check(
    "wrong key → endpoint 401 (key really transmits)",
    wrongKeyProbe.ok === false && `${wrongKeyProbe.error_class} ${wrongKeyProbe.error}`.includes("401"),
    JSON.stringify(wrongKeyProbe).slice(0, 140)
  );
  await req.put(api(`/admin/model-endpoints/${SLUG}`), { data: { api_key: GOOD_KEY } });
  // Key rotation propagates within the 300s per-task key cache — poll it out
  // on BOTH tasks before anything downstream depends on the good key.
  probe = {};
  let greens = 0;
  for (let i = 0; i < 70 && greens < 4; i++) {
    res = await req.post(api(`/admin/model-endpoints/${SLUG}/probe`));
    probe = res.ok() ? await res.json() : {};
    greens = probe.ok === true ? greens + 1 : 0;
    if (greens < 4) await page.waitForTimeout(5000);
  }
  check("key restored, probe green (post-rotation window)", greens >= 4, `rtt=${probe.rtt_ms}ms`);

  // ---------- 4. allowlist the external id
  controlsBefore = await (await req.get(api("/admin/model-controls"))).json();
  const putControls = (overrides) =>
    req.put(api("/admin/model-controls"), {
      data: {
        model_allowlist: controlsBefore.model_allowlist,
        param_bounds: controlsBefore.param_bounds,
        rate_limits: controlsBefore.rate_limits,
        cost: controlsBefore.cost,
        codegen: controlsBefore.codegen,
        deployment_policies: controlsBefore.deployment_policies,
        security: controlsBefore.security,
        ...overrides,
      },
    });
  res = await putControls({ model_allowlist: [...controlsBefore.model_allowlist, EXT_ID] });
  check("allowlist ext id", res.ok(), `HTTP ${res.status()}`);

  const registry = await (await req.get(api("/admin/templates/model-registry"))).json();
  const entry = registry.find((m) => m.id === EXT_ID);
  check("merged registry lists external", !!entry && entry.source === "external", JSON.stringify(entry ?? {}));

  // ---------- 5. session pinned to the external model (S18 rail seam)
  res = await req.post(api("/chat/sessions"), { data: {} });
  sessionId = (await res.json()).id;
  res = await req.patch(api(`/chat/sessions/${sessionId}`), { data: { model_id: EXT_ID } });
  check("session PATCH model_id=ext/…", res.ok(), `HTTP ${res.status()}`);
  const eff = await (await req.get(api(`/chat/sessions/${sessionId}/effective-params`))).json();
  check("effective-params honors ext pick", eff.effective?.model_id === EXT_ID, JSON.stringify(eff.effective ?? {}));

  // ---------- 6. chat streams from the external endpoint
  let t0 = Date.now();
  res = await req.post(api(`/chat/sessions/${sessionId}/messages`), {
    data: { content: "Say hello from outside Bedrock." },
    timeout: 60000,
  });
  const wall = Date.now() - t0;
  let sse = await res.text();
  const doneLine = sse.split("\n").find((l) => l.startsWith("data:") && l.includes("latency_ms"));
  const done = doneLine ? JSON.parse(doneLine.slice(5)) : {};
  // Deltas arrive one word per SSE frame — reassemble before asserting content.
  const streamedText = sse
    .split("\n")
    .filter((l) => l.startsWith("data:") && l.includes('"text"'))
    .map((l) => {
      try {
        return JSON.parse(l.slice(5)).text ?? "";
      } catch {
        return "";
      }
    })
    .join("");
  check(
    "SSE stream completes on ext model",
    res.ok() && streamedText.includes("Streaming from a public") && done.model_id === EXT_ID,
    `model=${done.model_id} first-to-last=${done.latency_ms}ms wall=${wall}ms in=${done.input_tokens} out=${done.output_tokens}`
  );

  // ---------- 7. priced invocation row (admin rates, not Bedrock table)
  await page.waitForTimeout(1500); // fire-and-forget recording
  const costs = await (await req.get(api("/admin/costs"))).json();
  const extRow = (costs.by_model ?? []).find((m) => m.model_id === EXT_ID);
  check("priced invocation row", !!extRow && extRow.usd > 0, JSON.stringify(extRow ?? {}));

  // ---------- 8. 429 at the clamped rate cap
  res = await putControls({
    model_allowlist: [...controlsBefore.model_allowlist, EXT_ID],
    rate_limits: { ...controlsBefore.rate_limits, per_user_rpm: 1 },
  });
  check("rate cap set (per_user_rpm=1)", res.ok());
  // TWO backend tasks each hold a ≤60s settings cache — wait out the TTL so
  // whichever task serves the next request enforces the new cap.
  await page.waitForTimeout(61000);
  // one message consumes the budget; the next must refuse with a structured 429
  await req.post(api(`/chat/sessions/${sessionId}/messages`), { data: { content: "burn the budget" }, timeout: 60000 });
  res = await req.post(api(`/chat/sessions/${sessionId}/messages`), { data: { content: "over the cap" } });
  let refused = res.status() === 429;
  let retryDetail = "";
  if (refused) {
    const body = await res.json().catch(() => ({}));
    retryDetail = JSON.stringify(body.detail ?? body).slice(0, 100);
  }
  check("429 at clamped cap (real status, not stream error)", refused, `HTTP ${res.status()} ${retryDetail}`);
  res = await putControls({
    model_allowlist: [...controlsBefore.model_allowlist, EXT_ID],
    rate_limits: controlsBefore.rate_limits,
  });
  check("rate limits restored", res.ok());
  await page.waitForTimeout(61000); // let the burned minute window roll over

  // ---------- 9. endpoint dies mid-conversation → fail closed, never fall back
  t0 = Date.now();
  res = await req.post(api(`/chat/sessions/${sessionId}/messages`), {
    data: { content: "please DRILL_FAIL_NOW" },
    timeout: 90000,
  });
  sse = await res.text();
  const hasErrorEvent = sse.includes("event: error") && sse.includes("ExternalEndpointError");
  const noFallbackDone = !sse.split("\n").some((l) => l.startsWith("data:") && l.includes('"model_id"') && !l.includes(EXT_ID));
  check(
    "endpoint failure → structured error, NO silent fallback [D16]",
    hasErrorEvent && noFallbackDone,
    `${Date.now() - t0}ms (includes 3 retries) :: ${sse.split("event: error")[1]?.slice(0, 120).replace(/\n/g, " ") ?? "no error event"}`
  );

  // ---------- 10. disable → gone from registry + allowlist within a minute
  res = await req.put(api(`/admin/model-endpoints/${SLUG}`), { data: { enabled: false } });
  check("disable endpoint", res.ok());
  let registryAfter = [];
  for (let i = 0; i < 14; i++) {
    registryAfter = await (await req.get(api("/admin/templates/model-registry"))).json();
    if (!registryAfter.some((m) => m.id === EXT_ID)) break;
    await page.waitForTimeout(5000);
  }
  check("registry drops external on disable (≤60s)", !registryAfter.some((m) => m.id === EXT_ID), `${registryAfter.length} entries`);
  // The RAW settings row keeps the id (pricing history); the RESOLVED domain
  // filters it — visible via the registry (above) and the session fallback.
  // Same two-task cache reality: give the TTL a chance before asserting.
  let effAfter = {};
  for (let i = 0; i < 14; i++) {
    effAfter = await (await req.get(api(`/chat/sessions/${sessionId}/effective-params`))).json();
    if (effAfter.effective && effAfter.effective.model_id !== EXT_ID) break;
    await page.waitForTimeout(5000);
  }
  check(
    "session falls back with clamp note",
    !!effAfter.effective &&
      effAfter.effective.model_id !== EXT_ID &&
      typeof effAfter.clamped_by?.model_id === "string",
    `effective=${effAfter.effective?.model_id} clamped_by=${effAfter.clamped_by?.model_id}`
  );

  // ---------- screenshots for the sprint log
  await page.goto(`${APP}/admin/models`, { waitUntil: "networkidle", timeout: 45000 });
  const shot1 = path.join(os.tmpdir(), "drill-s17-admin-models.png");
  await page.screenshot({ path: shot1, fullPage: true });
  console.log(`  screenshot: ${shot1}`);
  check("probe rtt evidence captured", typeof probeRtt === "number", `probe rtt=${probeRtt}ms`);
} catch (err) {
  check("drill flow", false, String(err).split("\n")[0]);
} finally {
  // ---------- restore production state
  if (controlsBefore) {
    await req.put(api("/admin/model-controls"), {
      data: {
        model_allowlist: controlsBefore.model_allowlist,
        param_bounds: controlsBefore.param_bounds,
        rate_limits: controlsBefore.rate_limits,
        cost: controlsBefore.cost,
        codegen: controlsBefore.codegen,
        deployment_policies: controlsBefore.deployment_policies,
        security: controlsBefore.security,
      },
    }).catch(() => {});
  }
  if (sessionId) await req.delete(api(`/chat/sessions/${sessionId}`)).catch(() => {});
  await browser.close();
}

const failed = checks.filter((c) => !c.ok);
console.log(failed.length ? `\nDRILL FAILED (${failed.length})` : "\nDRILL PASSED");
process.exit(failed.length ? 1 : 0);
