// Redaction-scope live drill (S14-04 middle path) — production.
//
// Observation channel note: the chat MODEL cannot be used to reveal whether a
// guardrail masked its input — it declines text-analysis probes ("I'm here to
// help you build requirements…"), and Bedrock exposes no per-call guardrail
// trace to the caller. So this drill observes the platform's OWN decision on
// the path where redaction happens in-process: the S17 external-endpoint
// adapter logs `pre-egress redaction on <slug>: {counts}` immediately before
// egress. Pointing a registered endpoint at an UNREACHABLE https URL means the
// call fails after `_prepare` has already run — the decision is observable, no
// public infrastructure is created, and nothing leaves AWS.
//
//   scope=non_interactive + purpose=chat  → no redaction log line (chat exempt)
//   scope=all           + purpose=chat    → redaction log line with CARD count
//
//   DEMO_USER_PASSWORD='...' node scripts/drill-redaction-scope.mjs
//   (run from a shell that has sourced scripts/deploy-env.sh — reads CloudWatch)
import { execFileSync } from "node:child_process";
import { chromium } from "playwright";

if (!process.env.APP_URL) {
  console.error("APP_URL is required (the installation's public URL, e.g. https://<your-domain>)");
  process.exit(2);
}
const APP = process.env.APP_URL;
const PASSWORD = process.env.DEMO_USER_PASSWORD;
const LOG_GROUP = "/marshal-ai/control-plane";
const SLUG = "scope-probe";
const EXT_ID = `ext/${SLUG}`;
const CARD = "4111 1111 1111 1111";

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

// NOTE: the filter pattern MUST carry its own double quotes — CloudWatch treats
// a bare multi-word pattern as tokenized terms and silently matches nothing on
// a hyphenated phrase. An unquoted pattern made this drill's "absence" check
// pass vacuously on first run; the quotes are the assertion's teeth.
const redactionLogs = (sinceMs) => {
  const out = execFileSync("aws", [
    "logs", "filter-log-events",
    "--log-group-name", LOG_GROUP,
    "--filter-pattern", '"pre-egress redaction"',
    "--start-time", String(sinceMs),
    "--query", "events[].message",
    "--output", "text",
  ], { encoding: "utf8", maxBuffer: 10 * 1024 * 1024 });
  return out.trim();
};

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

const admin = await signIn("admin@marshal.demo");
const power = await signIn("power@marshal.demo");
check("signed in (admin + power)", true);

let controlsBefore = null;
let sessionId = null;

const putControls = (overrides) => {
  const c = controlsBefore;
  return admin.ctx.request.put(api("/admin/model-controls"), {
    data: {
      model_allowlist: c.model_allowlist, param_bounds: c.param_bounds,
      rate_limits: c.rate_limits, cost: c.cost, codegen: c.codegen,
      deployment_policies: c.deployment_policies, security: c.security,
      ...overrides,
    },
  });
};

/** Fire one chat turn containing a card at the unreachable ext endpoint. */
const probe = async () => {
  const started = Date.now();
  const res = await power.ctx.request.post(api(`/chat/sessions/${sessionId}/messages`), {
    data: { content: `Our checkout logs the customer card ${CARD} — note that in the spec.` },
    timeout: 120000,
  });
  const body = await res.text();
  return { started, failedClosed: body.includes("ExternalEndpointError") };
};

