// S18-6 dark browser drill — the studio surface driven as a user would (R6.1),
// on production, flag OFF (admin dark preview). Complements ui-smoke's render/
// a11y checks with the interactive path: stream a chat turn beside the artifact
// pane, generate the spec set, watch the checkpoint update the Spec tab, verify
// requested-vs-effective in the rail and the export affordances.
//
//   DEMO_USER_PASSWORD='...' node scripts/drill-s18.mjs
import { chromium } from "playwright";
import os from "node:os";
import path from "node:path";

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
const shoot = async (page, tag) => {
  const p = path.join(os.tmpdir(), `drill-s18-${tag}.png`);
  await page.screenshot({ path: p, fullPage: true }).catch(() => {});
  console.log(`  screenshot: ${p}`);
};

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await ctx.newPage();
let sessionId = null;

try {
  // admin sign-in (dark preview persona)
  await page.goto(APP, { waitUntil: "networkidle", timeout: 45000 });
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL(/amazoncognito\.com/, { timeout: 30000 });
  await page.locator('input[name="username"]:visible, input[id="signInFormUsername"]:visible').first().fill("admin@marshal.demo");
  await page.locator('input[name="password"]:visible, input[id="signInFormPassword"]:visible').first().fill(PASSWORD);
  await page.locator('input[name="signInSubmitButton"]:visible, button[type="submit"]:visible, input[type="submit"]:visible').first().click();
  await page.waitForURL(/\/(home|onboarding|projects)/, { timeout: 45000 });
  check("admin signed in", true);

  const res = await ctx.request.post(api("/chat/sessions"), { data: {} });
  sessionId = (await res.json()).id;
  check("session created", !!sessionId, sessionId);

  // 1. studio opens dark with banner
  await page.goto(`${APP}/studio/${sessionId}`, { waitUntil: "networkidle", timeout: 45000 });
  check("dark banner", await page.getByText(/dark launch/i).first().isVisible().catch(() => false));

  // 2. stream a chat turn beside the pane
  const composer = page.locator("textarea").first();
  await composer.fill("We need an internal FAQ assistant for HR policy documents. Keep it short.");
  await composer.press("Enter");
  await page.waitForTimeout(1000);
  check("user message rendered", await page.getByText(/FAQ assistant for HR/).first().isVisible().catch(() => false));
  // wait for the assistant stream to finish (Composer re-enables)
  await page.waitForFunction(() => !document.querySelector("textarea")?.disabled, null, { timeout: 60000 }).catch(() => {});
  const threadText = await page.locator("body").innerText();
  check("assistant streamed a reply", threadText.length > 400, `${threadText.length} chars on page`);
  await shoot(page, "chat-streamed");

  // 3. staleness honesty appears once messages outpace the (absent) spec — n/a
  //    pre-spec; instead: generate the spec set and watch the checkpoint land.
  const generate = page.getByRole("button", { name: /generate spec/i }).first();
  const canGenerate = await generate.isVisible().catch(() => false);
  check("generate affordance visible", canGenerate);
  const visible = (locator, timeout) =>
    locator.waitFor({ state: "visible", timeout }).then(() => true).catch(() => false);
  if (canGenerate) {
    await generate.click();
    // per-doc progress renders in the Spec tab, then documents appear
    const progress = await visible(
      page.getByText(/Generating the spec set|Generating requirements/i).first(),
      20000
    );
    check("generation progress renders in Spec tab", progress);
    // checkpoint: rendered markdown appears without a manual reload (SSE-driven)
    const specLanded = await visible(
      page.locator('[role="tabpanel"] h1, [role="tabpanel"] h2').first(),
      240000
    );
    await page.waitForTimeout(2000);
    check("checkpoint updated Spec tab (no reload)", specLanded);
    await shoot(page, "spec-checkpoint");
  }

  // 4. rail: set temperature; requested value persists via PATCH + effective echo
  const slider = page.locator('input[type="range"]').first();
  if (await slider.isVisible().catch(() => false)) {
    await slider.focus();
    await slider.press("ArrowRight");
    await page.waitForTimeout(1500);
    const eff = await (await ctx.request.get(api(`/chat/sessions/${sessionId}/effective-params`))).json();
    check(
      "rail change persisted (requested temp set)",
      typeof eff.requested?.temperature === "number",
      JSON.stringify(eff.requested)
    );
  } else {
    check("rail change persisted (requested temp set)", false, "slider not visible");
  }

  // 5. export tab shows the spec zip once the project exists
  await page.getByRole("tab", { name: "Export" }).click();
  const zipLink = await visible(page.getByText(/Download spec zip/i).first(), 10000);
  check("export tab offers spec zip", zipLink);
  await shoot(page, "export-tab");

  // 6. deep link back to chat carries the session
  await page.getByRole("link", { name: /open in chat/i }).click();
  await page.waitForURL(/\/chat\?session=/, { timeout: 20000 });
  const chatCarried = await visible(page.getByText(/FAQ assistant for HR/).first(), 30000);
  check("deep link back to /chat carries session", chatCarried);
} catch (err) {
  check("drill flow", false, String(err).split("\n")[0]);
  await shoot(page, "error");
} finally {
  if (sessionId) await ctx.request.delete(api(`/chat/sessions/${sessionId}`)).catch(() => {});
  await browser.close();
}

const failed = checks.filter((c) => !c.ok);
console.log(failed.length ? `\nS18 DRILL FAILED (${failed.length})` : "\nS18 DRILL PASSED");
process.exit(failed.length ? 1 : 0);
