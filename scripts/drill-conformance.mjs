// Codegen-quality drill (B7/B8): rebuild the LIVE mortgage demo project and
// verify the new build carries a spec-conformance report — and that the build
// card renders it. Build-only: the active deployment is not redeployed.
//
//   DEMO_USER_PASSWORD='...' node scripts/drill-conformance.mjs
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
  if (!project) throw new Error("mortgage project missing");
  const pid = project.id;

  // Deployment snapshot BEFORE (must be unchanged after the drill)
  const depBefore = await (await power.ctx.request.get(api(`/projects/${pid}/deployment`))).json();
  const urlBefore = depBefore?.api_url ?? null;

  // Build (poll to terminal; ~2-4 min with generation + conformance review)
  const r0 = await power.ctx.request.post(api(`/projects/${pid}/builds`));
  if (r0.status() === 409) throw new Error("another build is in flight — rerun later");
  const build = await r0.json();
  check("build accepted", r0.status() === 202, build.id);

  let final = null;
  for (let i = 0; i < 90; i++) {
    await new Promise((res) => setTimeout(res, 5000));
    final = await (await power.ctx.request.get(api(`/builds/${build.id}`))).json();
    if (["ready", "failed", "cancelled"].includes(final.status)) break;
  }
  check("build reached terminal state", !!final && ["ready", "failed"].includes(final.status), final?.status);

  const manifest = final?.manifest ?? {};
  if (final?.status === "ready") {
    const rep = manifest.conformance;
    check("conformance report on manifest", !!rep, rep?.status);
    check("report status ok", rep?.status === "ok", rep?.review_error ? `review_error: ${rep.review_error}` : (rep?.model_id ?? ""));
    const verdicts = rep?.verdicts ?? [];
    check("verdicts present", verdicts.length >= 2, `${verdicts.length} verdicts`);
    const det = verdicts.filter((v) => v.source === "deterministic");
    check("deterministic contracts extracted", det.length >= 2, det.map((v) => `${v.check}:${v.verdict}`).join(", "));
    const violated = verdicts.filter((v) => v.verdict === "violated");
    console.log(`  summary: ${JSON.stringify(rep?.summary)}`);
    for (const v of verdicts) console.log(`  [${v.source}] ${v.verdict}: ${v.criterion}\n    evidence: ${v.evidence}`);
    if (violated.length > 0) {
      check("drift caught (violations reported as warnings on a READY build)", true, `${violated.length} violated`);
    } else {
      check("no drift this run — all deterministic contracts met (variance)", det.every((v) => v.verdict === "met"));
    }
    if (manifest.inline_retry) console.log(`  inline_retry: ${JSON.stringify(manifest.inline_retry)}`);

    // Build card renders the section
    await power.page.goto(`${APP}/projects/${pid}?tab=build`, { waitUntil: "networkidle", timeout: 45000 });
    const section = power.page.getByText("Spec conformance", { exact: false }).first();
    await section.waitFor({ state: "visible", timeout: 20000 });
    check("build card renders Spec conformance section", true);
  } else {
    // Failed build still yields drill evidence: retry record + actionable copy
    const findings = final?.error?.findings ?? [];
    console.log(`  findings: ${JSON.stringify(findings, null, 2)}`);
    check("inline_retry recorded when ceiling bound", !!manifest.inline_retry, JSON.stringify(manifest.inline_retry));
    check(
      "size finding copy is actionable",
      findings.some((f) => f.message.includes("inline ceiling") && f.message.includes("cdk-app")),
    );
  }

  // The LIVE deployment must be untouched
  const depAfter = await (await power.ctx.request.get(api(`/projects/${pid}/deployment`))).json();
  check("live deployment untouched", (depAfter?.api_url ?? null) === urlBefore && depAfter?.status === depBefore?.status,
    `${depAfter?.status} ${depAfter?.api_url ?? ""}`);
} finally {
  await browser.close();
}

const failed = checks.filter((c) => !c.ok);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
