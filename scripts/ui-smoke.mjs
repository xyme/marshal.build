// Headless UI smoke probe for the deployed marshal app.
//
// Verifies the real browser path that manual click-throughs and curl miss:
// sign-in via Cognito hosted UI, core modals opening, and the Next.js
// /api/backend/* proxy returning no 5xx (catches keep-alive/socket-reuse
// regressions between CloudFront -> frontend -> Service Connect -> backend).
//
// Usage:
//   npx playwright install chromium   # once per machine
//   DEMO_USER_PASSWORD='...' node scripts/ui-smoke.mjs
//
// Env:
//   APP_URL             required (installation public URL)
//   SMOKE_EMAIL         default power@marshal.demo
//   DEMO_USER_PASSWORD  required
//   HAMMER_COUNT        sequential authed GETs through the proxy (default 40)
//
// Exit code 0 = all checks passed; 1 = any failure. Screenshots on failure
// go to the OS temp dir (path printed).
//
// Intentionally NOT wired into CI: needs live creds + deployed stack.
// Run after every cloud deploy that touches frontend, proxy, or networking.
import { chromium } from "playwright";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import zlib from "node:zlib";

if (!process.env.APP_URL) {
  console.error("APP_URL is required (the installation's public URL, e.g. https://<your-domain>)");
  process.exit(2);
}
const APP = process.env.APP_URL;
const EMAIL = process.env.SMOKE_EMAIL ?? "power@marshal.demo";
const PASSWORD = process.env.DEMO_USER_PASSWORD;
// Admin-context flows sign in as a DEDICATED smoke admin (13.5R): the real
// admin@marshal.demo is TOTP-enrolled, which correctly blocks password-only
// headless sign-in. smoke-admin carries a PLATFORM-HELD TOTP seed
// (marshal/smoke/admin-totp, 13.5T) so its hosted-UI challenge is answered
// in-script — fetch it before running:
//   SMOKE_ADMIN_TOTP_SECRET=$(aws secretsmanager get-secret-value \
//     --secret-id marshal/smoke/admin-totp --query SecretString --output text)
const SMOKE_ADMIN = process.env.SMOKE_ADMIN_EMAIL ?? "smoke-admin@marshal.demo";
const SMOKE_ADMIN_TOTP_SECRET = process.env.SMOKE_ADMIN_TOTP_SECRET ?? "";

/** RFC-6238 TOTP (SHA1/30s/6 digits) from a base32 seed — stdlib only. */
function totpCode(seed) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const clean = seed.toUpperCase().replace(/[^A-Z2-7]/g, "");
  let bits = "";
  for (const c of clean) bits += alphabet.indexOf(c).toString(2).padStart(5, "0");
  const bytes = Buffer.from((bits.match(/.{8}/g) ?? []).map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
  const digest = crypto.createHmac("sha1", bytes).update(counter).digest();
  const offset = digest[digest.length - 1] & 0xf;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

/** Cognito accepts each TOTP code once; consecutive sign-ins inside one
 * 30s window fail as code reuse (learned 13.5T: the 4d2 template-admin
 * sign-in burned s18's window). Wait for a fresh window when needed. */
let lastTotpWindow = -1;
async function freshTotpCode() {
  let win = Math.floor(Date.now() / 30000);
  if (win === lastTotpWindow) {
    const waitMs = (win + 1) * 30000 - Date.now() + 500;
    await new Promise((r) => setTimeout(r, waitMs));
    win = Math.floor(Date.now() / 30000);
  }
  lastTotpWindow = win;
  return totpCode(SMOKE_ADMIN_TOTP_SECRET);
}

/** Answer the hosted-UI TOTP challenge when Cognito presents one. */
async function answerTotpChallenge(p, email) {
  if (email !== SMOKE_ADMIN) return; // only the smoke admin is enrolled
  const input = p
    .locator(
      'input[name="totpCode"]:visible, input[id="totpCodeInput"]:visible, ' +
        'input[autocomplete="one-time-code"]:visible, input[name="softwareTokenMfaCode"]:visible'
    )
    .first();
  const appeared = await input.waitFor({ timeout: 6000 }).then(() => true).catch(() => false);
  if (!appeared) return;
  if (!SMOKE_ADMIN_TOTP_SECRET) {
    throw new Error("TOTP challenge shown but SMOKE_ADMIN_TOTP_SECRET is unset");
  }
  await input.fill(await freshTotpCode());
  await p
    .locator(
      'input[name="signInSubmitButton"]:visible, button[type="submit"]:visible, input[type="submit"]:visible'
    )
    .first()
    .click();
}
/** Minimal STORED-entry zip (pdf-docx AC-6 fixture) — stdlib only. */
function buildStoredZip(entries) {
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const [name, text] of entries) {
    const nameBuf = Buffer.from(name);
    const data = Buffer.from(text);
    const crc = zlib.crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4); // version needed
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18); // compressed (stored)
    local.writeUInt32LE(data.length, 22); // uncompressed
    local.writeUInt16LE(nameBuf.length, 26);
    locals.push(local, nameBuf, data);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 6);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBuf.length, 28);
    central.writeUInt32LE(offset, 42);
    centrals.push(central, nameBuf);
    offset += 30 + nameBuf.length + data.length;
  }
  const centralStart = offset;
  const centralBuf = Buffer.concat(centrals);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(entries.length, 8);
  eocd.writeUInt16LE(entries.length, 10);
  eocd.writeUInt32LE(centralBuf.length, 12);
  eocd.writeUInt32LE(centralStart, 16);
  return Buffer.concat([...locals, centralBuf, eocd]);
}

const HAMMER_COUNT = Number(process.env.HAMMER_COUNT ?? 40);
const SHOT_DIR = os.tmpdir();

if (!PASSWORD) {
  console.error("DEMO_USER_PASSWORD is required");
  process.exit(1);
}

const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];
const checks = []; // { name, ok, detail }
let guidedSessionId = null;
const check = (name, ok, detail = "") => {
  checks.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
};

