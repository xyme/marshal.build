// One-off live drill (S8 R5): spec → build → deploy built artifact → URL responds → teardown.
// Run: DEMO_USER_PASSWORD='...' node deploy-drill.mjs [inline-cfn|cdk-app]
import { chromium } from "playwright";

if (!process.env.APP_URL) {
  console.error("APP_URL is required (the installation's public URL, e.g. https://<your-domain>)");
  process.exit(2);
}
const APP = process.env.APP_URL;
const PASSWORD = process.env.DEMO_USER_PASSWORD;
const api = (p) => `${APP}/api/backend/v1${p}`;
const log = (m) => console.log(`${new Date().toISOString().slice(11, 19)}  ${m}`);
const fail = (m) => {
  console.error(`FAIL: ${m}`);
  process.exitCode = 1;
};

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

const power = await signIn("power@marshal.demo");
const admin = await signIn("admin@marshal.demo");
let projectId = null;

try {
  // project + specs
  let res = await power.request.post(api("/projects"), {
    data: { name: `S8 Deploy Drill ${Date.now()}`, description: "live built-artifact deploy" },
  });
  projectId = (await res.json()).id;
  log(`project ${projectId}`);
  await power.request.put(api(`/projects/${projectId}/specs/requirements`), {
    data: { content: "# Requirements\n\n## Goal\nA tiny feedback-collector API: visitors POST a feedback message; the team GETs the list back.\n\n## Acceptance\n- POST /feedback stores {message}\n- GET /feedback returns stored items\n" },
  });
  await power.request.put(api(`/projects/${projectId}/specs/design`), {
    data: { content: "# Design\n\n## Architecture\nAPI Gateway REST + one Lambda (python) + DynamoDB table. Lambda handles both routes; table name via env var TABLE_NAME.\n" },
  });

  // build → ready
  const profile = process.argv[2] ?? "inline-cfn";
  res = await power.request.post(api(`/projects/${projectId}/builds`), {
    data: { artifact_profile: profile },
  });
  const build = await res.json();
  log(`build ${build.id} started`);
  let ready = null;
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 5000));
    const b = await (await power.request.get(api(`/builds/${build.id}`))).json();
    if (["ready", "failed", "cancelled"].includes(b.status)) { ready = b; break; }
  }
  if (ready?.status !== "ready") throw new Error(`build ${ready?.status}: ${JSON.stringify(ready?.error)}`);
  log(`build ready #${ready.content_hash.slice(0, 8)} (profile ${ready.artifact_profile}, engine ${ready.manifest?.engine?.name ?? "internal"})`);
  if (profile === "cdk-app") {
    const bundle = await power.request.get(api(`/builds/${build.id}/bundle`));
    const body = await bundle.body();
    log(`bundle: HTTP ${bundle.status()} ${body.length} bytes (repo layout)`);
    if (bundle.status() !== 200 || body.length < 1000) fail("cdk bundle looks wrong");
  }

  // deploy (risk gate aware)
  const tryDeploy = () =>
    power.request.post(api(`/projects/${projectId}/deploy`), { data: { build_id: build.id } });
  res = await tryDeploy();
  if (res.status() === 403) {
    const detail = (await res.json()).detail;
    log(`gate: ${detail.code} — deciding as admin`);
    const risk = await (await power.request.get(api(`/projects/${projectId}/risk`))).json();
    const decide = await admin.request.post(api(`/risk-assessments/${risk.id}/decide`), {
      data: { outcome: "approve", notes: "S8 deploy drill" },
    });
    if (decide.status() !== 200) throw new Error(`admin decide failed ${decide.status()}`);
    res = await tryDeploy();
  }
  if (res.status() !== 202) throw new Error(`deploy HTTP ${res.status()}: ${await res.text()}`);
  log("deploy accepted — polling (lease + stack can take minutes)");

  let dep = null;
  for (let i = 0; i < 180; i++) {
    await new Promise((r) => setTimeout(r, 10000));
    const d = await (await power.request.get(api(`/projects/${projectId}/deployment`))).json();
    if (i % 6 === 0) log(`  status: ${d.status}`);
    if (["active", "failed"].includes(d.status)) { dep = d; break; }
  }
  if (dep?.status !== "active") throw new Error(`deployment ${dep?.status}: ${dep?.error}`);
  log(`ACTIVE — build_id=${dep.build_id?.slice(0, 8)} url=${dep.app_url}`);
  if (!dep.build_id) fail("deployment lacks build provenance");

  // hit the generated app
  const urls = [`${dep.app_url.replace(/\/$/, "")}/feedback`, dep.app_url];
  let hit = null;
  for (const u of urls) {
    const r = await power.request.get(u, { timeout: 20000 }).catch(() => null);
    if (r && r.status() < 500) { hit = `${u} → HTTP ${r.status()}`; if (r.status() === 200) break; }
  }
  if (!hit) fail("generated app URL did not respond");
  else log(`app responds: ${hit}`);

  // teardown
  res = await power.request.post(api(`/projects/${projectId}/deployment/teardown`));
  if (res.status() !== 202) throw new Error(`teardown HTTP ${res.status()}`);
  log("teardown accepted — polling");
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 10000));
    const d = await (await power.request.get(api(`/projects/${projectId}/deployment`))).json();
    if (i % 6 === 0) log(`  status: ${d.status}`);
    if (["torn_down", "failed"].includes(d.status)) {
      if (d.status !== "torn_down") fail(`teardown ended ${d.status}: ${d.error}`);
      else log("torn down cleanly");
      break;
    }
  }
} catch (err) {
  fail(String(err));
} finally {
  if (projectId) {
    const res = await power.request.delete(api(`/projects/${projectId}`)).catch(() => null);
    log(`project cleanup: HTTP ${res?.status()}`);
  }
  await browser.close();
  console.log(process.exitCode ? "\nDRILL FAILED" : "\nDRILL PASSED");
}
