// P0 correctness live drill: B21 safe-default auth + strict policy ordering,
// B13 CDK parity, and B23 literal anchoring/gate observation.
// One real lease only (the CDK project); every setting and resource restores.
//
// DEMO_USER_PASSWORD='...' node scripts/drill-p0-correctness.mjs
import { chromium } from "playwright";

if (!process.env.APP_URL) {
  console.error("APP_URL is required (the installation's public URL, e.g. https://<your-domain>)");
  process.exit(2);
}
const APP = process.env.APP_URL;
const PASSWORD = process.env.DEMO_USER_PASSWORD;
if (!PASSWORD) throw new Error("DEMO_USER_PASSWORD required");
const api = (p) => `${APP}/api/backend/v1${p}`;
const checks = [];
const check = (name, ok, detail = "") => {
  checks.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
};
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const browser = await chromium.launch();
async function signIn(email) {
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
}

const power = await signIn("power@marshal.demo");
const admin = await signIn("admin@marshal.demo");
let baselineControls = null;
const projects = new Map(); // id -> {ctx, active}

async function controls() {
  return await (await admin.ctx.request.get(api("/admin/model-controls"))).json();
}
async function putControls(fresh, patch) {
  return admin.ctx.request.put(api("/admin/model-controls"), {
    data: {
      model_allowlist: fresh.model_allowlist,
      param_bounds: fresh.param_bounds,
      rate_limits: fresh.rate_limits ?? {},
      cost: fresh.cost ?? {},
      codegen: patch.codegen ?? fresh.codegen ?? {},
      deployment_policies: patch.deployment_policies ?? fresh.deployment_policies ?? {},
      security: fresh.security ?? {},
      feature_flags: fresh.feature_flags ?? {},
    },
  });
}
async function setEndpointPolicy(value) {
  const fresh = await controls();
  const next = { ...(fresh.deployment_policies ?? {}) };
  if (value === undefined) delete next.require_endpoint_auth;
  else next.require_endpoint_auth = value;
  return putControls(fresh, { deployment_policies: next });
}
async function setCodegenProvider(value) {
  const fresh = await controls();
  return putControls(fresh, { codegen: { ...(fresh.codegen ?? {}), provider: value } });
}
async function createProject(name, requirements, design) {
  const r = await power.ctx.request.post(api("/projects"), { data: { name } });
  const project = await r.json();
  projects.set(project.id, { active: false });
  await power.ctx.request.put(api(`/projects/${project.id}/specs/requirements`), { data: { content: requirements } });
  await power.ctx.request.put(api(`/projects/${project.id}/specs/design`), { data: { content: design } });
  await power.ctx.request.put(api(`/projects/${project.id}/specs/tasks`), { data: { content: "# Tasks\n- [ ] Implement the API\n" } });
  return project.id;
}
async function buildProject(projectId, profile) {
  const r = await power.ctx.request.post(api(`/projects/${projectId}/builds`), {
    data: { artifact_profile: profile },
  });
  if (r.status() !== 202) throw new Error(`build start HTTP ${r.status()}: ${await r.text()}`);
  const id = (await r.json()).id;
  for (let i = 0; i < 160; i++) {
    await wait(5000);
    const b = await (await power.ctx.request.get(api(`/builds/${id}`))).json();
    if (["ready", "failed", "cancelled"].includes(b.status)) return b;
  }
  throw new Error(`build ${id} timed out`);
}
async function approveIfHeld(projectId, buildId) {
  let r = await power.ctx.request.post(api(`/projects/${projectId}/deploy`), { data: { build_id: buildId } });
  if (r.status() === 403) {
    const risk = await (await power.ctx.request.get(api(`/projects/${projectId}/risk`))).json();
    const decision = await admin.ctx.request.post(api(`/admin/risk-assessments/${risk.id}/decide`), {
      data: { outcome: "approve", notes: "P0 correctness drill — disposable" },
    });
    if (!decision.ok()) throw new Error(`risk decision HTTP ${decision.status()}`);
    r = await power.ctx.request.post(api(`/projects/${projectId}/deploy`), { data: { build_id: buildId } });
  }
  return r;
}
async function waitDeployment(projectId) {
  for (let i = 0; i < 180; i++) {
    await wait(5000);
    const r = await power.ctx.request.get(api(`/projects/${projectId}/deployment`));
    if (r.status() === 404) continue;
    const d = await r.json();
    if (["active", "failed", "torn_down"].includes(d.status)) return d;
  }
  throw new Error("deployment timed out");
}
function firstGetPath(template) {
  const resources = template.Resources ?? {};
  const paths = {};
  function pathOf(id, depth = 0) {
    if (depth > 10 || !resources[id]) return null;
    if (paths[id]) return paths[id];
    const p = resources[id].Properties ?? {};
    let parent = "";
    if (p.ParentId?.Ref) parent = pathOf(p.ParentId.Ref, depth + 1) ?? "";
    const result = `${parent === "/" ? "" : parent}/${p.PathPart ?? ""}` || "/";
    paths[id] = result;
    return result;
  }
  for (const resource of Object.values(resources)) {
    if (resource.Type !== "AWS::ApiGateway::Method") continue;
    const p = resource.Properties ?? {};
    if (String(p.HttpMethod).toUpperCase() !== "GET" || !p.ApiKeyRequired) continue;
    if (p.ResourceId?.Ref) return pathOf(p.ResourceId.Ref) ?? "/";
    return "/";
  }
  return "/";
}
async function teardown(projectId) {
  const info = projects.get(projectId);
  if (!info?.active) return;
  const r = await power.ctx.request.post(api(`/projects/${projectId}/deployment/teardown`));
  if (r.status() !== 202) {
    console.error(`cleanup: teardown ${projectId} HTTP ${r.status()}`);
    return;
  }
  for (let i = 0; i < 140; i++) {
    await wait(5000);
    const d = await (await power.ctx.request.get(api(`/projects/${projectId}/deployment`))).json();
    if (d.status === "torn_down") {
      info.active = false;
      console.log(`cleanup: ${projectId} torn down`);
      return;
    }
    if (d.status === "failed") {
      console.error(`cleanup: teardown ${projectId} failed: ${d.error}`);
      return;
    }
  }
}

