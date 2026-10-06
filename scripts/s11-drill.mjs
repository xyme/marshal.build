// S11 live drill: deploy → no-op update fails fast → real in-place update
// (supersede) → extend → history → teardown. Degrade/expiry sweeps are
// unit-verified now and drilled live in the S12 Beta E2E (policy TTLs).
import { chromium } from "playwright";

if (!process.env.APP_URL) {
  console.error("APP_URL is required (the installation's public URL, e.g. https://<your-domain>)");
  process.exit(2);
}
const APP = process.env.APP_URL;
const PASSWORD = process.env.DEMO_USER_PASSWORD;
const api = (p) => `${APP}/api/backend/v1${p}`;
const log = (m) => console.log(`${new Date().toISOString().slice(11, 19)}  ${m}`);
const fail = (m) => { console.error(`FAIL: ${m}`); process.exitCode = 1; };

const browser = await chromium.launch();

async function signIn(email) {
  const ctx = await browser.newContext();
  const p = await ctx.newPage();
  await p.goto(APP, { waitUntil: "networkidle", timeout: 45000 });
  await p.getByRole("button", { name: /sign in/i }).click();
  await p.waitForURL(/amazoncognito\.com/, { timeout: 30000 });
  await p.locator('input[name="username"]:visible').first().fill(email);
  await p.locator('input[name="password"]:visible').first().fill(PASSWORD);
  await p.locator('input[name="signInSubmitButton"]:visible, button[type="submit"]:visible').first().click();
  await p.waitForURL(/\/(home|onboarding|projects)/, { timeout: 45000 });
  return ctx;
}

async function buildReady(ctx, projectId) {
  let res = await ctx.request.post(api(`/projects/${projectId}/builds`), {
    data: { artifact_profile: "inline-cfn" },
  });
  const build = await res.json();
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 5000));
    const b = await (await ctx.request.get(api(`/builds/${build.id}`))).json();
    if (["ready", "failed", "cancelled"].includes(b.status)) return b;
  }
  return null;
}

async function waitStatus(ctx, projectId, want, tries = 120) {
  for (let i = 0; i < tries; i++) {
    await new Promise((r) => setTimeout(r, 10000));
    const d = await (await ctx.request.get(api(`/projects/${projectId}/deployment`))).json();
    if (i % 6 === 0) log(`  status: ${d.status}`);
    if (want.includes(d.status)) return d;
  }
  return null;
}

const power = await signIn("power@marshal.demo");
const admin = await signIn("admin@marshal.demo");
let projectId = null;