try {
  controlsBefore = await (await admin.ctx.request.get(api("/admin/model-controls"))).json();

  // ---- endpoint at an unreachable https URL (no public infra created) ----
  const endpoint = {
    label: "Redaction scope probe (unreachable by design)",
    base_url: "https://scope-probe.invalid.marshal.build/v1",
    model_name: "probe", tier: "standard",
    usd_per_1k_input: 0.001, usd_per_1k_output: 0.001,
    max_context_tokens: 8192, timeout_s: 5,
  };
  let r = await admin.ctx.request.post(api("/admin/model-endpoints"), {
    data: { slug: SLUG, ...endpoint },
  });
  if (r.status() === 422 && (await r.text()).includes("already exists")) {
    r = await admin.ctx.request.put(api(`/admin/model-endpoints/${SLUG}`), {
      data: { ...endpoint, enabled: true },
    });
  }
  check("probe endpoint registered", r.ok() || r.status() === 201, `HTTP ${r.status()}`);

  r = await putControls({ model_allowlist: [...controlsBefore.model_allowlist, EXT_ID] });
  check("probe endpoint allowlisted", r.ok());

  const s = await power.ctx.request.post(api("/chat/sessions"), { data: {} });
  sessionId = (await s.json()).id;
  r = await power.ctx.request.patch(api(`/chat/sessions/${sessionId}`), {
    data: { model_id: EXT_ID },
  });
  check("session pinned to probe endpoint", r.ok(), `HTTP ${r.status()}`);

  // ---- A: scope = non_interactive → chat exempt, no redaction line ----
  r = await admin.ctx.request.put(api("/admin/model-controls"), {
    data: {
      model_allowlist: [...controlsBefore.model_allowlist, EXT_ID],
      param_bounds: controlsBefore.param_bounds, rate_limits: controlsBefore.rate_limits,
      cost: controlsBefore.cost, codegen: controlsBefore.codegen,
      deployment_policies: controlsBefore.deployment_policies,
      security: { ...controlsBefore.security, pii_redaction: true, pii_redaction_scope: "non_interactive" },
    },
  });
  check("scope=non_interactive set", r.ok());
  // TWO tasks each hold a ≤60s settings cache whose window started at an
  // unknown point — 2× TTL + margin is the only honest wait.
  await admin.page.waitForTimeout(130000);

  const a = await probe();
  check("A: call failed closed (endpoint unreachable, as designed)", a.failedClosed);
  // Absence needs a generous settle window so "no line" isn't just delivery lag
  // (CloudWatch Logs filter lags ~10–40s behind the write).
  await admin.page.waitForTimeout(90000);
  const aLogs = redactionLogs(a.started);
  check(
    "A: chat EXEMPT at scope=non_interactive (no pre-egress redaction ran)",
    !aLogs.includes(SLUG),
    aLogs ? `unexpected: ${aLogs.slice(0, 120)}` : "no redaction log lines"
  );

  // ---- B: scope = all → chat masked before egress ----
  r = await admin.ctx.request.put(api("/admin/model-controls"), {
    data: {
      model_allowlist: [...controlsBefore.model_allowlist, EXT_ID],
      param_bounds: controlsBefore.param_bounds, rate_limits: controlsBefore.rate_limits,
      cost: controlsBefore.cost, codegen: controlsBefore.codegen,
      deployment_policies: controlsBefore.deployment_policies,
      security: { ...controlsBefore.security, pii_redaction: true, pii_redaction_scope: "all" },
    },
  });
  check("scope=all set", r.ok());
  await admin.page.waitForTimeout(130000);

  const b = await probe();
  check("B: call failed closed (endpoint unreachable, as designed)", b.failedClosed);
  // Presence: poll until the line lands (or give up) rather than guessing a lag.
  let bLogs = "";
  for (let i = 0; i < 12; i++) {
    await admin.page.waitForTimeout(10000);
    bLogs = redactionLogs(b.started);
    if (bLogs.includes(SLUG)) break;
  }
  const line = (bLogs.match(new RegExp(`[^\\t\\n]*${SLUG}[^\\t\\n]*`)) ?? [""])[0];
  check(
    "B: chat MASKED at scope=all (card redacted before egress)",
    line.includes(SLUG) && line.includes("CARD_NUMBER"),
    line.slice(0, 160) || "no redaction log line found"
  );
} catch (err) {
  check("drill flow", false, String(err).split("\n")[0]);
} finally {
  // ---- restore: disable endpoint, restore allowlist + security ----
  await admin.ctx.request.put(api(`/admin/model-endpoints/${SLUG}`), {
    data: { enabled: false },
  }).catch(() => {});
  if (controlsBefore) {
    const r = await admin.ctx.request.put(api("/admin/model-controls"), {
      data: {
        model_allowlist: controlsBefore.model_allowlist,
        param_bounds: controlsBefore.param_bounds, rate_limits: controlsBefore.rate_limits,
        cost: controlsBefore.cost, codegen: controlsBefore.codegen,
        deployment_policies: controlsBefore.deployment_policies,
        security: controlsBefore.security ?? {},
      },
    }).catch(() => null);
    check("platform state restored (endpoint disabled, security reset)", !!r && r.ok());
  }
  if (sessionId) {
    await power.ctx.request.delete(api(`/chat/sessions/${sessionId}`)).catch(() => {});
  }
  await browser.close();
}

const failed = checks.filter((c) => !c.ok);
console.log(failed.length ? `\nREDACTION DRILL FAILED (${failed.length})` : "\nREDACTION DRILL PASSED");
process.exit(failed.length ? 1 : 0);