try {
  baselineControls = await controls();
  const baselinePolicy = baselineControls.deployment_policies?.require_endpoint_auth;
  const baselineProvider = baselineControls.codegen?.provider ?? "internal";
  check("sessions + controls", true, `policy=${baselinePolicy ?? "default(absent)"} provider=${baselineProvider}`);

  // A. Explicit PUBLIC build: static truth + strict refusal, zero lease.
  await setCodegenProvider("internal");
  await setEndpointPolicy("default");
  const publicId = await createProject(
    `P0 Public Drill ${Date.now()}`,
    "# Requirements\nEndpoint authentication: PUBLIC\n\nGET /status returns a health message.\n",
    "# Design\nOne Python Lambda behind API Gateway REST. GET /status returns JSON.\n",
  );
  const publicBuild = await buildProject(publicId, "inline-cfn");
  check("explicit PUBLIC build ready", publicBuild.status === "ready", publicBuild.error?.message ?? publicBuild.status);
  if (publicBuild.status !== "ready") throw new Error("public build failed");
  check(
    "manifest records explicit public intent",
    publicBuild.manifest?.endpoint_auth?.mode === "open" &&
      publicBuild.manifest?.endpoint_auth?.source === "explicit_public",
    JSON.stringify(publicBuild.manifest?.endpoint_auth),
  );
  const publicTemplate = await (await power.ctx.request.get(api(`/builds/${publicBuild.id}/artifacts/template.json`))).json();
  const publicDoc = JSON.parse(publicTemplate.content);
  check(
    "public artifact has no key infrastructure",
    !Object.values(publicDoc.Resources ?? {}).some((x) => x.Type === "AWS::ApiGateway::ApiKey") &&
      !publicDoc.Outputs?.ApiKeyId,
  );
  const riskBefore = await (await power.ctx.request.get(api(`/projects/${publicId}/risk`))).json();
  await setEndpointPolicy("always");
  const refused = await power.ctx.request.post(api(`/projects/${publicId}/deploy`), {
    data: { build_id: publicBuild.id },
  });
  const refusalBody = await refused.json().catch(() => ({}));
  check(
    "always policy refuses public preflight",
    refused.status() === 422 && refusalBody.detail?.code === "policy_endpoint_auth",
    `HTTP ${refused.status()} code=${refusalBody.detail?.code}`,
  );
  const riskAfter = await (await power.ctx.request.get(api(`/projects/${publicId}/risk`))).json();
  check(
    "strict refusal spends no new risk call",
    riskBefore.id === riskAfter.id && (riskBefore.timeline?.length ?? 0) === (riskAfter.timeline?.length ?? 0),
  );
  const noDep = await power.ctx.request.get(api(`/projects/${publicId}/deployment`));
  check("strict refusal creates no deployment/lease", noDep.status() === 404, `HTTP ${noDep.status()}`);
  await setEndpointPolicy("default");

  // B. No-signal CDK build: keyed by default, real deploy/reveal/smoke.
  await setCodegenProvider("runner");
  const cdkId = await createProject(
    `P0 CDK Keyed Drill ${Date.now()}`,
    "# Requirements\nA small internal status API. GET /status returns JSON with a short message.\n",
    "# Design\nAWS CDK TypeScript: RestApi + one Python Lambda. GET /status.\n",
  );
  const cdkBuild = await buildProject(cdkId, "cdk-app");
  check("no-signal CDK build ready", cdkBuild.status === "ready", cdkBuild.error?.message ?? cdkBuild.status);
  if (cdkBuild.status !== "ready") throw new Error("cdk build failed");
  check(
    "CDK manifest is keyed by platform default",
    cdkBuild.manifest?.endpoint_auth?.mode === "key_required" &&
      cdkBuild.manifest?.endpoint_auth?.source === "default",
    JSON.stringify(cdkBuild.manifest?.endpoint_auth),
  );
  const synthArtifact = await (await power.ctx.request.get(api(`/builds/${cdkBuild.id}/artifacts/synth/template.json`))).json();
  const synth = JSON.parse(synthArtifact.content);
  const types = Object.values(synth.Resources ?? {}).map((x) => x.Type);
  check(
    "synth contains key + plan + plan-key + ApiKeyId",
    types.includes("AWS::ApiGateway::ApiKey") &&
      types.includes("AWS::ApiGateway::UsagePlan") &&
      types.includes("AWS::ApiGateway::UsagePlanKey") &&
      !!synth.Outputs?.ApiKeyId,
  );
  check(
    "all non-OPTIONS business methods are protected",
    Object.values(synth.Resources ?? {})
      .filter((x) => x.Type === "AWS::ApiGateway::Method")
      .filter((x) => String(x.Properties?.HttpMethod).toUpperCase() !== "OPTIONS")
      .every((x) => x.Properties?.ApiKeyRequired === true),
  );
  const deploy = await approveIfHeld(cdkId, cdkBuild.id);
  if (deploy.status() !== 202) throw new Error(`deploy HTTP ${deploy.status()}: ${await deploy.text()}`);
  projects.get(cdkId).active = true;
  const dep = await waitDeployment(cdkId);
  check("CDK keyed deployment active", dep.status === "active", `${dep.status} ${dep.error ?? ""}`);
  if (dep.status !== "active") throw new Error("cdk deployment failed");
  const reveal = await power.ctx.request.post(api(`/projects/${cdkId}/deployment/api-key/reveal`));
  const revealed = reveal.ok() ? await reveal.json() : null;
  check("owner reveal returns live key", !!revealed?.value && !!revealed?.api_key_id, `HTTP ${reveal.status()}`);
  if (!revealed) throw new Error("key reveal failed");
  const route = firstGetPath(synth);
  const base = dep.app_url.replace(/\/$/, "");
  const anonymous = await power.ctx.request.get(`${base}${route}`, { timeout: 30000 });
  check("anonymous business call refused", anonymous.status() === 403, `${route} HTTP ${anonymous.status()}`);
  let keyedStatus = 0;
  for (let i = 0; i < 60; i++) {
    const keyed = await power.ctx.request.get(`${base}${route}`, {
      headers: { "x-api-key": revealed.value },
      timeout: 30000,
    }).catch(() => null);
    keyedStatus = keyed?.status() ?? 0;
    if (keyedStatus > 0 && keyedStatus !== 403) break;
    await wait(15000);
  }
  check("keyed business call reaches the app", keyedStatus > 0 && keyedStatus < 500 && keyedStatus !== 403, `${route} HTTP ${keyedStatus}`);
  const refreshedBuild = await (await power.ctx.request.get(api(`/builds/${cdkBuild.id}`))).json();
  check(
    "platform smoke did not run expected-key build keyless",
    refreshedBuild.manifest?.smoke?.reason !== "api_key_value_unavailable" ||
      refreshedBuild.manifest?.smoke?.passed === null,
    JSON.stringify(refreshedBuild.manifest?.smoke ?? {}),
  );

  // C. Build-only B23 observation. Pass = deterministic block OR all det checks met.
  await setCodegenProvider("internal");
  const confId = await createProject(
    `P0 Conformance Drill ${Date.now()}`,
    '# Requirements\nEndpoint authentication: PUBLIC\nThe request SHALL accept exactly {applicant_ref, annual_income, loan_amount}.\nThe decision_band SHALL be one of "likely", "possible", "refer".\n',
    "# Design\nOne Python Lambda computes the three exact decision bands.\n",
  );
  const confBuild = await buildProject(confId, "inline-cfn");
  const deterministic = (confBuild.manifest?.conformance?.verdicts ?? []).filter((v) => v.source === "deterministic");
  const violated = deterministic.filter((v) => v.verdict === "violated");
  if (violated.length) {
    check(
      "deterministic drift cannot become ready",
      confBuild.status === "failed" &&
        (confBuild.error?.findings ?? []).some((f) => ["field_contract", "enum_literals"].includes(f.check)),
      `${confBuild.status}, ${violated.length} violation(s)`,
    );
  } else {
    check(
      "literal anchoring produced a conforming ready build",
      confBuild.status === "ready" && deterministic.length >= 2 && deterministic.every((v) => v.verdict === "met"),
      `${confBuild.status}, deterministic=${deterministic.length}`,
    );
    console.log("INFO  live model did not drift this run; checked-in shared-finalizer tests are the deterministic block proof");
  }
} catch (err) {
  check("drill aborted", false, String(err).split("\n")[0]);
} finally {
  // Restore from fresh state so concurrent unrelated admin changes survive.
  try {
    if (baselineControls) {
      const fresh = await controls();
      const restoredPolicies = { ...(fresh.deployment_policies ?? {}) };
      const oldPolicy = baselineControls.deployment_policies?.require_endpoint_auth;
      if (oldPolicy === undefined) delete restoredPolicies.require_endpoint_auth;
      else restoredPolicies.require_endpoint_auth = oldPolicy;
      await putControls(fresh, {
        codegen: { ...(fresh.codegen ?? {}), provider: baselineControls.codegen?.provider ?? "internal" },
        deployment_policies: restoredPolicies,
      });
      console.log("cleanup: policy + codegen provider restored");
    }
  } catch (err) {
    console.error("cleanup: settings restore FAILED", err);
  }
  for (const projectId of [...projects.keys()].reverse()) {
    try {
      await teardown(projectId);
      const r = await power.ctx.request.delete(api(`/projects/${projectId}`));
      console.log(`cleanup: project ${projectId} DELETE HTTP ${r.status()}`);
    } catch (err) {
      console.error(`cleanup: project ${projectId} FAILED`, err);
    }
  }
  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}