try {
  let res = await power.request.post(api("/projects"), {
    data: { name: `S11 Drill ${Date.now()}`, description: "deployment maturity drill" },
  });
  projectId = (await res.json()).id;
  log(`project ${projectId}`);
  await power.request.put(api(`/projects/${projectId}/specs/requirements`), {
    data: { content: "# Requirements\n\n## Goal\nA tiny status API: GET /status returns {ok:true, version:1}.\n" },
  });
  await power.request.put(api(`/projects/${projectId}/specs/design`), {
    data: { content: "# Design\n\n## Architecture\nAPI Gateway REST + one Lambda. No database. Version constant in code.\n" },
  });

  const build1 = await buildReady(power, projectId);
  if (build1?.status !== "ready") throw new Error(`build1 ${build1?.status}`);
  log(`build1 ready #${build1.content_hash.slice(0, 8)}`);

  // deploy (gate-aware)
  const deploy = (buildId) =>
    power.request.post(api(`/projects/${projectId}/deploy`), { data: { build_id: buildId } });
  res = await deploy(build1.id);
  if (res.status() === 403) {
    const risk = await (await power.request.get(api(`/projects/${projectId}/risk`))).json();
    await admin.request.post(api(`/risk-assessments/${risk.id}/decide`), {
      data: { outcome: "approve", notes: "S11 drill" },
    });
    res = await deploy(build1.id);
  }
  if (res.status() !== 202) throw new Error(`deploy HTTP ${res.status()}: ${await res.text()}`);
  let dep = await waitStatus(power, projectId, ["active", "failed"]);
  if (dep?.status !== "active") throw new Error(`deployment ${dep?.status}: ${dep?.error}`);
  log(`ACTIVE url=${dep.app_url}`);
  if (!dep.expires_at) fail("expires_at not set on activation (R5.1)");
  else log(`expires_at=${dep.expires_at} health=${dep.health}`);

  // no-op in-place update: same build → fails fast with "No changes"
  res = await deploy(build1.id);
  if (res.status() !== 202) throw new Error(`no-op update not accepted: ${res.status()}`);
  dep = await waitStatus(power, projectId, ["failed", "active"], 30);
  if (dep?.status === "failed" && /No changes/i.test(dep.error ?? "")) {
    log("no-op update failed fast with 'No changes' ✓");
  } else {
    fail(`expected fast no-op failure, got ${dep?.status}: ${dep?.error}`);
  }
  // prior stack must still be ACTIVE in history
  let history = await (await power.request.get(api(`/projects/${projectId}/deployments`))).json();
  const activeRows = history.filter((h) => h.status === "active");
  if (activeRows.length !== 1) fail(`expected 1 active row after no-op, got ${activeRows.length}`);

  // spec change → new build → REAL in-place update → supersede
  await power.request.put(api(`/projects/${projectId}/specs/requirements`), {
    data: { content: "# Requirements\n\n## Goal\nA tiny status API: GET /status returns {ok:true, version:2}. Also GET /ping returns pong.\n" },
  });
  const build2 = await buildReady(power, projectId);
  if (build2?.status !== "ready") throw new Error(`build2 ${build2?.status}`);
  log(`build2 ready #${build2.content_hash.slice(0, 8)}`);
  res = await deploy(build2.id);
  if (res.status() === 403) {
    const risk = await (await power.request.get(api(`/projects/${projectId}/risk`))).json();
    await admin.request.post(api(`/risk-assessments/${risk.id}/decide`), {
      data: { outcome: "approve", notes: "S11 drill v2" },
    });
    res = await deploy(build2.id);
  }
  if (res.status() !== 202) throw new Error(`update HTTP ${res.status()}: ${await res.text()}`);
  dep = await waitStatus(power, projectId, ["active", "failed"]);
  if (dep?.status !== "active") throw new Error(`update ended ${dep?.status}: ${dep?.error}`);
  if (dep.build_id !== build2.id) fail("active deployment does not carry build2 provenance");
  log(`UPDATED in place — active build ${dep.build_id.slice(0, 8)}`);
  const r2 = await power.request.get(dep.app_url, { timeout: 20000 }).catch(() => null);
  log(`app responds after update: HTTP ${r2 ? r2.status() : "none"}`);

  history = await (await power.request.get(api(`/projects/${projectId}/deployments`))).json();
  const superseded = history.filter((h) => h.status === "superseded");
  if (superseded.length !== 1) fail(`expected 1 superseded row, got ${superseded.length}`);
  else log(`history: ${history.length} attempts, 1 superseded ✓`);

  // extend within policy
  res = await power.request.post(api(`/projects/${projectId}/deployment/extend`), {
    data: { hours: 24 },
  });
  if (res.status() !== 200) fail(`extend HTTP ${res.status()}: ${await res.text()}`);
  else {
    const extended = await res.json();
    log(`extended to ${extended.expires_at} ✓`);
  }
  // over-max extension refused
  res = await power.request.post(api(`/projects/${projectId}/deployment/extend`), {
    data: { hours: 168 },
  });
  if (res.status() !== 422) fail(`over-max extend should 422, got ${res.status()}`);
  else log("over-max extension refused (422) ✓");

  // teardown
  res = await power.request.post(api(`/projects/${projectId}/deployment/teardown`));
  if (res.status() !== 202) throw new Error(`teardown HTTP ${res.status()}`);
  dep = await waitStatus(power, projectId, ["torn_down", "failed"]);
  log(dep?.status === "torn_down" ? "torn down cleanly" : `teardown ended ${dep?.status}`);
  if (dep?.status !== "torn_down") fail("teardown failed");
} catch (err) {
  fail(String(err));
} finally {
  if (projectId) {
    const res = await power.request.delete(api(`/projects/${projectId}`)).catch(() => null);
    log(`project cleanup: HTTP ${res?.status()}`);
  }
  await browser.close();
  console.log(process.exitCode ? "\nS11 DRILL FAILED" : "\nS11 DRILL PASSED");
}
