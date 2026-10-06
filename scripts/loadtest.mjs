// S13-01 load-test harness — measured baseline for the published SLOs.
//
// Drives the REAL user path (CloudFront → frontend proxy → backend) with
// browser-session auth, because that is the path users take and the path that
// hid the S1 Service Connect outage from curl-level probes (§7.0 practice #1).
//
// Usage:
//   DEMO_USER_PASSWORD='...' node scripts/loadtest.mjs                # 1x
//   DEMO_USER_PASSWORD='...' LOAD_MULTIPLIER=5 node scripts/loadtest.mjs
//   ... SESSIONS=10 DURATION_S=120 PROFILE=reads node scripts/loadtest.mjs
//
// Env:
//   APP_URL           required (installation public URL)
//   DEMO_USER_PASSWORD  required
//   LOAD_MULTIPLIER   1 (default) | 5 | 20 — scales concurrent sessions
//   SESSIONS          override the computed session count
//   DURATION_S        per-phase duration (default 60)
//   PROFILE           all (default) | reads | sse | dashboards
//   SLO_CHECK         "1" to exit non-zero when an SLO is breached
//
// SLO targets (S13-01, sized for D4 ≈50 concurrent users):
//   p95 API reads      < 500ms
//   p95 dashboards     < 1500ms   (admin aggregates)
//   SSE first event    < 3000ms
//   5xx rate           0 at 5x
import { chromium } from "playwright";

if (!process.env.APP_URL) {
  console.error("APP_URL is required (the installation's public URL, e.g. https://<your-domain>)");
  process.exit(2);
}
const APP = process.env.APP_URL;
const PASSWORD = process.env.DEMO_USER_PASSWORD;
const MULT = Number(process.env.LOAD_MULTIPLIER ?? 1);
const DURATION_S = Number(process.env.DURATION_S ?? 60);
const PROFILE = process.env.PROFILE ?? "all";
const SLO_CHECK = process.env.SLO_CHECK === "1";
// D4 sizing: ~50 concurrent users. A browser session issuing continuous
// requests represents ~10 real users' request rate, so 5 sessions ≈ 50 users.
const SESSIONS = Number(process.env.SESSIONS ?? Math.max(1, 5 * MULT));

const SLOS = {
  "api.read": 500,
  "api.dashboard": 1500,
  "sse.first_event": 3000,
};

if (!PASSWORD) {
  console.error("DEMO_USER_PASSWORD is required");
  process.exit(2);
}

const api = (p) => `${APP}/api/backend/v1${p}`;
const samples = new Map(); // label -> [ms]
const errors = [];
let requests = 0;

function record(label, ms) {
  if (!samples.has(label)) samples.set(label, []);
  samples.get(label).push(ms);
}

function pct(arr, p) {
  if (!arr.length) return null;
  const sorted = [...arr].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))];
}

async function timed(label, fn) {
  const t0 = performance.now();
  try {
    const res = await fn();
    const ms = performance.now() - t0;
    requests++;
    const status = typeof res?.status === "function" ? res.status() : res?.status;
    if (status >= 500) errors.push(`${label} HTTP ${status}`);
    else if (status >= 400 && status !== 404) errors.push(`${label} HTTP ${status}`);
    record(label, ms);
    return res;
  } catch (e) {
    requests++;
    errors.push(`${label} threw ${String(e).slice(0, 120)}`);
    return null;
  }
}

async function signIn(email) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(APP, { waitUntil: "networkidle", timeout: 60000 });
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL(/amazoncognito\.com/, { timeout: 45000 });
  await page.locator('input[name="username"]:visible').first().fill(email);
  await page.locator('input[name="password"]:visible').first().fill(PASSWORD);
  await page
    .locator('input[name="signInSubmitButton"]:visible, button[type="submit"]:visible')
    .first()
    .click();
  await page.waitForURL(/\/(home|onboarding|projects)/, { timeout: 60000 });
  return { ctx, page };
}

// --- workload phases ---------------------------------------------------------

async function readLoop(ctx, deadline) {
  const paths = [
    ["projects", "/projects?page_size=24"],
    ["unread", "/notifications/unread-count"],
    ["samples", "/marketplace/samples?page_size=12"],
    ["stats", "/users/me/stats"],
    ["templates", "/templates"],
  ];
  while (Date.now() < deadline) {
    for (const [name, p] of paths) {
      if (Date.now() >= deadline) break;
      // Per-path label AND the aggregate the SLO is stated against
      const t0 = performance.now();
      await timed(`api.read.${name}`, () => ctx.request.get(api(p), { timeout: 30000 }));
      record("api.read", performance.now() - t0);
    }
  }
}

async function dashboardLoop(ctx, deadline) {
  const paths = [
    ["costs", "/admin/costs"],
    ["costs_user", "/admin/costs/breakdown?group_by=user"],
    ["costs_purpose", "/admin/costs/breakdown?group_by=purpose"],
    ["risk", "/admin/risk-assessments?status=all"],
    ["audit", "/admin/audit-logs?page_size=50"],
  ];
  while (Date.now() < deadline) {
    for (const [name, p] of paths) {
      if (Date.now() >= deadline) break;
      const t0 = performance.now();
      await timed(`api.dashboard.${name}`, () => ctx.request.get(api(p), { timeout: 30000 }));
      record("api.dashboard", performance.now() - t0);
    }
  }
}

