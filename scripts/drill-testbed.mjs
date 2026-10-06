// B20 deployment-modes drill (deployment-modes spec R3.1): live proof that
// (a) Full Governance still ENFORCES (regression pin), (b) a Testbed deploy
// rides the advisory gate, (c) minted credentials are attributed, LIMITED
// (allowed action works, custody actions AccessDenied), renewable, and
// audited, (d) the policy kill-switch refuses both deploy and mint.
//
//   DEMO_USER_PASSWORD='...' node scripts/drill-testbed.mjs
//
// Requires the aws CLI on PATH (used to exercise the vended credentials).
import { execFileSync } from "node:child_process";
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

// aws CLI under the vended session (profile stripped so nothing ambient leaks in)
const awsCli = (creds, args) => {
  const env = { ...process.env };
  delete env.AWS_PROFILE;
  env.AWS_ACCESS_KEY_ID = creds.access_key_id;
  env.AWS_SECRET_ACCESS_KEY = creds.secret_access_key;
  env.AWS_SESSION_TOKEN = creds.session_token;
  env.AWS_DEFAULT_REGION = creds.region ?? "us-east-1";
  try {
    const out = execFileSync("aws", args, { env, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    return { ok: true, out };
  } catch (err) {
    return { ok: false, out: `${err.stdout ?? ""}${err.stderr ?? ""}` };
  }
};

let pid = null;
let disabledRestore = null;
const power = await signIn("power@marshal.demo");
const admin = await signIn("admin@marshal.demo");
check("sessions established", true);

try {
  // ---- project with NO spec docs: the strongest gate contrast
  let r = await power.ctx.request.post(api("/projects"), { data: { name: "Testbed Drill (B20)" } });
  const project = await r.json();
  pid = project.id;
  check("drill project created", r.status() === 201, pid);

  // ---- (a) Full Governance regression: enforce still blocks (no spec -> 403)
  r = await power.ctx.request.post(api(`/projects/${pid}/deploy`), { data: {} });
  const fgDetail = r.status() === 403 ? (await r.json()).detail : null;
  check("full-governance deploy still ENFORCES (403 pre-lease)", r.status() === 403, `code=${fgDetail?.code}`);

  // ---- risk payload carries mode context
  const risk = await (await power.ctx.request.get(api(`/projects/${pid}/risk`))).json();
  check(
    "risk payload carries gate_modes + default_mode",
    risk.gate_modes?.full_governance === "enforce" && risk.gate_modes?.testbed === "advisory" && risk.default_mode === "full_governance",
    JSON.stringify(risk.gate_modes ?? {})
  );

  // ---- (b) Testbed deploy rides the advisory gate (same project, same docs-less state)
  r = await power.ctx.request.post(api(`/projects/${pid}/deploy`), { data: { mode: "testbed" } });
  const accepted = r.status() === 202 ? await r.json() : null;
  check("testbed deploy accepted through advisory gate", r.status() === 202 && accepted?.mode === "testbed", `HTTP ${r.status()} mode=${accepted?.mode}`);
  if (r.status() !== 202) throw new Error("testbed deploy refused");

  // ---- wait for active (ISB lease + sample app stack)
  let dep = null;
  for (let i = 0; i < 160; i++) {
    await power.page.waitForTimeout(5000);
    dep = await (await power.ctx.request.get(api(`/projects/${pid}/deployment`))).json();
    if (["active", "failed", "torn_down"].includes(dep.status)) break;
  }
  check("testbed deployment active", dep?.status === "active", `status=${dep?.status} account=${dep?.lease_account_id}`);
  if (dep?.status !== "active") throw new Error(`deployment ended ${dep?.status}: ${dep?.error ?? ""}`);
  check(
    "timeline names the Testbed account",
    (dep.timeline ?? []).some((t) => String(t.detail ?? "").includes("Testbed account")),
    ""
  );

  // ---- (c) mint credentials
  r = await power.ctx.request.post(api(`/projects/${pid}/deployment/testbed-credentials`));
  const mintBody = await r.json().catch(() => ({}));
  const creds = r.status() === 200 ? mintBody : null;
  check(
    "credentials minted",
    !!creds?.access_key_id && !!creds?.session_token,
    creds ? `session ${creds.session_name}` : `HTTP ${r.status()} ${JSON.stringify(mintBody?.detail ?? mintBody ?? {}).slice(0, 400)}`
  );
  if (!creds) throw new Error("mint failed");
  check("console URL is a federation login link", String(creds.console_url ?? "").startsWith("https://signin.aws.amazon.com/federation?Action=login"), "");
  check("session named for the user", /^marshal-[0-9a-f]{8}$/.test(creds.session_name ?? ""), creds.session_name);

  // identity: attribution rides the assumed-role ARN
  const ident = awsCli(creds, ["sts", "get-caller-identity", "--output", "json"]);
  const arn = ident.ok ? JSON.parse(ident.out).Arn : "";
  check(
    "CloudTrail attribution: ARN carries role + session name",
    ident.ok && arn.includes(`assumed-role/MarshalTestbedUserRole/${creds.session_name}`) && arn.includes(creds.account_id),
    arn || ident.out.slice(0, 200)
  );

  // allowed: an action inside BOTH walls (marshal policy + ISB SCP) — the
  // deploy pipeline proves lambda is SCP-permitted in pool accounts
  const lambdaRead = awsCli(creds, ["lambda", "list-functions", "--max-items", "1", "--output", "json"]);
  check("allowed action works (lambda read)", lambdaRead.ok, lambdaRead.ok ? "" : lambdaRead.out.slice(0, 160));

  // informational: where does ISB's OUTER wall stand on self-service quota
  // raises? (marshal's role allows it; the SCP verdict is ISB's to change.)
  const quotas = awsCli(creds, ["service-quotas", "list-service-quotas", "--service-code", "lambda", "--max-items", "1", "--output", "json"]);
  console.log(`INFO  servicequotas via vended session: ${quotas.ok ? "SCP permits" : "SCP DENIES (quota raises need an ISB SCP amendment)"}`);

  // denied: identity mutation
  const iamDenied = awsCli(creds, ["iam", "create-user", "--user-name", "b20-drill-denied"]);
  check("denied: iam:CreateUser AccessDenied", !iamDenied.ok && /AccessDenied|explicit deny/i.test(iamDenied.out), iamDenied.out.split("\n")[0]?.slice(0, 160));

  // denied: marshal custody (deleting the marshal-managed stack)
  const cfnDenied = awsCli(creds, ["cloudformation", "delete-stack", "--stack-name", dep.stack_name]);
  check("denied: delete marshal stack AccessDenied", !cfnDenied.ok && /AccessDenied|explicit deny/i.test(cfnDenied.out), cfnDenied.out.split("\n")[0]?.slice(0, 160));

  // stack still standing after the denied delete
  const still = await (await power.ctx.request.get(api(`/projects/${pid}/deployment`))).json();
  check("stack unharmed after denied delete", still.status === "active", still.status);

  // renewable
  r = await power.ctx.request.post(api(`/projects/${pid}/deployment/testbed-credentials`));
  check("re-mint works (renewable while lease lives)", r.status() === 200, `HTTP ${r.status()}`);

  // ---- audit trail: issuance is SECURITY-classed
  const auditResp = await admin.ctx.request.get(api(`/admin/audit-logs?category=security&q=testbed`));
  const audit = await auditResp.json().catch(() => ({}));
  const auditRow = (audit.items ?? []).find((e) => e.action === "testbed_credentials_issued");
  check(
    "SECURITY audit row for issuance",
    !!auditRow,
    auditRow ? auditRow.action : `HTTP ${auditResp.status()} ${JSON.stringify(audit).slice(0, 160)}`
  );

  // ---- (d) policy kill-switch
  const controls = await (await admin.ctx.request.get(api("/admin/model-controls"))).json();
  disabledRestore = controls.deployment_policies ?? {};
  const flip = async (policies) =>
    admin.ctx.request.put(api("/admin/model-controls"), {
      data: { ...controls, deployment_policies: policies },
    });
  let fr = await flip({ ...disabledRestore, testbed_enabled: false });
  check("policy flip accepted", fr.status() === 200, `HTTP ${fr.status()}`);
  r = await power.ctx.request.post(api(`/projects/${pid}/deployment/testbed-credentials`));
  check("mint refused when disabled (422)", r.status() === 422, `HTTP ${r.status()}`);
  const p2 = await (await power.ctx.request.post(api("/projects"), { data: { name: "Testbed Drill disabled probe" } })).json();
  r = await power.ctx.request.post(api(`/projects/${p2.id}/deploy`), { data: { mode: "testbed" } });
  const p2detail = r.status() === 422 ? (await r.json()).detail : null;
  check("testbed deploy refused when disabled (422 policy_testbed_disabled)", r.status() === 422 && p2detail?.code === "policy_testbed_disabled", `HTTP ${r.status()}`);
  fr = await flip(disabledRestore);
  check("policy restored", fr.status() === 200, `HTTP ${fr.status()}`);
  disabledRestore = null;
} catch (err) {
  check("drill aborted", false, String(err).split("\n")[0]);
} finally {
  // restore policy if the flip happened but restore didn't
  if (disabledRestore) {
    try {
      const controls = await (await admin.ctx.request.get(api("/admin/model-controls"))).json();
      await admin.ctx.request.put(api("/admin/model-controls"), {
        data: { ...controls, deployment_policies: disabledRestore },
      });
      console.log("policy restored in cleanup");
    } catch { /* recorded below by the operator */ }
  }
  // teardown the drill deployment + verify reclamation
  if (pid) {
    try {
      const r = await power.ctx.request.post(api(`/projects/${pid}/deployment/teardown`));
      if (r.status() === 202) {
        let dep = null;
        for (let i = 0; i < 120; i++) {
          await power.page.waitForTimeout(5000);
          dep = await (await power.ctx.request.get(api(`/projects/${pid}/deployment`))).json();
          if (["torn_down", "failed"].includes(dep.status)) break;
        }
        check("teardown clean (lease terminated with the account)", dep?.status === "torn_down", dep?.status);
      } else {
        console.log(`teardown skipped (HTTP ${r.status()})`);
      }
    } catch (err) {
      check("teardown", false, String(err).split("\n")[0]);
    }
  }
  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}