const browser = await chromium.launch();
const context = await browser.newContext();
const page = await context.newPage();
page.on("console", (m) => {
  if (m.type() === "error") consoleErrors.push(m.text().slice(0, 300));
});
page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 500)));
page.on("requestfailed", (r) => {
  const err = r.failure()?.errorText ?? "";
  if (err.includes("ERR_ABORTED")) return; // canceled by our own navigation, not a server fault
  failedRequests.push(`${r.method()} ${r.url().slice(0, 140)} :: ${err}`);
});
page.on("response", (r) => {
  if (r.status() >= 500)
    failedRequests.push(`HTTP ${r.status()} ${r.request().method()} ${r.url().slice(0, 140)}`);
});

const shoot = async (tag) => {
  const p = path.join(SHOT_DIR, `marshal-smoke-${tag}.png`);
  await page.screenshot({ path: p }).catch(() => {});
  console.log(`  screenshot: ${p}`);
};

try {
  // --- 1. sign in through the hosted UI ---
  await page.goto(APP, { waitUntil: "networkidle", timeout: 45000 });
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL(/amazoncognito\.com/, { timeout: 30000 });
  await page.locator('input[name="username"]:visible, input[id="signInFormUsername"]:visible').first().fill(EMAIL);
  await page.locator('input[name="password"]:visible, input[id="signInFormPassword"]:visible').first().fill(PASSWORD);
  await page.locator('input[name="signInSubmitButton"]:visible, button[type="submit"]:visible, input[type="submit"]:visible').first().click();
  await page.waitForURL(/\/(home|onboarding|projects)/, { timeout: 45000 });
  check("sign-in", true, page.url());

  // --- 1b. notification bell renders (S5) ---
  const bell = await page
    .getByRole("button", { name: /notifications/i })
    .isVisible({ timeout: 8000 })
    .catch(() => false);
  check("notification bell", bell);

  // Un-onboarded accounts get guard-redirected to /onboarding from any page;
  // complete it whenever it appears so the rest of the probe can proceed.
  const completeOnboardingIfPresent = async () => {
    await page.waitForTimeout(1500); // allow guard redirect to settle
    if (!page.url().includes("/onboarding")) return;
    await page.getByText("Power User", { exact: false }).first().click();
    await page.getByRole("button", { name: /next/i }).click();
    await page.getByRole("button", { name: /next|skip/i }).click();
    await page.getByRole("button", { name: /start the tour/i }).click();
    await page.waitForURL(/\/home/, { timeout: 20000 });
    const closeBtn = page.locator(".driver-popover-close-btn");
    if (await closeBtn.isVisible({ timeout: 5000 }).catch(() => false)) await closeBtn.click();
    check("onboarding completed", true);
  };
  await completeOnboardingIfPresent();

  // --- 2. /projects: New Project modal opens ---
  await page.goto(`${APP}/projects`, { waitUntil: "networkidle", timeout: 45000 });
  await completeOnboardingIfPresent();
  if (!page.url().includes("/projects"))
    await page.goto(`${APP}/projects`, { waitUntil: "networkidle", timeout: 45000 });
  await page.getByRole("button", { name: /new project/i }).click({ timeout: 10000 });
  const projectModal = await page
    .locator('[role="dialog"], form')
    .filter({ hasText: /name/i })
    .first()
    .isVisible({ timeout: 5000 })
    .catch(() => false);
  check("new-project modal", projectModal);
  if (!projectModal) await shoot("projects");

  // --- 3. /chat: New Session modal opens; guided wizard renders (S4-05) ---
  await page.goto(`${APP}/chat`, { waitUntil: "networkidle", timeout: 45000 });
  await page.getByRole("button", { name: /new session/i }).first().click({ timeout: 10000 });
  const sessionModal = await page
    .getByText(/start from scratch/i)
    .isVisible({ timeout: 5000 })
    .catch(() => false);
  check("new-session modal", sessionModal);
  if (!sessionModal) await shoot("chat");
  if (sessionModal) {
    const sessionsUrl = `${APP}/api/backend/v1/chat/sessions`;
    const beforeResponse = await context.request.get(sessionsUrl).catch(() => null);
    if (!beforeResponse?.ok()) {
      throw new Error(
        `Cannot claim guided-session cleanup baseline (${beforeResponse ? `HTTP ${beforeResponse.status()}` : "no response"})`,
      );
    }
    const beforeBody = await beforeResponse.json();
    const beforeIds = new Set((beforeBody.items ?? beforeBody ?? []).map((item) => item.id));
    const guidedToggle = page.getByRole("button", { name: /guided wizard/i });
    if (await guidedToggle.isVisible({ timeout: 2000 }).catch(() => false)) {
      await guidedToggle.click();
      await page.getByRole("button", { name: /create session/i }).click();
      // Wait for the CREATE to settle (the modal unmounts) before asserting the
      // wizard: session creation also refreshes the session list, which is slow
      // when a long-lived environment has accumulated many sessions. A fixed
      // 15s assert made this flake with no product fault behind it.
      await page.getByRole("dialog").waitFor({ state: "detached", timeout: 30000 }).catch(() => {});
      const wizardVisible = await page
        .getByText(/what problem are you trying to solve/i)
        .waitFor({ state: "visible", timeout: 30000 })
        .then(() => true)
        .catch(() => false);
      const afterResponse = await context.request.get(sessionsUrl).catch(() => null);
      if (!afterResponse?.ok()) {
        throw new Error(
          `Cannot verify guided-session cleanup claim (${afterResponse ? `HTTP ${afterResponse.status()}` : "no response"})`,
        );
      }
      const afterBody = await afterResponse.json();
      const claimedSessions = (afterBody.items ?? afterBody ?? []).filter(
        (item) => !beforeIds.has(item.id),
      );
      if (claimedSessions.length !== 1) {
        throw new Error(
          `Guided-session cleanup requires exactly one new ID; observed ${claimedSessions.length}`,
        );
      }
      guidedSessionId = claimedSessions[0].id;
      check("guided wizard step 1", wizardVisible);
      if (!wizardVisible) await shoot("guided");
    }
  }

  // --- 3b. /marketplace: catalog renders; first sample detail + fork modal open ---
  await page.goto(`${APP}/marketplace`, { waitUntil: "networkidle", timeout: 45000 });
  const cardLink = page.locator('a[href^="/marketplace/"]').first();
  const hasSamples = await cardLink.isVisible({ timeout: 8000 }).catch(() => false);
  const emptyState = await page
    .getByText(/no samples match/i)
    .isVisible({ timeout: 2000 })
    .catch(() => false);
  check("marketplace catalog", hasSamples || emptyState, hasSamples ? "cards" : "empty state");
  if (hasSamples) {
    // Navigate directly (clicking mid-prefetch can force a hard-nav fallback
    // that makes timing flaky; the card link's href is the contract here).
    const href = await cardLink.getAttribute("href");
    await page.goto(`${APP}${href}`, { waitUntil: "networkidle", timeout: 45000 });
    const forkBtn = page.getByRole("button", { name: /fork to (my )?project/i }).first();
    const detailOk = await forkBtn.isVisible({ timeout: 20000 }).catch(() => false);
    check("sample detail", detailOk);
    if (detailOk) {
      await forkBtn.click();
      const modalOk = await page
        .getByText(/creates an independent project/i)
        .isVisible({ timeout: 5000 })
        .catch(() => false);
      check("fork modal", modalOk);
      if (modalOk) await page.keyboard.press("Escape").catch(() => {});
      const cancel = page.getByRole("button", { name: /cancel/i }).first();
      if (await cancel.isVisible({ timeout: 1000 }).catch(() => false)) await cancel.click();
    }
    if (!detailOk) await shoot("marketplace-detail");
  }

  // --- 3c. S7 collaboration + submissions live flow (multi-context) ---
  // owner=power, editor=admin (as a regular member), viewer=business.
  const api = (p) => `${APP}/api/backend/v1${p}`;
  const signInNewContext = async (email) => {
    const ctx = await browser.newContext();
    const p = await ctx.newPage();
    await p.goto(APP, { waitUntil: "networkidle", timeout: 45000 });
    await p.getByRole("button", { name: /sign in/i }).click();
    await p.waitForURL(/amazoncognito\.com/, { timeout: 30000 });
    await p.locator('input[name="username"]:visible, input[id="signInFormUsername"]:visible').first().fill(email);
    await p.locator('input[name="password"]:visible, input[id="signInFormPassword"]:visible').first().fill(PASSWORD);
    await p.locator('input[name="signInSubmitButton"]:visible, button[type="submit"]:visible, input[type="submit"]:visible').first().click();
    await answerTotpChallenge(p, email); // 13.5T: enrolled smoke-admin
    await p.waitForURL(/\/(home|onboarding|projects)/, { timeout: 45000 });
    return { ctx, page: p };
  };

  let s7ProjectId = null;
  let editorSession = null;
  let viewerSession = null;
  try {
    // owner: project + a spec with a heading (comment anchor target)
    let res = await context.request.post(api("/projects"), {
      data: { name: `S7 Probe ${Date.now()}`, description: "collaboration probe" },
    });
    s7ProjectId = (await res.json()).id;
    check("s7 project created", res.status() === 201, s7ProjectId);
    res = await context.request.put(api(`/projects/${s7ProjectId}/specs/requirements`), {
      data: { content: "# Requirements\n\n## Goals\nProbe the collaboration seam.\n" },
    });
    check("s7 owner spec save", res.status() === 200);

    // share: admin as editor, business as viewer
    res = await context.request.post(api(`/projects/${s7ProjectId}/members`), {
      data: { email: SMOKE_ADMIN, role: "editor" },
    });
    check("s7 add editor member", res.status() === 201);
    res = await context.request.post(api(`/projects/${s7ProjectId}/members`), {
      data: { email: "business@marshal.demo", role: "viewer" },
    });
    check("s7 add viewer member", res.status() === 201);

    // share modal renders member management (owner UI)
    await page.goto(`${APP}/projects/${s7ProjectId}`, { waitUntil: "networkidle", timeout: 45000 });
    await page.getByRole("button", { name: /share/i }).first().click({ timeout: 10000 });
    const shareModal = await page
      .getByText(/viewers can read and comment/i)
      .isVisible({ timeout: 5000 })
      .catch(() => false);
    check("s7 share modal", shareModal);
    if (shareModal) await page.keyboard.press("Escape").catch(() => {});
    const closeShare = page.getByRole("button", { name: "✕" }).first();
    if (await closeShare.isVisible({ timeout: 1000 }).catch(() => false)) await closeShare.click();

    // editor context: role visible, editor rights work, presence heartbeat fires
    editorSession = await signInNewContext(SMOKE_ADMIN);
    res = await editorSession.ctx.request.get(api("/projects"));
    const editorList = await res.json();
    const sharedEntry = (editorList.items ?? []).find((p) => p.id === s7ProjectId);
    check("s7 editor sees shared project", sharedEntry?.my_role === "editor", sharedEntry?.my_role);
    res = await editorSession.ctx.request.put(api(`/projects/${s7ProjectId}/specs/requirements`), {
      data: { content: "# Requirements\n\n## Goals\nProbe the collaboration seam (edited by editor).\n" },
    });
    check("s7 editor spec save", res.status() === 200);
    await editorSession.page.goto(`${APP}/projects/${s7ProjectId}`, { waitUntil: "networkidle", timeout: 45000 });
    const editorBadge = await editorSession.page
      .getByText(/shared · editor/i)
      .isVisible({ timeout: 8000 })
      .catch(() => false);
    check("s7 editor badge", editorBadge);
    await page.waitForTimeout(1500); // let the heartbeat land
    res = await context.request.get(api(`/projects/${s7ProjectId}/presence`));
    const presence = await res.json();
    check(
      "s7 presence shows editor",
      (presence.active ?? []).some((a) => a.email === SMOKE_ADMIN),
      JSON.stringify(presence.active?.map((a) => a.email) ?? [])
    );

    // viewer context: read-only enforced by the API, comments allowed
    viewerSession = await signInNewContext("business@marshal.demo");
    res = await viewerSession.ctx.request.put(api(`/projects/${s7ProjectId}/specs/requirements`), {
      data: { content: "# should be refused" },
    });
    check("s7 viewer write blocked", res.status() === 403, `HTTP ${res.status()}`);
    res = await viewerSession.ctx.request.post(api(`/projects/${s7ProjectId}/comments`), {
      data: { doc_type: "requirements", anchor: "goals", anchor_text: "Goals", body: "Viewer question: is teardown covered?" },
    });
    check("s7 viewer comment", res.status() === 201);
    res = await context.request.get(api(`/projects/${s7ProjectId}/comments?doc_type=requirements`));
    const threads = (await res.json()).threads ?? [];
    check("s7 comment thread visible", threads.length === 1, `${threads.length} threads`);
    if (threads[0]) {
      res = await context.request.post(api(`/comments/${threads[0].id}/resolve`));
      check("s7 comment resolve", res.status() === 200);
    }

    // submissions: owner submits → admin queue → approve → draft → delete (zero residue)
    res = await context.request.post(api(`/projects/${s7ProjectId}/submit-to-marketplace`), {
      data: { title: "S7 Probe Sample", summary: "Probe submission — will be cleaned up.", category: "custom", keywords: ["probe"] },
    });
    const submission = await res.json();
    check("s7 submit to marketplace", res.status() === 201, submission.id);
    res = await context.request.get(api("/marketplace/my-submissions"));
    const mine = await res.json();
    check("s7 my-submissions row", mine.some((s) => s.id === submission.id));
    res = await editorSession.ctx.request.get(api("/admin/marketplace/submissions"));
    const queue = await res.json();
    check("s7 admin queue shows submission", queue.some((s) => s.id === submission.id), `${queue.length} pending`);
    res = await editorSession.ctx.request.post(api(`/admin/marketplace/submissions/${submission.id}/approve`));
    check("s7 approve → draft", res.status() === 200 && (await res.json()).status === "draft");
    res = await editorSession.ctx.request.delete(api(`/admin/marketplace/samples/${submission.id}`));
    check("s7 draft sample cleanup", res.status() === 204, `HTTP ${res.status()}`);
  } catch (err) {
    check("s7 flow", false, String(err).split("\n")[0]);
    await shoot("s7");
  } finally {
    // cleanup: comments/members/presence cascade with the project
    if (s7ProjectId) {
      const res = await context.request.delete(api(`/projects/${s7ProjectId}`)).catch(() => null);
      check("s7 project cleanup", !!res && res.status() === 204, res ? `HTTP ${res.status()}` : "no response");
    }
    await editorSession?.ctx.close().catch(() => {});
    await viewerSession?.ctx.close().catch(() => {});
  }

  // --- 3c2. PDF/DOCX ingestion (pdf-docx spec AC-6): a fixture .docx imports
  // through the API the modal calls; the extracted text becomes requirements.
  {
    let docxProjectId = null;
    try {
      const body =
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' +
        "<w:body><w:p><w:r><w:t>Imported drill phrase: quartz marmoset</w:t></w:r></w:p></w:body></w:document>";
      // Minimal in-script DOCX: a stored-entry zip built by hand (no deps).
      const docx = buildStoredZip([
        ["[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'],
        ["word/document.xml", body],
      ]);
      const res = await context.request.post(api("/projects/import"), {
        data: {
          name: `Docx Import ${Date.now()}`,
          document_b64: docx.toString("base64"),
          document_name: "drill.docx",
        },
      });
      check("pdfx docx import (AC-6)", res.status() === 201, `HTTP ${res.status()}`);
      if (res.status() === 201) {
        docxProjectId = (await res.json()).id;
        const specs = await context.request.get(api(`/projects/${docxProjectId}/specs`));
        const text = JSON.stringify(await specs.json());
        check(
          "pdfx extracted text landed as requirements",
          text.includes("quartz marmoset"),
          "phrase present"
        );
        const refused = await context.request.post(api("/projects/import"), {
          data: { name: "Bad Doc", document_b64: Buffer.from([0x89, 0x50, 0x4e, 0x47, 0, 1]).toString("base64") },
        });
        const detail = ((await refused.json()) ?? {}).detail ?? "";
        check(
          "pdfx unsupported format refused by name",
          refused.status() === 422 && String(detail).includes("not a supported format"),
          `HTTP ${refused.status()}`
        );
      }
    } catch (err) {
      check("pdfx docx import (AC-6)", false, String(err).split("\n")[0]);
    } finally {
      if (docxProjectId) {
        await context.request.delete(api(`/projects/${docxProjectId}`)).catch(() => {});
      }
    }
  }

  // --- 3d. S8 codegen live flow: spec → build → validate → artifacts → bundle ---
  let s8ProjectId = null;
  let adminCtx2 = null;
  try {
    let res = await context.request.post(api("/projects"), {
      data: { name: `S8 Probe ${Date.now()}`, description: "codegen probe" },
    });
    s8ProjectId = (await res.json()).id;
    check("s8 project created", res.status() === 201, s8ProjectId);
    await context.request.put(api(`/projects/${s8ProjectId}/specs/requirements`), {
      data: {
        content:
          "# Requirements\n\n## Goal\nA tiny feedback-collector API: visitors POST a feedback " +
          "message; the team GETs the list back.\n\n## Acceptance\n- POST /feedback stores " +
          "{message}\n- GET /feedback returns stored items\n",
      },
    });
    res = await context.request.put(api(`/projects/${s8ProjectId}/specs/design`), {
      data: {
        content:
          "# Design\n\n## Architecture\nAPI Gateway REST + one Lambda (python) + DynamoDB " +
          "table. Lambda handles both routes; table name via env var TABLE_NAME.\n",
      },
    });
    check("s8 specs saved", res.status() === 200);

    res = await context.request.post(api(`/projects/${s8ProjectId}/builds`));
    const build = await res.json();
    check("s8 build started", res.status() === 202, build.id);

    // poll to terminal (real Bedrock generation — a few minutes budgeted)
    let final = null;
    for (let i = 0; i < 60; i++) {
      await page.waitForTimeout(5000);
      const poll = await context.request.get(api(`/builds/${build.id}`));
      const body = await poll.json();
      if (["ready", "failed", "cancelled"].includes(body.status)) {
        final = body;
        break;
      }
    }
    check(
      "s8 build ready",
      final?.status === "ready",
      final ? `${final.status}${final.error ? ` — ${JSON.stringify(final.error).slice(0, 200)}` : ""}` : "timeout"
    );

    if (final?.status === "ready") {
      res = await context.request.get(api(`/builds/${build.id}/artifacts`));
      const artifacts = (await res.json()).items.map((a) => a.path);
      check(
        "s8 artifacts (template+src+readme)",
        artifacts.includes("template.json") && artifacts.includes("README.md") && artifacts.some((p) => p.endsWith(".py")),
        artifacts.join(", ")
      );
      res = await context.request.get(api(`/builds/${build.id}/bundle`));
      const bundleBody = await res.body();
      check(
        "s8 handoff bundle zip",
        res.status() === 200 && bundleBody.length > 500 && bundleBody[0] === 0x50 && bundleBody[1] === 0x4b,
        `${bundleBody.length} bytes`
      );
      // Build tab renders (browser-level)
      await page.goto(`${APP}/projects/${s8ProjectId}?tab=build`, { waitUntil: "networkidle", timeout: 45000 });
      const buildTabOk = await page
        .getByText(/generate agent code/i)
        .isVisible({ timeout: 8000 })
        .catch(() => false);
      check("s8 build tab renders", buildTabOk);
      const deployBtn = await page
        .getByRole("button", { name: /deploy this build/i })
        .isVisible({ timeout: 5000 })
        .catch(() => false);
      check("s8 deploy-this-build visible", deployBtn);
    }

    // chat→build handoff (13.5Y): the spec panel must point at the Build tab —
    // tester feedback showed the authoring surfaces dead-ended at a saved spec.
    {
      res = await context.request.post(api("/chat/sessions"), {
        data: { project_id: s8ProjectId, mode: "freeform" },
      });
      const handoffSession = await res.json();
      check("s8y session on project", res.status() === 201, handoffSession.id);
      await page.goto(`${APP}/chat?session=${handoffSession.id}`, {
        waitUntil: "networkidle",
        timeout: 45000,
      });
      const handoffLink = page.locator('[data-testid="spec-start-build"]');
      const handoffVisible = await handoffLink
        .isVisible({ timeout: 10000 })
        .catch(() => false);
      check("s8y chat→build handoff link", handoffVisible);
      if (handoffVisible) {
        await handoffLink.click();
        await page.waitForURL(/tab=build/, { timeout: 20000 }).catch(() => {});
        // The tab surface differs by build state (ready heading vs start
        // button vs failed banner) — landing means EITHER renders on ?tab=build.
        const landed = await Promise.race([
          page.getByText(/generate agent code/i).waitFor({ timeout: 10000 }).then(() => true),
          page.getByRole("button", { name: /start build/i }).waitFor({ timeout: 10000 }).then(() => true),
        ]).catch(() => false);
        check(
          "s8y handoff lands on build tab",
          landed && page.url().includes("tab=build"),
          page.url()
        );
      } else {
        check("s8y handoff lands on build tab", false, "link not visible");
        await shoot("s8y-handoff");
      }

      // 13.5AB: deleting a session that GENERATED must succeed (was a live 500:
      // bare FKs from specs/spec_generations) and must leave the project's
      // spec history intact. One message → one-doc regenerate → delete.
      if (handoffSession.id) {
        res = await context.request.post(api(`/chat/sessions/${handoffSession.id}/messages`), {
          data: { content: "Add a one-line acceptance test for the health route." },
          timeout: 120000,
        });
        check("s8ab chat message on session", res.status() === 200, `HTTP ${res.status()}`);
        res = await context.request.post(api(`/chat/sessions/${handoffSession.id}/generate-spec/tasks`));
        check("s8ab single-doc regenerate accepted", res.status() === 202, `HTTP ${res.status()}`);
        let gen = null;
        for (let i = 0; i < 36 && res.status() === 202; i++) {
          await page.waitForTimeout(5000);
          const poll = await context.request.get(api(`/chat/sessions/${handoffSession.id}/generate-spec/latest`));
          gen = poll.ok() ? await poll.json() : null;
          if (gen && ["done", "failed"].includes(gen.status)) break;
        }
        check("s8ab regenerate reached terminal", !!gen && gen.status === "done", gen ? `${gen.status} ${gen.error ?? ""}` : "no status");
        res = await context.request.delete(api(`/chat/sessions/${handoffSession.id}`));
        check("s8ab delete generated session → 204", res.status() === 204, `HTTP ${res.status()}`);
        res = await context.request.get(api(`/chat/sessions/${handoffSession.id}`));
        check("s8ab deleted session is gone", res.status() === 404, `HTTP ${res.status()}`);
        res = await context.request.get(api(`/projects/${s8ProjectId}/specs`));
        const specsAfter = res.ok() ? await res.json() : {};
        check(
          "s8ab project specs survive session delete",
          !!specsAfter.tasks?.latest && !!specsAfter.requirements?.latest,
          `tasks v${specsAfter.tasks?.latest?.version ?? "?"}`
        );
      }
    }

    // codegen shows up in the admin cost pivot (R7.4)
    adminCtx2 = await signInNewContext(SMOKE_ADMIN);
    res = await adminCtx2.ctx.request.get(api("/admin/costs/breakdown?group_by=purpose"));
    const purposes = ((await res.json()).items ?? []).map((r) => r.purpose);
    check("s8 cost pivot has codegen", purposes.includes("codegen"), purposes.join(","));
  } catch (err) {
    check("s8 flow", false, String(err).split("\n")[0]);
    await shoot("s8");
  } finally {
    if (s8ProjectId) {
      const res = await context.request.delete(api(`/projects/${s8ProjectId}`)).catch(() => null);
      check("s8 project cleanup", !!res && res.status() === 204, res ? `HTTP ${res.status()}` : "no response");
    }
    await adminCtx2?.ctx.close().catch(() => {});
  }

  // --- 4. proxy hammer: sequential authed GETs to surface socket-reuse resets ---
  // Envoy reuses upstream connections; spacing some requests past small
  // keep-alive windows is what originally reproduced ECONNRESET -> 500.
  let hammerFails = 0;
  for (let i = 0; i < HAMMER_COUNT; i++) {
    const res = await context.request.get(`${APP}/api/backend/projects/`, { timeout: 15000 }).catch(() => null);
    if (!res || res.status() >= 500) hammerFails++;
    if (i % 10 === 9) await page.waitForTimeout(6000); // straddle short keep-alive windows
  }
  check(`proxy hammer (${HAMMER_COUNT} GETs)`, hammerFails === 0, hammerFails ? `${hammerFails} failures` : "0 failures");

  // --- 4b. S16-06 version surface: what is actually running, and whether the
  // code's migration head matches the database's. A mismatch here is the first
  // thing to check in an incident (docs/runbook.md), so the smoke asserts it.
  try {
    const res = await context.request.get(api("/meta/build-info"), { timeout: 15000 });
    const info = res.ok() ? await res.json() : {};
    check(
      "build-info reachable",
      res.ok() && typeof info.git_sha === "string" && info.git_sha.length > 0,
      res.ok() ? `sha=${info.git_sha} built=${info.build_time ?? "?"}` : `HTTP ${res.status()}`
    );
    check(
      "migrations in sync (code head == db head)",
      info.migrations_in_sync === true,
      `code=${info.migration_head_code ?? "?"} db=${info.migration_head_db ?? "?"}`
    );
  } catch (err) {
    check("build-info reachable", false, String(err).split("\n")[0]);
  }

  // --- 4c. S16 admin surfaces answer (analytics/chargeback) ---
  {
    let adminProbe = null;
    try {
      adminProbe = await signInNewContext(SMOKE_ADMIN);
      for (const [label, path] of [
        ["analytics summary", "/admin/analytics/summary"],
        ["analytics funnel", "/admin/analytics/funnel"],
        ["chargeback report", "/admin/costs/chargeback"],
        ["email channel status", "/admin/email/status"],
        ["audit log inspection", "/admin/audit-logs?page=1&page_size=10"],
      ]) {
        const res = await adminProbe.ctx.request.get(api(path), { timeout: 20000 }).catch(() => null);
        check(`S16 ${label}`, !!res && res.ok(), res ? `HTTP ${res.status()}` : "no response");
      }
    } catch (err) {
      check("S16 admin surfaces", false, String(err).split("\n")[0]);
    } finally {
      await adminProbe?.ctx.close().catch(() => {});
    }
  }

  // --- 4d. S15-04 in-app docs actually RENDER (not a 404 page).
  // Learned the hard way: a .dockerignore pattern stripped the docs route from
  // the image and every check still passed — the axe scan below happily scans a
  // 404 page. Assert real content, per page, from the nav registry.
  for (const [slug, expect] of [
    ["quickstart", /quickstart/i],
    ["troubleshooting", /troubleshooting/i],
    // release-notes removed from user space by owner instruction (13.5C —
    // "keep that internal"); asserted ABSENT below, not present.
    ["studio", /the studio/i], // S18 surface guide (docs expansion, 5 Aug 2026)
    // Function guides (verbose docs, 5 Aug 2026)
    ["chat-and-specs", /chat & specifications/i],
    ["codegen", /code generation/i],
    ["governance", /governance/i],
    ["deployment", /deployment & enclaves/i],
    ["marketplace", /marketplace/i],
  ]) {
    const res = await page.goto(`${APP}/docs/${slug}`, { waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => null);
    const heading = await page.locator("article h1, article h2").first().textContent({ timeout: 5000 }).catch(() => "");
    const ok = !!res && res.status() === 200 && expect.test(heading ?? "");
    check(`docs /${slug} renders`, ok, res ? `HTTP ${res.status()} heading="${(heading ?? "").trim().slice(0, 40)}"` : "no response");
  }
  {
    // Release notes stay INTERNAL (owner instruction, 13.5C) — regression-pin
    // the absence so a docs-registry change cannot quietly re-expose them.
    const res = await page.goto(`${APP}/docs/release-notes`, { waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => null);
    check("docs /release-notes stays internal", !!res && res.status() === 404, res ? `HTTP ${res.status()}` : "no response");
  }
  {
    // Nav Deployments page (17 Sep 2026 live-defect fix: the nav item had
    // pointed at /projects since S15 with no real page behind it) — pin the
    // page AND the endpoint so the regression cannot return quietly.
    const res = await page.goto(`${APP}/deployments`, { waitUntil: "networkidle", timeout: 45000 }).catch(() => null);
    const heading = await page.getByRole("heading", { name: /^deployments$/i }).isVisible().catch(() => false);
    check(
      "deployments page renders (nav target exists)",
      !!res && res.status() === 200 && heading,
      res ? `HTTP ${res.status()} heading=${heading}` : "no response"
    );
    const listRes = await context.request.get(api("/users/me/deployments"), { timeout: 15000 });
    const body = listRes.ok() ? await listRes.json() : {};
    check(
      "my-deployments endpoint answers",
      listRes.ok() && Array.isArray(body.deployments),
      `HTTP ${listRes.status()} rows=${(body.deployments ?? []).length}`
    );
  }
  {
    // Milestone docs pass (17 Sep 2026): pin one phrase per updated page so
    // the docs users see actually carry the shipped-feature content.
    for (const [slug, phrase] of [
      ["chat-and-specs", "Grounding on an existing agent"],
      ["codegen", "packaged-cfn"],
      ["governance", "capability rungs"],
      ["troubleshooting", "Dependents block teardown"],
    ]) {
      await page.goto(`${APP}/docs/${slug}`, { waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => null);
      const present = await page
        .getByText(phrase, { exact: false })
        .first()
        .isVisible()
        .catch(() => false);
      check(`docs /${slug} carries milestone content`, present, `"${phrase}"`);
    }
  }

  // --- 4d2. Admin template EDITOR renders (not just the list API).
  // Learned 4 Aug 2026: js-yaml v5 dropped its default export while stale
  // @types kept declaring one — tsc passed, the build only warned, and the
  // editor died at runtime with "Cannot read properties of undefined". Same
  // class as the docs-404 lesson: a page can crash while every check stays
  // green unless the smoke RENDERS it.
  {
    let tplAdmin = null;
    try {
      tplAdmin = await signInNewContext(SMOKE_ADMIN);
      const listRes = await tplAdmin.ctx.request.get(api("/admin/templates"), { timeout: 20000 });
      const templates = listRes.ok() ? await listRes.json() : [];
      check("template admin list", listRes.ok() && templates.length > 0, `${templates.length} templates`);
      for (const [label, slug] of [
        ["existing", templates[0]?.id],
        ["new", "new"],
      ]) {
        if (!slug) continue;
        await tplAdmin.page.goto(`${APP}/admin/templates/${slug}`, { waitUntil: "networkidle", timeout: 45000 });
        const crashed = await tplAdmin.page.getByText(/something went wrong/i).first().isVisible().catch(() => false);
        const formOk = await tplAdmin.page.getByText(/Model Guardrails/i).first().isVisible().catch(() => false);
        // The YAML preview lives behind a tab — click it, because rendering it
        // is what exercises the yamlDump call that broke (js-yaml v5 default
        // export regression); a passing default tab would not have caught it.
        await tplAdmin.page.getByRole("button", { name: /^YAML$/i }).first().click().catch(() => {});
        await tplAdmin.page.waitForTimeout(500);
        const yamlPreview = await tplAdmin.page.getByText(/template:/).first().isVisible().catch(() => false);
        const crashedAfter = await tplAdmin.page.getByText(/something went wrong/i).first().isVisible().catch(() => false);
        check(
          `template editor renders (${label})`,
          !crashed && !crashedAfter && formOk && yamlPreview,
          crashed || crashedAfter ? "error boundary shown" : !formOk ? "form missing" : yamlPreview ? "form + YAML preview" : "YAML preview missing"
        );
      }
    } catch (err) {
      check("template editor renders", false, String(err).split("\n")[0]);
    } finally {
      await tplAdmin?.ctx.close().catch(() => {});
    }
  }

  // --- 4e. S18 studio (dark launch): admin renders + axe; power 404s while off ---
  {
    let studio = null;
    let studioSessionId = null;
    try {
      studio = await signInNewContext(SMOKE_ADMIN);
      const featsRes = await studio.ctx.request.get(api("/meta/features"), { timeout: 15000 });
      const feats = featsRes.ok() ? await featsRes.json() : {};
      check("s18 features endpoint", typeof feats.studio === "boolean", JSON.stringify(feats));

      const created = await studio.ctx.request.post(api("/chat/sessions"), { data: {} });
      studioSessionId = created.status() === 201 ? (await created.json()).id : null;
      check("s18 session created", !!studioSessionId, studioSessionId ?? `HTTP ${created.status()}`);

      if (studioSessionId) {
        const pc = await studio.ctx.request.get(api(`/chat/sessions/${studioSessionId}/prompt-context`));
        check("s18 prompt-context endpoint", pc.ok(), `HTTP ${pc.status()}`);
        const ep = await studio.ctx.request.get(api(`/chat/sessions/${studioSessionId}/effective-params`));
        check("s18 effective-params endpoint", ep.ok(), `HTTP ${ep.status()}`);

        await studio.page.goto(`${APP}/studio/${studioSessionId}`, { waitUntil: "networkidle", timeout: 45000 });
        const tabVisible = await studio.page.getByRole("tab", { name: "Prompt context" }).isVisible().catch(() => false);
        check("s18 studio tabs render", tabVisible);
        if (feats.studio_dark) {
          const banner = await studio.page.getByText(/dark launch/i).first().isVisible().catch(() => false);
          check("s18 dark-launch banner (admin, flag off)", banner);
        }
        const rail = await studio.page.locator('[aria-label="Session configuration"]').isVisible().catch(() => false);
        check("s18 config rail renders", rail);

        // Keyboard drill (R5.2): arrow key moves the tablist selection
        await studio.page.getByRole("tab", { name: "Spec" }).focus();
        await studio.page.keyboard.press("ArrowRight");
        const moved = await studio.page.getByRole("tab", { name: "Prompt context" }).getAttribute("aria-selected").catch(() => null);
        check("s18 tablist arrow-key movement", moved === "true", `aria-selected=${moved}`);

        // axe on /studio from the first dark-launch deploy (R5.1)
        try {
          const { default: AxeBuilder } = await import("@axe-core/playwright");
          const results = await new AxeBuilder({ page: studio.page })
            .withTags(["wcag2a", "wcag2aa"])
            .analyze();
          const serious = results.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""));
          const detail = serious
            .flatMap((v) => v.nodes.slice(0, 3).map((n) => `${v.id} @ ${n.target.join(" ")} :: ${n.html.slice(0, 90)}`))
            .join(" || ");
          check("axe scan (/studio)", serious.length === 0, serious.length ? detail : "0 serious/critical");
        } catch (err) {
          check("axe scan (/studio)", false, `did not run: ${String(err).split("\n")[0]}`);
        }

        // R1.2: with the flag off, non-admins get the 404 page
        if (!feats.studio_enabled) {
          await page.goto(`${APP}/studio/${studioSessionId}`, { waitUntil: "networkidle", timeout: 45000 }).catch(() => {});
          const notFoundVisible = await page.getByText(/could not be found|404/i).first().isVisible().catch(() => false);
          check("s18 power user 404 while dark", notFoundVisible);
        }
      }
    } catch (err) {
      check("s18 studio flow", false, String(err).split("\n")[0]);
    } finally {
      if (studio && studioSessionId) {
        await studio.ctx.request.delete(api(`/chat/sessions/${studioSessionId}`)).catch(() => {});
      }
      await studio?.ctx.close().catch(() => {});
    }
  }

  // --- 4f. beta-usability (B14 workbench + B12 global search) ---
  {
    try {
      // Workbench aggregate: shape contract (strip content is data-dependent)
      const wb = await page.request.get(api("/users/me/workbench"), { timeout: 15000 });
      const wbData = wb.ok() ? await wb.json() : {};
      check(
        "b14 workbench endpoint",
        wb.ok() &&
          typeof wbData.reviews?.pending === "number" &&
          Array.isArray(wbData.expiring_deployments) &&
          Array.isArray(wbData.failed_builds) &&
          Array.isArray(wbData.awaiting_resubmission),
        wb.ok() ? `pending=${wbData.reviews?.pending} expiring=${wbData.expiring_deployments?.length}` : `HTTP ${wb.status()}`
      );

      // Search dialog drill: open via the header button, query, result, Escape
      await page.goto(`${APP}/home`, { waitUntil: "networkidle", timeout: 45000 });
      await page.getByTestId("global-search-button").click();
      const searchInput = page.getByRole("combobox", { name: "Search" });
      await searchInput.waitFor({ state: "visible", timeout: 10000 });
      check("b12 search dialog opens", true);
      const t0 = Date.now();
      await searchInput.fill("sentiment");
      const firstOption = page.getByRole("option").first();
      await firstOption.waitFor({ state: "visible", timeout: 15000 });
      const label = (await firstOption.textContent())?.trim() ?? "";
      check("b12 search returns grouped results", /sentiment/i.test(label), `first="${label}" in ${Date.now() - t0}ms`);
      // docs group is client-side — search a doc title too
      await searchInput.fill("governance");
      const docOption = page.getByRole("option", { name: /governance/i }).first();
      const docVisible = await docOption
        .waitFor({ state: "visible", timeout: 10000 })
        .then(() => true)
        .catch(() => false);
      check("b12 docs searched client-side", docVisible);
      await page.keyboard.press("Escape");
      const dialogGone = await page
        .getByRole("dialog", { name: "Search" })
        .isHidden({ timeout: 5000 })
        .catch(() => true);
      check("b12 escape closes search", dialogGone);
    } catch (err) {
      check("beta-usability flow", false, String(err).split("\n")[0]);
    }
  }

  // --- 4g. integration wave (B9/B10/B11): admin Integrations page renders ---
  {
    let integrations = null;
    try {
      integrations = await signInNewContext(SMOKE_ADMIN);
      await integrations.page.goto(`${APP}/admin/integrations`, { waitUntil: "networkidle", timeout: 45000 });
      const sections = await Promise.all([
        integrations.page.getByRole("heading", { name: "Service accounts" }).isVisible().catch(() => false),
        integrations.page.getByRole("heading", { name: "Outbound webhooks" }).isVisible().catch(() => false),
        integrations.page.getByRole("heading", { name: "Chat-ops channel" }).isVisible().catch(() => false),
      ]);
      check("integrations admin page renders", sections.every(Boolean), sections.join(","));
    } catch (err) {
      check("integrations admin page renders", false, String(err).split("\n")[0]);
    } finally {
      await integrations?.ctx.close().catch(() => {});
    }
  }

  // --- 5. accessibility scan (S15-06; deps live in the root package.json) ---
  try {
    const { default: AxeBuilder } = await import("@axe-core/playwright");
    // The S15-06 keyboard-drill surfaces: projects list, chat, deploy console
    // (first project page carries build/deploy tabs), and the docs home.
    for (const route of ["/projects", "/chat", "/docs"]) {
      await page.goto(`${APP}${route}`, { waitUntil: "networkidle", timeout: 45000 });
      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa"])
        .analyze();
      const serious = results.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""));
      // Report the offending NODE, not just the rule id: "color-contrast" alone
      // sent an investigation chasing a heisenbug that would not reproduce.
      const detail = serious
        .flatMap((v) =>
          v.nodes.slice(0, 3).map((n) => {
            const why = [...(n.any ?? []), ...(n.all ?? [])]
              .map((c) => c.message)
              .filter(Boolean)
              .join("; ")
              .replace(/\s+/g, " ");
            return `${v.id} @ ${n.target.join(" ")} :: ${n.html.slice(0, 90)} :: ${why.slice(0, 160)}`;
          })
        )
        .join(" || ");
      check(`axe scan (${route})`, serious.length === 0, serious.length ? detail : "0 serious/critical");
    }
  } catch (err) {
    check("axe scan", false, `did not run: ${String(err).split("\n")[0]}`);
  }
} catch (err) {
  check("probe flow", false, String(err).split("\n")[0]);
  await shoot("error");
}

if (guidedSessionId) {
  const cleanup = await context.request
    .delete(`${APP}/api/backend/v1/chat/sessions/${guidedSessionId}`)
    .catch(() => null);
  check(
    "guided session cleanup",
    !!cleanup && cleanup.status() === 204,
    cleanup ? `HTTP ${cleanup.status()}` : "no response",
  );
}

check("no page errors", pageErrors.length === 0, pageErrors[0] ?? "");
check("no HTTP 5xx / network failures", failedRequests.length === 0, failedRequests[0] ?? "");
if (consoleErrors.length) console.log(`note: ${consoleErrors.length} console error(s); first: ${consoleErrors[0]}`);

await browser.close();
const failed = checks.filter((c) => !c.ok);
console.log(failed.length ? `\nSMOKE FAILED (${failed.length})` : "\nSMOKE PASSED");
process.exit(failed.length ? 1 : 0);