// SSE: time from SEND to first streamed event — the perceived latency of the
// user action. Session creation happens BEFORE the timer starts: in real usage
// the session already exists when the user presses send, so including it
// measured a request users never make at that moment.
async function sseLoop(page, deadline) {
  while (Date.now() < deadline) {
    const ms = await page
      .evaluate(async ({ base }) => {
        const sess = await fetch(`${base}/chat/sessions`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: "{}",
        }).then((r) => r.json());
        const t0 = performance.now();
        const res = await fetch(`${base}/chat/sessions/${sess.id}/messages`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ content: "Reply with one word: ping" }),
        });
        if (!res.ok) return { error: res.status };
        const reader = res.body.getReader();
        await reader.read(); // first chunk = first SSE event
        const elapsed = performance.now() - t0;
        try { reader.cancel(); } catch {}
        return { ms: elapsed };
      }, { base: `${APP}/api/backend/v1` })
      .catch((e) => ({ error: String(e).slice(0, 80) }));
    requests++;
    if (ms?.error) errors.push(`sse ${ms.error}`);
    else record("sse.first_event", ms.ms);
  }
}

// --- run ---------------------------------------------------------------------

const browser = await chromium.launch();
const started = new Date();
console.log(
  `loadtest: ${SESSIONS} session(s) (multiplier ${MULT}x ≈ ${SESSIONS * 10} users), ` +
    `${DURATION_S}s, profile=${PROFILE}, target ${APP}`
);

const sessions = [];
try {
  // power users drive reads + SSE; one admin session drives dashboards
  for (let i = 0; i < SESSIONS; i++) sessions.push(await signIn("power@marshal.demo"));
  const admin = PROFILE === "all" || PROFILE === "dashboards" ? await signIn("admin@marshal.demo") : null;
  if (admin) sessions.push(admin);

  const deadline = Date.now() + DURATION_S * 1000;
  const work = [];
  for (let i = 0; i < SESSIONS; i++) {
    const { ctx, page } = sessions[i];
    if (PROFILE === "all" || PROFILE === "reads") work.push(readLoop(ctx, deadline));
    // one session drives SSE (model calls cost money; caps still apply)
    if ((PROFILE === "all" || PROFILE === "sse") && i === 0) work.push(sseLoop(page, deadline));
  }
  if (admin) work.push(dashboardLoop(admin.ctx, deadline));
  await Promise.all(work);
} finally {
  await browser.close();
}

// --- report ------------------------------------------------------------------

const elapsedS = (Date.now() - started.getTime()) / 1000;
const report = {
  started: started.toISOString(),
  target: APP,
  multiplier: MULT,
  sessions: SESSIONS,
  approx_users: SESSIONS * 10,
  duration_s: Math.round(elapsedS),
  requests,
  rps: Number((requests / elapsedS).toFixed(1)),
  errors: errors.length,
  metrics: {},
};
for (const [label, arr] of samples) {
  report.metrics[label] = {
    n: arr.length,
    p50: Math.round(pct(arr, 50)),
    p95: Math.round(pct(arr, 95)),
    p99: Math.round(pct(arr, 99)),
    max: Math.round(Math.max(...arr)),
    slo_ms: SLOS[label] ?? null,
    slo_met: SLOS[label] ? pct(arr, 95) < SLOS[label] : null,
  };
}

console.log("\n=== LOAD REPORT ===");
console.log(`requests=${report.requests} rps=${report.rps} errors=${report.errors}`);
for (const [label, m] of Object.entries(report.metrics)) {
  const verdict = m.slo_met === null ? "" : m.slo_met ? "  SLO OK" : "  SLO BREACH";
  console.log(
    `${label.padEnd(18)} n=${String(m.n).padStart(4)}  p50=${String(m.p50).padStart(5)}ms  ` +
      `p95=${String(m.p95).padStart(5)}ms  p99=${String(m.p99).padStart(5)}ms` +
      (m.slo_ms ? `  (target p95<${m.slo_ms}ms)${verdict}` : "")
  );
}
if (errors.length) {
  console.log("\nfirst errors:");
  for (const e of errors.slice(0, 8)) console.log(`  ${e}`);
}
console.log(`\nJSON: ${JSON.stringify(report)}`);

const breaches = Object.entries(report.metrics).filter(([, m]) => m.slo_met === false);
const serverErrors = errors.filter((e) => /HTTP 5\d\d/.test(e)).length;
if (SLO_CHECK && (breaches.length || serverErrors)) {
  console.error(`\nSLO CHECK FAILED: ${breaches.length} breach(es), ${serverErrors} 5xx`);
  process.exit(1);
}
console.log(breaches.length || serverErrors ? "\nLOAD RUN COMPLETE (with breaches)" : "\nLOAD RUN COMPLETE — all SLOs met");
