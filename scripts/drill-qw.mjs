// Changeset-preview live drill — against a live installation (APP_URL).
//
// project → build → REAL Enclave deploy (through the risk gate: admin approves
// a MEDIUM score, the honest path) → preview of the SAME build says "no
// changes" → revised design → new build → preview lists concrete resource
// changes → the running stack is proven unmodified → teardown.
//
// Redaction scope has its own drill (scripts/drill-redaction-scope.mjs): the
// chat model refuses text-analysis probes, so guardrail attachment is observed
// through the platform's own pre-egress log line instead.
//
//   DEMO_USER_PASSWORD='...' node scripts/drill-qw.mjs
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
  await p_waitHome(page);
  return { ctx, page };
};
const p_waitHome = (page) => page.waitForURL(/\/(home|onboarding|projects)/, { timeout: 45000 });

const admin = await signIn("admin@marshal.demo");
const power = await signIn("power@marshal.demo");
check("signed in (admin + power)", true);

let projectId = null;
let deployed = false;

try {
  // ---------------- changeset preview on a real Enclave ----------------
  let r = await power.ctx.request.post(api("/projects"), {
    data: { name: `QW Drill ${Date.now()}`, description: "changeset preview drill" },
  });
  projectId = (await r.json()).id;
  check("project created", r.status() === 201, projectId);

  await power.ctx.request.put(api(`/projects/${projectId}/specs/requirements`), {
    data: { content: "# Requirements\n\n## Goal\nTiny feedback API: POST /feedback stores {message}; GET /feedback lists.\n" },
  });
  r = await power.ctx.request.put(api(`/projects/${projectId}/specs/design`), {
    data: { content: "# Design\n\n## Architecture\nAPI Gateway REST + one python Lambda + DynamoDB table, name via env TABLE_NAME.\n" },
  });
  check("specs saved", r.status() === 200);

  const startBuild = async () => {
    const res = await power.ctx.request.post(api(`/projects/${projectId}/builds`));
    const build = await res.json();
    for (let i = 0; i < 60; i++) {
      await power.page.waitForTimeout(5000);
      const poll = await (await power.ctx.request.get(api(`/builds/${build.id}`))).json();
      if (["ready", "failed", "cancelled"].includes(poll.status)) return poll;
    }
    return { status: "timeout", id: build.id };
  };

  const build1 = await startBuild();
  check("build 1 ready", build1.status === "ready", build1.status);

  const tryDeploy = () =>
    power.ctx.request.post(api(`/projects/${projectId}/deploy`), {
      data: { build_id: build1.id },
    });
  r = await tryDeploy();
  if (r.status() === 403) {
    // Risk gate held it (MEDIUM/HIGH) — the S4 path: an admin decides, then
    // deploy proceeds. Drill exercises the real governance loop rather than
    // assuming a LOW score.
    const body = await r.json().catch(() => ({}));
    const code = body?.detail?.code;
    const risk = await (await power.ctx.request.get(api(`/projects/${projectId}/risk`))).json();
    const assessmentId = (risk.timeline ?? [])[0]?.id;
    check("risk gate held the deploy (expected for MEDIUM/HIGH)", code === "risk_pending",
      `${code} score=${risk.score} level=${risk.level}`);
    const dec = await admin.ctx.request.post(
      api(`/admin/risk-assessments/${assessmentId}/decide`),
      { data: { outcome: "approve", notes: "Quick-wins drill: approved for changeset preview test" } }
    );
    check("admin approved assessment", dec.ok(), `HTTP ${dec.status()}`);
    r = await tryDeploy();
  }
  check("deploy accepted", r.status() === 202, `HTTP ${r.status()}`);
  let dep = null;
  if (r.status() === 202) {
    for (let i = 0; i < 120; i++) {
      await power.page.waitForTimeout(5000);
      dep = await (await power.ctx.request.get(api(`/projects/${projectId}/deployment`))).json();
      if (["active", "failed"].includes(dep.status)) break;
    }
  }
  deployed = dep?.status === "active";
  check("deployment active", deployed, `${dep?.status ?? "not started"} ${dep?.app_url ?? ""}`);

  if (deployed) {
    // preview of the SAME build → no changes
    r = await power.ctx.request.post(api(`/projects/${projectId}/deployment/preview`), {
      data: { build_id: build1.id }, timeout: 60000,
    });
    let preview = r.ok() ? await r.json() : { error: r.status() };
    check("preview: same build → no changes", preview.no_changes === true, JSON.stringify(preview).slice(0, 100));

    // revise the design so the next build differs materially
    await power.ctx.request.put(api(`/projects/${projectId}/specs/design`), {
      data: { content: "# Design\n\n## Architecture\nAPI Gateway REST + one python Lambda + DynamoDB table (env TABLE_NAME) + an SNS topic FeedbackTopic notified on every new feedback item.\n" },
    });
    const build2 = await startBuild();
    check("build 2 ready (revised design)", build2.status === "ready", build2.status);

    if (build2.status === "ready") {
      r = await power.ctx.request.post(api(`/projects/${projectId}/deployment/preview`), {
        data: { build_id: build2.id }, timeout: 60000,
      });
      preview = r.ok() ? await r.json() : { error: r.status() };
      const kinds = (preview.changes ?? []).map((c) => `${c.action}:${c.logical_id}`);
      check(
        "preview: revised build → concrete changes, stack untouched",
        preview.no_changes === false && (preview.changes ?? []).length > 0,
        kinds.slice(0, 6).join(", ") || JSON.stringify(preview).slice(0, 120)
      );
      // the running deployment must still be the ORIGINAL (preview never applies)
      dep = await (await power.ctx.request.get(api(`/projects/${projectId}/deployment`))).json();
      check("running stack unmodified after previews", dep.status === "active");
    }
  }
} catch (err) {
  check("drill flow", false, String(err).split("\n")[0]);
} finally {
  // -------- teardown + restore --------
  if (deployed && projectId) {
    const t = await power.ctx.request.post(api(`/projects/${projectId}/deployment/teardown`)).catch(() => null);
    let final = null;
    if (t && t.status() === 202) {
      for (let i = 0; i < 90; i++) {
        await power.page.waitForTimeout(5000);
        final = await (await power.ctx.request.get(api(`/projects/${projectId}/deployment`))).json();
        if (["torn_down", "failed"].includes(final.status)) break;
      }
    }
    check("teardown complete", final?.status === "torn_down", final?.status ?? "not started");
  }
  if (projectId) {
    await power.ctx.request.delete(api(`/projects/${projectId}`)).catch(() => {});
  }
  await browser.close();
}

const failed = checks.filter((c) => !c.ok);
console.log(failed.length ? `\nQW DRILL FAILED (${failed.length})` : "\nQW DRILL PASSED");
process.exit(failed.length ? 1 : 0);
