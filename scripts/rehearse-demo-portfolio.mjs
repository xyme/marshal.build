#!/usr/bin/env node
/**
 * Evidence-gated rehearsal of the canonical exact-three demo portfolio.
 *
 * This command intentionally performs live product mutations when run: it creates or
 * reuses stable marker projects, saves canonical specs, builds, deploys, drills, previews,
 * tears down, uploads private evidence, then updates catalog verification metadata.
 * It is not invoked by tests or deployment automation.
 *
 * Required environment:
 *   APP_URL
 *   DEMO_USER_PASSWORD
 *   AWS_REGION (or AWS_DEFAULT_REGION)
 *   CODEGEN_WORKSPACE_BUCKET (or DEMO_PORTFOLIO_EVIDENCE_BUCKET)
 *   ECS task credentials (AWS_CONTAINER_CREDENTIALS_RELATIVE_URI/FULL_URI) OR
 *   scoped AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (+ AWS_SESSION_TOKEN)
 *
 * Optional:
 *   PYTHON_BIN                         Python able to import backend fixtures
 *   DEMO_ADMIN_STORAGE_STATE           Playwright storage state for the MFA-authenticated admin
 *   DEMO_ADMIN_TOTP_SECRET             base32 Cognito TOTP secret (used locally; never logged)
 *   REQUIRE_ADMIN_MFA=1                require an enrolled, MFA-satisfied admin session
 *   KEEP_SENTIMENT_ACTIVE=1           only keep Sentiment active after all evidence succeeds
 *   PORTFOLIO_BUILD_ATTEMPTS=1..3     default 3
 */

import { execFileSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "..");
const APP = (process.env.APP_URL ?? "").replace(/\/$/, "");
const PASSWORD = process.env.DEMO_USER_PASSWORD;
const REGION = process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION;
const EVIDENCE_BUCKET =
  process.env.DEMO_PORTFOLIO_EVIDENCE_BUCKET ?? process.env.CODEGEN_WORKSPACE_BUCKET;
const ADMIN_STORAGE_STATE = process.env.DEMO_ADMIN_STORAGE_STATE ?? null;
const ADMIN_TOTP_SECRET = process.env.DEMO_ADMIN_TOTP_SECRET?.trim() || null;
const REQUIRE_ADMIN_MFA = parseEnvFlag("REQUIRE_ADMIN_MFA", false);
const KEEP_SENTIMENT_ACTIVE = parseEnvFlag("KEEP_SENTIMENT_ACTIVE", false);
const MAX_BUILD_ATTEMPTS = Number.parseInt(process.env.PORTFOLIO_BUILD_ATTEMPTS ?? "3", 10);
const API_PREFIX = "/api/backend/v1";
const MARKER_PREFIX = "[marshal-demo-portfolio:v1:";
const MFA_INPUT_SELECTOR = [
  'input[name="mfaCode"]:visible',
  'input[name="totpCode"]:visible',
  'input[name="softwareTokenMfaCode"]:visible',
  'input[name="code"]:visible',
  'input[name="otp"]:visible',
  'input[autocomplete="one-time-code"]:visible',
  'input[id*="mfa" i]:visible',
  'input[id*="totp" i]:visible',
].join(", ");
const SENSITIVE_VALUES = new Set(
  [PASSWORD, ADMIN_TOTP_SECRET].filter(
    (value) => typeof value === "string" && value.length > 0,
  ),
);
const ACTIVE_BUILD_STATES = new Set(["queued", "dispatched", "generating", "validating"]);
const BUILD_TERMINAL_STATES = new Set(["ready", "failed", "cancelled"]);
const ACTIVE_OR_INFLIGHT_DEPLOYMENT_STATES = new Set([
  "pending",
  "pre_flight",
  "leasing",
  "deploying",
  "updating",
  "tearing_down",
  "active",
]);
const TEARDOWN_COMPLETE_STATES = new Set(["torn_down", "superseded"]);
const DEPLOYMENT_HISTORY_LIMIT = 30;
const PROJECT_HISTORY_ROLLOVER_THRESHOLD = 24;
const CONFLICTING_BUILD_WAIT_STEPS = 12;
const ROLLOVER_CLAIM_ID = crypto.randomBytes(8).toString("hex");
const ALLOWED_VARIANCE_CHECKS = new Set([
  "inline_packaging",
  "field_contract",
  "enum_literals",
]);

if (!APP) failEnv("APP_URL is required (the installation's public URL, e.g. https://<your-domain>)");
if (!PASSWORD) failEnv("DEMO_USER_PASSWORD is required");
if (!REGION) failEnv("AWS_REGION or AWS_DEFAULT_REGION is required for private evidence S3");
if (!EVIDENCE_BUCKET) {
  failEnv(
    "CODEGEN_WORKSPACE_BUCKET or DEMO_PORTFOLIO_EVIDENCE_BUCKET is required for private evidence S3",
  );
}
if (REQUIRE_ADMIN_MFA && !ADMIN_STORAGE_STATE && !ADMIN_TOTP_SECRET) {
  failEnv(
    "REQUIRE_ADMIN_MFA=1 requires DEMO_ADMIN_STORAGE_STATE or DEMO_ADMIN_TOTP_SECRET",
  );
}
if (!Number.isInteger(MAX_BUILD_ATTEMPTS) || MAX_BUILD_ATTEMPTS < 1 || MAX_BUILD_ATTEMPTS > 3) {
  failEnv("PORTFOLIO_BUILD_ATTEMPTS must be an integer from 1 through 3");
}

function failEnv(message) {
  console.error(`REFUSED: ${message}`);
  process.exit(2);
}

function parseEnvFlag(name, defaultValue) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return defaultValue;
  if (raw === "1") return true;
  if (raw === "0") return false;
  failEnv(`${name} must be exactly 0 or 1`);
}

function sha256Bytes(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function sha256Text(value) {
  return sha256Bytes(Buffer.from(value, "utf8"));
}

function stableJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  return `{${Object.keys(value)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`)
    .join(",")}}`;
}

function fixtureDigest(fixture) {
  return sha256Text(stableJson(fixture));
}

function catalogPayloadFromFixture(fixture) {
  const metadata = fixture.metadata_extra;
  return {
    portfolio_key: fixture.portfolio_key,
    title: fixture.title,
    description: fixture.description,
    long_description: fixture.long_description,
    category: fixture.category,
    complexity: fixture.complexity,
    models_used: fixture.models_used,
    template_name: fixture.template_name,
    spec_snapshot: fixture.spec_snapshot,
    keywords: fixture.keywords,
    assets: {
      sample_data_inline: fixture.assets.sample_data_inline,
      required_evidence: fixture.assets.evidence.required,
    },
    metadata_extra: {
      portfolio_key: metadata.portfolio_key,
      portfolio_schema_version: metadata.portfolio_schema_version,
      portfolio_namespace: metadata.portfolio_namespace,
      governance: metadata.governance,
      runtime_model: metadata.runtime_model,
      tech_stack: metadata.tech_stack,
      est_build_usd: metadata.est_build_usd,
      est_run_usd_month: metadata.est_run_usd_month,
    },
  };
}

function catalogPayloadFromRow(row) {
  const metadata = row.metadata_extra ?? {};
  return {
    portfolio_key: metadata.portfolio_key,
    title: row.title,
    description: row.description,
    long_description: row.long_description,
    category: row.category,
    complexity: row.complexity,
    models_used: row.models_used ?? [],
    template_name: row.template_id === null ? null : row.template_id,
    spec_snapshot: row.spec_snapshot ?? {},
    keywords: row.keywords ?? [],
    assets: {
      sample_data_inline: row.assets?.sample_data_inline,
      required_evidence: row.assets?.evidence?.required,
    },
    metadata_extra: {
      portfolio_key: metadata.portfolio_key,
      portfolio_schema_version: metadata.portfolio_schema_version,
      portfolio_namespace: metadata.portfolio_namespace,
      governance: metadata.governance,
      runtime_model: metadata.runtime_model,
      tech_stack: metadata.tech_stack,
      est_build_usd: metadata.est_build_usd,
      est_run_usd_month: metadata.est_run_usd_month,
    },
  };
}

function catalogPayloadDigest(fixture) {
  return sha256Text(stableJson(catalogPayloadFromFixture(fixture)));
}

function specHash(snapshot) {
  return sha256Text(
    `requirements:${snapshot.requirements_md}\n` +
      `design:${snapshot.design_md}\n` +
      `tasks:${snapshot.tasks_md}`,
  );
}

function markerFor(key) {
  return `${MARKER_PREFIX}${key}]`;
}

function projectName(fixture) {
  return `[Showcase] ${fixture.title}`;
}

function retiredMarkerFor(fixture, project) {
  return (
    `[marshal-demo-portfolio-retired:v1:${fixture.portfolio_key}:` +
    `${project.id}:${ROLLOVER_CLAIM_ID}]`
  );
}

function retiredProjectName(fixture, project) {
  return `[Retired ${String(project.id).slice(0, 8)}] ${fixture.title}`.slice(0, 120);
}

function api(route) {
  return `${APP}${API_PREFIX}${route}`;
}

function nowIso() {
  return new Date().toISOString();
}

function safeError(value) {
  const secretKeys = /key|password|secret|token|authorization|credential/i;
  if (Array.isArray(value)) return value.map(safeError);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, secretKeys.test(key) ? "[REDACTED]" : safeError(item)]),
    );
  }
  let text = String(value ?? "");
  for (const sensitive of SENSITIVE_VALUES) {
    if (sensitive) text = text.split(sensitive).join("[REDACTED]");
  }
  return text
    .replace(/mat_[A-Za-z0-9_-]+/g, "[REDACTED]")
    .replace(/([?&](?:key|token|password)=)[^&\s]+/gi, "$1[REDACTED]");
}

function assert(condition, message, detail = undefined) {
  if (!condition) {
    const suffix = detail === undefined ? "" : ` — ${JSON.stringify(safeError(detail)).slice(0, 600)}`;
    throw new Error(`${message}${suffix}`);
  }
  console.log(`PASS  ${message}`);
}

function loadCanonicalFixtures() {
  const code = [
    "import json, os, sys",
    `sys.path.insert(0, ${JSON.stringify(path.join(REPO_ROOT, "backend"))})`,
    "sys.path.insert(0, '/app')",
    "from fixtures.marketplace.portfolio import PORTFOLIO_SNAPSHOTS, portfolio_sha256",
    "print(json.dumps({'fixtures': PORTFOLIO_SNAPSHOTS, 'portfolio_sha256': portfolio_sha256()}, ensure_ascii=False))",
  ].join("; ");
  const candidates = [
    process.env.PYTHON_BIN,
    path.join(REPO_ROOT, "backend", ".venv", "bin", "python"),
    "python3",
    "python",
  ].filter(Boolean);
  const failures = [];
  const fixtureEnv = { ...process.env, PYTHONPATH: path.join(REPO_ROOT, "backend") };
  delete fixtureEnv.DEMO_ADMIN_TOTP_SECRET;
  delete fixtureEnv.DEMO_ADMIN_STORAGE_STATE;
  for (const candidate of candidates) {
    try {
      const output = execFileSync(candidate, ["-c", code], {
        cwd: REPO_ROOT,
        encoding: "utf8",
        env: fixtureEnv,
        stdio: ["ignore", "pipe", "pipe"],
      });
      const parsed = JSON.parse(output);
      assert(parsed.fixtures?.length === 3, "canonical fixture module contains exactly three snapshots");
      const localDigest = sha256Text(stableJson(parsed.fixtures));
      assert(localDigest === parsed.portfolio_sha256, "fixture portfolio SHA-256 matches Python source");
      return parsed;
    } catch (error) {
      failures.push(`${candidate}: ${safeError(error?.message ?? error)}`);
    }
  }
  throw new Error(
    "Cannot import canonical backend fixtures. Set PYTHON_BIN to the backend Python interpreter. " +
      failures.join(" | "),
  );
}

async function requestJson(actor, method, route, options = {}) {
  const requestOptions = {};
  if (options.data !== undefined) requestOptions.data = options.data;
  if (options.headers !== undefined) requestOptions.headers = options.headers;
  if (options.timeout !== undefined) requestOptions.timeout = options.timeout;
  const response = await actor.ctx.request[method](api(route), requestOptions);
  const expected = options.expected ?? [200];
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!expected.includes(response.status())) {
    throw new Error(
      `${method.toUpperCase()} ${route} returned HTTP ${response.status()}: ` +
        JSON.stringify(safeError(body)).slice(0, 800),
    );
  }
  return { status: response.status(), body, response };
}

function decodeBase32(secret) {
  const normalized = secret.toUpperCase().replace(/[\s-]+/g, "").replace(/=+$/g, "");
  if (!normalized || !/^[A-Z2-7]+$/.test(normalized)) {
    throw new Error("DEMO_ADMIN_TOTP_SECRET is not valid base32");
  }
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const character of normalized) {
    bits += alphabet.indexOf(character).toString(2).padStart(5, "0");
  }
  const bytes = [];
  for (let offset = 0; offset + 8 <= bits.length; offset += 8) {
    bytes.push(Number.parseInt(bits.slice(offset, offset + 8), 2));
  }
  if (!bytes.length) throw new Error("DEMO_ADMIN_TOTP_SECRET decoded to no bytes");
  return Buffer.from(bytes);
}

async function currentTotp(secret) {
  const seconds = Math.floor(Date.now() / 1000);
  const remaining = 30 - (seconds % 30);
  if (remaining <= 3) {
    await new Promise((resolve) => setTimeout(resolve, (remaining + 1) * 1000));
  }
  const counter = BigInt(Math.floor(Date.now() / 1000 / 30));
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(counter);
  const digest = crypto.createHmac("sha1", decodeBase32(secret)).update(message).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const binary =
    ((digest[offset] & 0x7f) << 24) |
    ((digest[offset + 1] & 0xff) << 16) |
    ((digest[offset + 2] & 0xff) << 8) |
    (digest[offset + 3] & 0xff);
  const code = String(binary % 1_000_000).padStart(6, "0");
  SENSITIVE_VALUES.add(code);
  return code;
}

async function actorFromContext(ctx, email) {
  const page = await ctx.newPage();
  await page.goto(APP, { waitUntil: "networkidle", timeout: 60_000 });
  const actor = { email, ctx, page };
  const me = (await requestJson(actor, "get", "/users/me")).body;
  assert(me.email === email, `${email}: authenticated session belongs to the expected identity`);
  actor.me = me;
  return actor;
}

async function signIn(browser, email, { storageState = null, totpSecret = null } = {}) {
  const contextOptions = { viewport: { width: 1600, height: 1000 } };
  if (storageState) {
    const storagePath = path.resolve(process.cwd(), storageState);
    if (!fs.existsSync(storagePath)) {
      throw new Error("DEMO_ADMIN_STORAGE_STATE does not exist");
    }
    contextOptions.storageState = storagePath;
    const ctx = await browser.newContext(contextOptions);
    return actorFromContext(ctx, email);
  }

  const ctx = await browser.newContext(contextOptions);
  const page = await ctx.newPage();
  await page.goto(APP, { waitUntil: "networkidle", timeout: 60_000 });
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL(/amazoncognito\.com/, { timeout: 30_000 });
  await page
    .locator('input[name="username"]:visible, input[id="signInFormUsername"]:visible')
    .first()
    .fill(email);
  await page
    .locator('input[name="password"]:visible, input[id="signInFormPassword"]:visible')
    .first()
    .fill(PASSWORD);
  await page
    .locator(
      'input[name="signInSubmitButton"]:visible, button[type="submit"]:visible, input[type="submit"]:visible',
    )
    .first()
    .click();

  const outcome = await Promise.race([
    page.waitForURL(/\/(home|onboarding|projects)/, { timeout: 60_000 }).then(() => "app"),
    page.locator(MFA_INPUT_SELECTOR).first().waitFor({ state: "visible", timeout: 60_000 }).then(() => "mfa"),
  ]);
  if (outcome === "mfa") {
    if (email !== "admin@marshal.demo") {
      throw new Error(`Unexpected Cognito MFA challenge for ${email}`);
    }
    if (!totpSecret) {
      throw new Error(
        "Cognito requested an admin MFA code; provide DEMO_ADMIN_TOTP_SECRET or DEMO_ADMIN_STORAGE_STATE",
      );
    }
    const code = await currentTotp(totpSecret);
    await page.locator(MFA_INPUT_SELECTOR).first().fill(code);
    await page
      .locator('button[type="submit"]:visible, input[type="submit"]:visible')
      .first()
      .click();
    await page.waitForURL(/\/(home|onboarding|projects)/, { timeout: 60_000 });
  }
  const actor = { email, ctx, page };
  const me = (await requestJson(actor, "get", "/users/me")).body;
  assert(me.email === email, `${email}: authenticated session belongs to the expected identity`);
  actor.me = me;
  return actor;
}

async function ensurePersonas(browser) {
  const [power, admin, business] = await Promise.all([
    signIn(browser, "power@marshal.demo"),
    signIn(browser, "admin@marshal.demo", {
      storageState: ADMIN_STORAGE_STATE,
      totpSecret: ADMIN_TOTP_SECRET,
    }),
    signIn(browser, "business@marshal.demo"),
  ]);
  assert(power.me.role === "power" && power.me.persona === "power", "power persona authenticated");
  assert(admin.me.role === "admin", "admin role authenticated");
  assert(business.me.role === "business", "business role authenticated");
  return { power, admin, business };
}

async function assertAdminMfa(admin) {
  const status = (await requestJson(admin, "get", "/users/me/mfa")).body;
  if (status.required_for_me) {
    assert(status.session_mfa === true, "admin policy-required session used MFA");
  }
  if (REQUIRE_ADMIN_MFA) {
    assert(status.session_mfa === true, "REQUIRE_ADMIN_MFA session_mfa assertion passed");
    assert(status.enrolled === true, "REQUIRE_ADMIN_MFA admin is enrolled in software-token MFA");
  }
  return {
    enrolled: Boolean(status.enrolled),
    session_mfa: Boolean(status.session_mfa),
    required_for_admins: Boolean(status.required_for_admins),
    required_for_me: Boolean(status.required_for_me),
  };
}

async function listVisibleProjects(power, status = null) {
  const items = [];
  for (let page = 1; ; page += 1) {
    const query = new URLSearchParams({ page: String(page), page_size: "100" });
    if (status) query.set("status", status);
    const body = (await requestJson(power, "get", `/projects?${query.toString()}`)).body;
    items.push(...(body.items ?? []));
    if (items.length >= Number(body.total ?? items.length)) break;
  }
  return items;
}

async function projectCandidates(power, fixture) {
  const marker = markerFor(fixture.portfolio_key);
  const visible = await listVisibleProjects(power);
  const archived = await listVisibleProjects(power, "archived");
  const byId = new Map([...visible, ...archived].map((item) => [item.id, item]));
  return [...byId.values()].filter(
    (item) => item.name === projectName(fixture) || String(item.description ?? "").includes(marker),
  );
}

function hasExactProjectMarker(project, marker) {
  const markers =
    String(project.description ?? "").match(
      /\[marshal-demo-portfolio:v1:[a-z0-9-]+\]/g,
    ) ?? [];
  return markers.length === 1 && markers[0] === marker;
}

async function recoverProjectMutation(power, projectId, mutate, applied, label) {
  try {
    return await mutate();
  } catch (mutationError) {
    let observed;
    try {
      observed = (await requestJson(power, "get", `/projects/${projectId}`)).body;
    } catch (verificationError) {
      throw new AggregateError(
        [mutationError, verificationError],
        `${label}: mutation response was lost and server state could not be verified`,
      );
    }
    if (!applied(observed)) throw mutationError;
    console.warn(`WARN  ${label}: response was lost; fresh project state proves the mutation committed`);
    return observed;
  }
}

async function rolloverStableProjectIfNeeded(power, project, fixture, projectMutations) {
  const history = await deploymentHistory(power, project.id);
  if (history.length >= DEPLOYMENT_HISTORY_LIMIT) {
    throw new Error(
      `${fixture.title}: deployment history reached the ${DEPLOYMENT_HISTORY_LIMIT}-row API cap; ` +
        "safe rollover cannot prove exhaustive state",
    );
  }
  if (history.length < PROJECT_HISTORY_ROLLOVER_THRESHOLD) return project;

  const activeDeployments = history.filter((row) =>
    ACTIVE_OR_INFLIGHT_DEPLOYMENT_STATES.has(row.status),
  );
  assert(
    activeDeployments.length === 0,
    `${fixture.title}: rollover has zero active/inflight deployment rows`,
    activeDeployments.map((row) => ({ id: row.id, status: row.status })),
  );
  const activeBuilds = (await listBuilds(power, project.id)).filter((build) =>
    ACTIVE_BUILD_STATES.has(build.status),
  );
  assert(
    activeBuilds.length === 0 && !["building", "deployed"].includes(project.status),
    `${fixture.title}: rollover has zero active/inflight project work`,
    {
      project_status: project.status,
      builds: activeBuilds.map((build) => ({ id: build.id, status: build.status })),
    },
  );

  const marker = markerFor(fixture.portfolio_key);
  const retiredMarker = retiredMarkerFor(fixture, project);
  const priorDescription = String(project.description ?? "").replace(marker, "").trim();
  const retiredDescription = `${retiredMarker} ${priorDescription}`.trim().slice(0, 2000);
  const retiredName = retiredProjectName(fixture, project);
  const retirement = {
    fixture,
    project: { ...project },
    retiredName,
    retiredDescription,
  };
  projectMutations.retired.push(retirement);
  let retired = await recoverProjectMutation(
    power,
    project.id,
    () =>
      requestJson(power, "put", `/projects/${project.id}`, {
        data: { name: retiredName, description: retiredDescription },
      }).then((response) => response.body),
    (observed) =>
      observed.name === retiredName && observed.description === retiredDescription,
    `${fixture.title}: retire project name/marker`,
  );
  assert(
    retired.name === retiredName &&
      retired.description === retiredDescription &&
      !hasExactProjectMarker(retired, marker),
    `${fixture.title}: rollover retired the old showcase name and marker`,
  );
  retired = await recoverProjectMutation(
    power,
    project.id,
    () =>
      requestJson(power, "post", `/projects/${project.id}/archive`).then(
        (response) => response.body,
      ),
    (observed) => observed.status === "archived",
    `${fixture.title}: archive retired project`,
  );
  assert(retired.status === "archived", `${fixture.title}: rollover archived the retired project`);
  const collisions = await projectCandidates(power, fixture);
  assert(
    collisions.length === 0,
    `${fixture.title}: retired project no longer owns the stable showcase identity`,
    collisions.map((item) => item.id),
  );
  const postArchiveHistory = await deploymentHistory(power, project.id);
  requireExhaustiveDeploymentHistory(postArchiveHistory, `${fixture.title} post-archive rollover`);
  const postArchiveDeployments = postArchiveHistory.filter((row) =>
    ACTIVE_OR_INFLIGHT_DEPLOYMENT_STATES.has(row.status),
  );
  const postArchiveBuilds = (await listBuilds(power, project.id)).filter((build) =>
    ACTIVE_BUILD_STATES.has(build.status),
  );
  const postArchiveProject = (
    await requestJson(power, "get", `/projects/${project.id}`)
  ).body;
  assert(
    postArchiveProject.status === "archived" &&
      postArchiveProject.name === retiredName &&
      postArchiveProject.description === retiredDescription &&
      postArchiveDeployments.length === 0 &&
      postArchiveBuilds.length === 0,
    `${fixture.title}: retired project remains archived and quiescent before replacement`,
    {
      project_status: postArchiveProject.status,
      deployments: postArchiveDeployments.map((row) => ({ id: row.id, status: row.status })),
      builds: postArchiveBuilds.map((build) => ({ id: build.id, status: build.status })),
    },
  );
  console.log(
    `PASS  ${fixture.title}: safely rolled over ${history.length} deployment attempts from ${project.id}`,
  );
  return null;
}

async function ensureStableProject(power, fixture, projectMutations) {
  const marker = markerFor(fixture.portfolio_key);
  let candidates = await projectCandidates(power, fixture);
  let exact = candidates.filter(
    (item) =>
      item.status !== "archived" &&
      item.name === projectName(fixture) &&
      hasExactProjectMarker(item, marker),
  );
  const ambiguous = candidates.filter((item) => !exact.some((match) => match.id === item.id));
  if (exact.length > 1 || ambiguous.length > 0) {
    throw new Error(
      `${fixture.title}: project name/marker reconciliation is ambiguous; ` +
        `exact=${exact.map((item) => item.id).join(",") || "none"} ` +
        `collisions=${ambiguous.map((item) => item.id).join(",") || "none"}`,
    );
  }
  let project = exact[0] ?? null;
  if (project) {
    project = (await requestJson(power, "get", `/projects/${project.id}`)).body;
    assert(project.my_role === "owner", `${fixture.title}: stable project is owned by power`);
    assert(project.name === projectName(fixture), `${fixture.title}: showcase name is exact`);
    assert(
      hasExactProjectMarker(project, marker),
      `${fixture.title}: stable description marker is exact`,
    );
    project = await rolloverStableProjectIfNeeded(
      power,
      project,
      fixture,
      projectMutations,
    );
  }
  let createdRecord = null;
  if (!project) {
    const creationClaim = `creation claim=${ROLLOVER_CLAIM_ID}`;
    const description =
      `${marker} API-created rehearsal project; canonical fixture sha256=${fixtureDigest(fixture)}; ` +
      creationClaim;
    try {
      project = (
        await requestJson(power, "post", "/projects", {
          expected: [201],
          data: { name: projectName(fixture), description },
        })
      ).body;
    } catch (creationError) {
      let claimed;
      try {
        candidates = await projectCandidates(power, fixture);
        claimed = candidates.filter((item) =>
          String(item.description ?? "").includes(creationClaim),
        );
      } catch (discoveryError) {
        throw new AggregateError(
          [creationError, discoveryError],
          `${fixture.title}: project-create response was lost and ownership could not be reconciled`,
        );
      }
      if (claimed.length !== 1) throw creationError;
      project = claimed[0];
      console.warn(
        `WARN  ${fixture.title}: project-create response was lost; unique claim proves ownership`,
      );
    }
    createdRecord = { fixture, project: { ...project }, stable: false };
    projectMutations.created.push(createdRecord);
    candidates = await projectCandidates(power, fixture);
    exact = candidates.filter(
      (item) =>
        item.status !== "archived" &&
        item.name === projectName(fixture) &&
        hasExactProjectMarker(item, marker),
    );
    if (exact.length !== 1 || candidates.length !== 1 || exact[0].id !== project.id) {
      throw new Error(
        `${fixture.title}: project creation did not converge to one exact name+marker pair`,
      );
    }
  }
  const detail = (await requestJson(power, "get", `/projects/${project.id}`)).body;
  assert(detail.my_role === "owner", `${fixture.title}: stable project is owned by power`);
  assert(detail.name === projectName(fixture), `${fixture.title}: showcase name is exact`);
  assert(
    hasExactProjectMarker(detail, marker),
    `${fixture.title}: stable description marker is exact`,
  );
  if (createdRecord) {
    createdRecord.project = { ...detail };
    createdRecord.stable = true;
  }
  return detail;
}

async function cancelActiveBuildsForCleanup(power, project, label) {
  for (let round = 0; round < 3; round += 1) {
    const active = (await listBuilds(power, project.id)).filter((build) =>
      ACTIVE_BUILD_STATES.has(build.status),
    );
    if (active.length === 0) return;
    for (const build of active) {
      await requestJson(power, "post", `/builds/${build.id}/cancel`, {
        expected: [200, 409],
      });
      const terminal = await pollBuild(power, build.id);
      assert(
        BUILD_TERMINAL_STATES.has(terminal.status),
        `${label}: active build ${build.id} became terminal during cleanup`,
      );
    }
  }
  const remaining = (await listBuilds(power, project.id)).filter((build) =>
    ACTIVE_BUILD_STATES.has(build.status),
  );
  assert(
    remaining.length === 0,
    `${label}: cleanup left zero active builds`,
    remaining.map((build) => ({ id: build.id, status: build.status })),
  );
}

async function verifyRetiredProjectsQuiescent(power, retirements) {
  for (const retirement of retirements) {
    const { fixture, project, retiredName, retiredDescription } = retirement;
    const detail = (await requestJson(power, "get", `/projects/${project.id}`)).body;
    const history = await deploymentHistory(power, project.id);
    requireExhaustiveDeploymentHistory(history, `${fixture.title} retired project`);
    const activeDeployments = history.filter((row) =>
      ACTIVE_OR_INFLIGHT_DEPLOYMENT_STATES.has(row.status),
    );
    const activeBuilds = (await listBuilds(power, project.id)).filter((build) =>
      ACTIVE_BUILD_STATES.has(build.status),
    );
    assert(
      detail.status === "archived" &&
        detail.name === retiredName &&
        detail.description === retiredDescription &&
        activeDeployments.length === 0 &&
        activeBuilds.length === 0,
      `${fixture.title}: claimed retired project is archived and quiescent`,
      {
        project_status: detail.status,
        deployments: activeDeployments.map((row) => ({ id: row.id, status: row.status })),
        builds: activeBuilds.map((build) => ({ id: build.id, status: build.status })),
      },
    );
  }
}

async function convergeRetirementForCleanup(power, retirement) {
  const { fixture, project, retiredName, retiredDescription } = retirement;
  const current = (await requestJson(power, "get", `/projects/${project.id}`)).body;
  const stillOriginal =
    current.name === project.name && current.description === project.description;
  const alreadyClaimed =
    current.name === retiredName && current.description === retiredDescription;
  if (!stillOriginal && !alreadyClaimed) {
    throw new Error(
      `${fixture.title}: retired project ${project.id} is owned by another rollover claim`,
    );
  }
  let retired = current;
  if (!alreadyClaimed) {
    retired = await recoverProjectMutation(
      power,
      project.id,
      () =>
        requestJson(power, "put", `/projects/${project.id}`, {
          data: { name: retiredName, description: retiredDescription },
        }).then((response) => response.body),
      (observed) =>
        observed.name === retiredName && observed.description === retiredDescription,
      `${fixture.title}: converge retired identity during cleanup`,
    );
  }
  if (retired.status !== "archived") {
    retired = await recoverProjectMutation(
      power,
      project.id,
      () =>
        requestJson(power, "post", `/projects/${project.id}/archive`).then(
          (response) => response.body,
        ),
      (observed) => observed.status === "archived",
      `${fixture.title}: converge retired archive during cleanup`,
    );
  }
  assert(
    retired.status === "archived",
    `${fixture.title}: retired project cleanup converged to archived`,
  );
}

async function retireUncommittedCreatedProject(power, record) {
  if (record.stable) return;
  const { fixture, project } = record;
  const detail = (await requestJson(power, "get", `/projects/${project.id}`)).body;
  const creationClaim = `creation claim=${ROLLOVER_CLAIM_ID}`;
  if (!String(detail.description ?? "").includes(creationClaim)) {
    throw new Error(
      `${fixture.title}: cannot retire uncommitted project ${project.id}; creation claim changed`,
    );
  }
  const abandonedMarker =
    `[marshal-demo-portfolio-abandoned:v1:${fixture.portfolio_key}:` +
    `${project.id}:${ROLLOVER_CLAIM_ID}]`;
  const abandonedName = `[Abandoned ${String(project.id).slice(0, 8)}] ${fixture.title}`.slice(
    0,
    120,
  );
  const abandonedDescription =
    `${abandonedMarker} Uncommitted rehearsal replacement retired after reconciliation failure.`;
  let abandoned = await recoverProjectMutation(
    power,
    project.id,
    () =>
      requestJson(power, "put", `/projects/${project.id}`, {
        data: { name: abandonedName, description: abandonedDescription },
      }).then((response) => response.body),
    (observed) =>
      observed.name === abandonedName && observed.description === abandonedDescription,
    `${fixture.title}: abandon uncommitted replacement`,
  );
  abandoned = await recoverProjectMutation(
    power,
    project.id,
    () =>
      requestJson(power, "post", `/projects/${project.id}/archive`).then(
        (response) => response.body,
      ),
    (observed) => observed.status === "archived",
    `${fixture.title}: archive uncommitted replacement`,
  );
  assert(
    abandoned.status === "archived",
    `${fixture.title}: uncommitted replacement was archived`,
  );
}

async function ensureBusinessViewer(power, business, project, fixture) {
  const membership = (await requestJson(power, "get", `/projects/${project.id}/members`)).body;
  const existing = (membership.members ?? []).find(
    (item) => item.email === "business@marshal.demo" || item.user_id === business.me.id,
  );
  if (!existing) {
    await requestJson(power, "post", `/projects/${project.id}/members`, {
      expected: [201],
      data: { email: "business@marshal.demo", role: "viewer" },
    });
  } else if (existing.role !== "viewer") {
    await requestJson(power, "patch", `/projects/${project.id}/members/${business.me.id}`, {
      data: { role: "viewer" },
    });
  }
  const viewed = (await requestJson(business, "get", `/projects/${project.id}`)).body;
  assert(viewed.my_role === "viewer", `${fixture.title}: business persona has viewer-only access`);
}

function latestContent(specResponse, doc) {
  return specResponse?.[doc]?.latest?.content ?? null;
}

async function ensureCanonicalSpecs(power, project, fixture) {
  let specs = (await requestJson(power, "get", `/projects/${project.id}/specs`)).body;
  for (const [doc, field] of [
    ["requirements", "requirements_md"],
    ["design", "design_md"],
    ["tasks", "tasks_md"],
  ]) {
    const expected = fixture.spec_snapshot[field];
    if (latestContent(specs, doc) !== expected) {
      await requestJson(power, "put", `/projects/${project.id}/specs/${doc}`, {
        data: { content: expected },
      });
    }
  }
  specs = (await requestJson(power, "get", `/projects/${project.id}/specs`)).body;
  const observed = {
    requirements_md: latestContent(specs, "requirements"),
    design_md: latestContent(specs, "design"),
    tasks_md: latestContent(specs, "tasks"),
  };
  assert(
    stableJson(observed) === stableJson(fixture.spec_snapshot),
    `${fixture.title}: server specs exactly match canonical snapshot`,
  );
  const expectedHash = specHash(fixture.spec_snapshot);
  assert(/^[0-9a-f]{64}$/.test(expectedHash), `${fixture.title}: exact spec hash computed`);
  return expectedHash;
}

function deterministicVerdicts(build) {
  return (build?.manifest?.conformance?.verdicts ?? []).filter(
    (verdict) => verdict.source === "deterministic",
  );
}

function acceptedBuild(build, expectedHash) {
  const verdicts = deterministicVerdicts(build);
  return (
    build?.status === "ready" &&
    build?.provider === "internal" &&
    build?.artifact_profile === "inline-cfn" &&
    build?.spec_hash === expectedHash &&
    build?.manifest?.conformance?.status === "ok" &&
    verdicts.length > 0 &&
    verdicts.every((verdict) => verdict.verdict === "met")
  );
}

function retryableBuild(build) {
  if (
    build?.error?.code === "crashed" &&
    [
      "Plan generation hit the token cap — retry the build",
      "template.json assembly hit the token cap",
    ].includes(build?.error?.message)
  ) {
    return true;
  }
  const findings = build?.error?.findings ?? [];
  if (!findings.length) return false;
  return findings.every((finding) => ALLOWED_VARIANCE_CHECKS.has(finding.check));
}

async function listBuilds(power, projectId) {
  const payload = (await requestJson(power, "get", `/projects/${projectId}/builds`)).body;
  return payload.items ?? payload ?? [];
}

async function pollBuild(power, buildId, timeoutSteps = 100) {
  for (let attempt = 0; attempt < timeoutSteps; attempt += 1) {
    const build = (await requestJson(power, "get", `/builds/${buildId}`)).body;
    if (BUILD_TERMINAL_STATES.has(build.status)) return build;
    await power.page.waitForTimeout(5_000);
  }
  throw new Error(
    `Build ${buildId} did not reach a terminal state within ${timeoutSteps * 5} seconds`,
  );
}

async function waitBoundedlyForBuild(power, buildId, timeoutSteps) {
  let build = null;
  for (let attempt = 0; attempt < timeoutSteps; attempt += 1) {
    build = (await requestJson(power, "get", `/builds/${buildId}`)).body;
    if (BUILD_TERMINAL_STATES.has(build.status)) return build;
    await power.page.waitForTimeout(5_000);
  }
  return build;
}

async function reconcileActiveBuild(power, project, expectedHash, label) {
  const active = (await listBuilds(power, project.id)).filter((candidate) =>
    ACTIVE_BUILD_STATES.has(candidate.status),
  );
  if (active.length > 1) {
    throw new Error(
      `${label}: multiple active builds prevent safe reconciliation: ` +
        active.map((candidate) => `${candidate.id}:${candidate.status}`).join(","),
    );
  }
  if (active.length === 0) return null;

  let current = (await requestJson(power, "get", `/builds/${active[0].id}`)).body;
  if (BUILD_TERMINAL_STATES.has(current.status)) {
    return current.spec_hash === expectedHash ? current : null;
  }
  assert(
    ACTIVE_BUILD_STATES.has(current.status),
    `${label}: active build state is recognized before reconciliation`,
    { id: current.id, status: current.status },
  );
  if (current.spec_hash === expectedHash) {
    console.log(`INFO  ${label}: polling exact-hash active build ${current.id}`);
    return pollBuild(power, current.id);
  }

  console.log(
    `INFO  ${label}: bounded wait for conflicting active build ${current.id} (${current.status})`,
  );
  current = await waitBoundedlyForBuild(
    power,
    current.id,
    CONFLICTING_BUILD_WAIT_STEPS,
  );
  if (current && ACTIVE_BUILD_STATES.has(current.status)) {
    const cancellation = await requestJson(power, "post", `/builds/${current.id}/cancel`, {
      expected: [200, 409],
    });
    if (cancellation.status === 200) {
      assert(
        cancellation.body?.id === current.id && cancellation.body?.cancel_requested === true,
        `${label}: conflicting build cancellation was accepted by the product API`,
      );
    }
    current = await pollBuild(power, current.id);
    assert(
      BUILD_TERMINAL_STATES.has(current.status),
      `${label}: conflicting build became terminal before replacement`,
      { id: current.id, status: current.status },
    );
  }
  const remaining = (await listBuilds(power, project.id)).filter((candidate) =>
    ACTIVE_BUILD_STATES.has(candidate.status),
  );
  assert(
    remaining.length === 0,
    `${label}: no active build remains before starting another`,
    remaining.map((candidate) => ({ id: candidate.id, status: candidate.status })),
  );
  return null;
}

async function ensureBuild(power, project, expectedHash, label) {
  const resumed = await reconcileActiveBuild(power, project, expectedHash, label);
  if (resumed && acceptedBuild(resumed, expectedHash)) {
    console.log(`PASS  ${label}: resumed exact-hash accepted build ${resumed.id}`);
    return resumed;
  }

  const existing = await listBuilds(power, project.id);
  for (const candidate of existing) {
    if (candidate.spec_hash !== expectedHash || candidate.status !== "ready") continue;
    const full = (await requestJson(power, "get", `/builds/${candidate.id}`)).body;
    if (acceptedBuild(full, expectedHash)) {
      console.log(`PASS  ${label}: reused exact-hash accepted build ${candidate.id}`);
      return full;
    }
  }

  let last = resumed;
  for (let attempt = 1; attempt <= MAX_BUILD_ATTEMPTS; attempt += 1) {
    const started = (
      await requestJson(power, "post", `/projects/${project.id}/builds`, {
        expected: [202],
        data: { artifact_profile: "inline-cfn" },
      })
    ).body;
    last = await pollBuild(power, started.id);
    assert(last.spec_hash === expectedHash, `${label}: build ${attempt} froze the exact spec hash`);
    if (acceptedBuild(last, expectedHash)) {
      console.log(`PASS  ${label}: B23 deterministic verdicts accepted on attempt ${attempt}`);
      return last;
    }
    if (attempt === MAX_BUILD_ATTEMPTS || !retryableBuild(last)) {
      break;
    }
    console.log(
      `INFO  ${label}: retrying only allowed generation variance (${(last.error?.findings ?? [])
        .map((finding) => finding.check)
        .join(",")})`,
    );
  }
  throw new Error(
    `${label}: no accepted build after ${MAX_BUILD_ATTEMPTS} bounded attempts: ` +
      JSON.stringify(safeError(last?.error ?? last?.status)),
  );
}

function b23Vector(build) {
  return deterministicVerdicts(build).map((verdict) => ({
    check: verdict.check,
    verdict: verdict.verdict,
    criterion_sha256: sha256Text(verdict.criterion ?? ""),
    evidence_sha256: sha256Text(verdict.evidence ?? ""),
  }));
}

async function activeReviewRows(power, projectIds) {
  const ids = new Set(Array.isArray(projectIds) ? projectIds : [projectIds]);
  const mine = (await requestJson(power, "get", "/marketplace/my-submissions")).body ?? [];
  return mine.filter(
    (item) => ids.has(item.source_project_id) && ["submitted", "draft"].includes(item.status),
  );
}

async function cleanupReviewResidue(power, admin, projectIds) {
  const errors = [];
  const rows = await activeReviewRows(power, projectIds);
  for (const row of rows) {
    try {
      if (row.status === "submitted") {
        const withdrawn = await requestJson(
          power,
          "post",
          `/marketplace/submissions/${row.id}/withdraw`,
          { expected: [200, 404] },
        );
        if (withdrawn.status === 200) {
          assert(withdrawn.body?.status === "withdrawn", `review ${row.id}: submission withdrew`);
        }
      } else if (row.status === "draft") {
        await requestJson(admin, "delete", `/admin/marketplace/samples/${row.id}`, {
          expected: [204, 404],
        });
      }
    } catch (error) {
      errors.push(new Error(`review ${row.id} cleanup failed: ${safeError(error?.message ?? error)}`));
    }
  }
  try {
    const residue = await activeReviewRows(power, projectIds);
    if (residue.length) {
      errors.push(
        new Error(
          `review cleanup left submitted/draft residue: ${residue
            .map((row) => `${row.id}:${row.status}`)
            .join(",")}`,
        ),
      );
    }
  } catch (error) {
    errors.push(new Error(`review cleanup verification failed: ${safeError(error?.message ?? error)}`));
  }
  if (errors.length) throw new AggregateError(errors, "review cleanup did not converge");
}

async function exerciseAdminReview(power, admin, project, fixture) {
  await cleanupReviewResidue(power, admin, project.id);
  const submitted = (
    await requestJson(power, "post", `/projects/${project.id}/submit-to-marketplace`, {
      expected: [201],
      data: {
        title: `Rehearsal ${fixture.title}`.slice(0, 60),
        summary: `Synthetic reversible admin review for ${fixture.title}. No publication.`,
        category: fixture.category,
        keywords: ["portfolio-rehearsal", fixture.portfolio_key],
      },
    })
  ).body;
  const reviewId = submitted.id;
  let result = null;
  let primaryError = null;
  try {
    const queue = (
      await requestJson(admin, "get", "/admin/marketplace/submissions?status=submitted")
    ).body;
    const queued = (queue ?? []).filter((item) => item.id === reviewId);
    assert(queued.length === 1, `${fixture.title}: admin review queue contains one submission`);
    const approved = (
      await requestJson(admin, "post", `/admin/marketplace/submissions/${reviewId}/approve`)
    ).body;
    assert(approved.status === "draft", `${fixture.title}: admin approval enters draft curation`);
    const draft = (
      await requestJson(admin, "get", `/admin/marketplace/samples/${reviewId}`)
    ).body;
    const curated = (
      await requestJson(admin, "put", `/admin/marketplace/samples/${reviewId}`, {
        data: {
          title: draft.title,
          description: draft.description,
          long_description: draft.long_description,
          category: draft.category,
          complexity: fixture.complexity,
          models_used: fixture.models_used,
          template_id: draft.template_id,
          spec_snapshot: draft.spec_snapshot,
          assets: draft.assets ?? {},
          keywords: draft.keywords ?? [],
          metadata_extra: {
            ...(draft.metadata_extra ?? {}),
            portfolio_rehearsal_review: {
              fixture_sha256: fixtureDigest(fixture),
              reviewed_via: "admin_submission_to_draft",
            },
          },
        },
      })
    ).body;
    assert(curated.status === "draft", `${fixture.title}: admin curation stayed unpublished`);
    result = { submission_id: reviewId, reviewed: true, published: false };
  } catch (error) {
    primaryError = error;
  }
  let cleanupError = null;
  try {
    await cleanupReviewResidue(power, admin, project.id);
  } catch (error) {
    cleanupError = error;
  }
  if (primaryError && cleanupError) {
    throw new AggregateError(
      [primaryError, cleanupError],
      `${fixture.title}: review exercise and cleanup both failed`,
    );
  }
  if (primaryError) throw primaryError;
  if (cleanupError) throw cleanupError;
  return result;
}

async function getDeployment(power, projectId, allowed404 = true) {
  const response = await power.ctx.request.get(api(`/projects/${projectId}/deployment`));
  if (response.status() === 404 && allowed404) return null;
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok()) throw new Error(`GET deployment returned HTTP ${response.status()}`);
  return body;
}

async function deploymentHistory(power, projectId) {
  const body = (
    await requestJson(power, "get", `/projects/${projectId}/deployments`)
  ).body;
  return body ?? [];
}

async function captureDeploymentBaseline(power, project, label) {
  const history = await deploymentHistory(power, project.id);
  requireExhaustiveDeploymentHistory(history, `${label} deployment baseline`);
  const active = history.filter((row) => row.status === "active");
  if (active.length > 1) {
    throw new Error(
      `${label}: multiple active deployments prevent a safe baseline: ` +
        active.map((row) => row.id).join(","),
    );
  }
  return { active: active[0] ?? null };
}

function baselineActiveIsIntact(history, baseline) {
  if (!baseline?.active) return false;
  const current = history.find((row) => row.id === baseline.active.id);
  const active = history.filter((row) => row.status === "active");
  const inflight = history.filter(
    (row) =>
      ACTIVE_OR_INFLIGHT_DEPLOYMENT_STATES.has(row.status) && row.status !== "active",
  );
  return (
    current?.status === "active" &&
    current.build_id === baseline.active.build_id &&
    current.app_url === baseline.active.app_url &&
    active.length === 1 &&
    active[0].id === baseline.active.id &&
    inflight.length === 0
  );
}

function requireExhaustiveDeploymentHistory(history, label) {
  if (history.length >= DEPLOYMENT_HISTORY_LIMIT) {
    throw new Error(
      `${label}: deployment history returned the ${DEPLOYMENT_HISTORY_LIMIT}-row API cap; ` +
        "exhaustive safety cannot be proven",
    );
  }
}

async function exhaustiveDeploymentHistoryWithRetries(power, projectId, label) {
  const errors = [];
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const history = await deploymentHistory(power, projectId);
      requireExhaustiveDeploymentHistory(history, label);
      return history;
    } catch (error) {
      errors.push(error);
      if (attempt < 3) await power.page.waitForTimeout(1_000);
    }
  }
  throw new AggregateError(errors, `${label}: exhaustive history proof failed after 3 attempts`);
}

async function pollDeployment(power, projectId, terminal, timeoutSteps = 160) {
  for (let attempt = 0; attempt < timeoutSteps; attempt += 1) {
    const deployment = await getDeployment(power, projectId, false);
    if (terminal.has(deployment.status)) return deployment;
    await power.page.waitForTimeout(5_000);
  }
  throw new Error(`Deployment for ${projectId} did not reach ${[...terminal].join("/")}`);
}

function sharesDeploymentResources(row, target) {
  if (row.id === target.id) return true;
  const sharesStack = Boolean(target.stack_name && row.stack_name === target.stack_name);
  const sharesExternalLease = Boolean(
    target.lease_external_id && row.lease_external_id === target.lease_external_id,
  );
  const sharesLeaseAccountFallback = Boolean(
    !target.lease_external_id &&
      !row.lease_external_id &&
      target.lease_account_id &&
      row.lease_account_id === target.lease_account_id,
  );
  return sharesStack || sharesExternalLease || sharesLeaseAccountFallback;
}

async function pollTeardownTarget(power, project, target, label, timeoutSteps = 180) {
  assert(Boolean(target?.id), `${label}: teardown product response returned an exact deployment ID`);
  let exact = target;
  let siblings = [target];
  for (let attempt = 0; attempt < timeoutSteps; attempt += 1) {
    const history = await deploymentHistory(power, project.id);
    requireExhaustiveDeploymentHistory(history, label);
    exact = history.find((row) => row.id === target.id);
    if (!exact) {
      throw new Error(
        `${label}: teardown target ${target.id} is absent from exhaustive deployment history`,
      );
    }
    siblings = history.filter((row) => sharesDeploymentResources(row, target));
    const unresolved = siblings.filter((row) => !TEARDOWN_COMPLETE_STATES.has(row.status));
    if (TEARDOWN_COMPLETE_STATES.has(exact.status) && unresolved.length === 0) {
      console.log(
        `PASS  ${label}: exact teardown ${target.id} and all stack/lease siblings converged`,
      );
      return exact;
    }
    if (exact.status === "failed") {
      throw new Error(
        `${label}: exact teardown target ${target.id} failed: ${safeError(exact.error ?? "unknown")}`,
      );
    }
    if (
      !ACTIVE_OR_INFLIGHT_DEPLOYMENT_STATES.has(exact.status) &&
      !TEARDOWN_COMPLETE_STATES.has(exact.status)
    ) {
      throw new Error(`${label}: teardown target ${target.id} entered unknown state ${exact.status}`);
    }
    await power.page.waitForTimeout(5_000);
  }
  throw new Error(
    `${label}: teardown target ${target.id} did not converge; ` +
      `target=${exact?.status ?? "missing"}, siblings=${siblings
        .map((row) => `${row.id}:${row.status}`)
        .join(",")}`,
  );
}

function assessmentId(risk) {
  return risk?.id ?? risk?.assessment_id ?? risk?.timeline?.[0]?.id ?? null;
}

async function deployCanonical(power, admin, project, build, fixture) {
  let current = await getDeployment(power, project.id);
  if (current && ["pending", "pre_flight", "leasing", "deploying", "updating", "tearing_down"].includes(current.status)) {
    current = await pollDeployment(power, project.id, new Set(["active", "failed", "torn_down"]));
  }
  if (current?.status === "active" && current.build_id === build.id) {
    assert(current.mode === "full_governance", `${fixture.title}: reused deployment is Full Governance`);
    return current;
  }
  if (current?.status === "active" || current?.status === "failed") {
    const torn = await teardown(power, project, `${fixture.title} prior deployment`);
    assert(
      torn === null || TEARDOWN_COMPLETE_STATES.has(torn.status),
      `${fixture.title}: prior deployment reconciled through exact-row teardown`,
      torn,
    );
  }

  const submit = async () =>
    power.ctx.request.post(api(`/projects/${project.id}/deploy`), {
      data: { build_id: build.id, mode: "full_governance" },
    });
  let response = await submit();
  if (response.status() === 403) {
    const refusal = await response.json().catch(() => ({}));
    const code = refusal?.detail?.code;
    assert(code === "risk_pending", `${fixture.title}: Full Governance risk gate held correctly`, code);
    const risk = (await requestJson(power, "get", `/projects/${project.id}/risk`)).body;
    const id = assessmentId(risk);
    assert(Boolean(id), `${fixture.title}: pending risk assessment has an ID`);
    await requestJson(admin, "post", `/admin/risk-assessments/${id}/decide`, {
      data: {
        outcome: "approve",
        notes: `Portfolio rehearsal for ${fixture.title}; synthetic fixture ${fixtureDigest(fixture)}.`,
      },
    });
    response = await submit();
  }
  if (response.status() !== 202) {
    const body = await response.text();
    throw new Error(`Deploy ${fixture.title} returned HTTP ${response.status()}: ${safeError(body)}`);
  }
  const deployment = await pollDeployment(power, project.id, new Set(["active", "failed"]));
  assert(deployment.status === "active", `${fixture.title}: deployment became active`, deployment.status);
  assert(deployment.mode === "full_governance", `${fixture.title}: deployment mode is Full Governance`);
  assert(deployment.build_id === build.id, `${fixture.title}: deployment is pinned to accepted build`);
  return deployment;
}

async function teardown(power, project, label) {
  let last = null;
  for (let attempt = 0; attempt < 180; attempt += 1) {
    const history = await deploymentHistory(power, project.id);
    requireExhaustiveDeploymentHistory(history, label);
    if (history.length === 0) return null;

    const tearingDown = history.filter((row) => row.status === "tearing_down");
    if (tearingDown.length > 0) {
      last = await pollTeardownTarget(power, project, tearingDown[0], label);
      continue;
    }

    const otherInflight = history.filter(
      (row) =>
        ACTIVE_OR_INFLIGHT_DEPLOYMENT_STATES.has(row.status) &&
        !["active", "tearing_down"].includes(row.status),
    );
    if (otherInflight.length > 0) {
      await power.page.waitForTimeout(5_000);
      continue;
    }

    const active = history.filter((row) => row.status === "active");
    if (active.length > 1) {
      throw new Error(
        `${label}: multiple active deployment rows prevent safe teardown: ` +
          active.map((row) => row.id).join(","),
      );
    }
    const target =
      active[0] ?? (history[0]?.status === "failed" ? history[0] : null);
    if (target) {
      const response = await requestJson(
        power,
        "post",
        `/projects/${project.id}/deployment/teardown`,
        { expected: [202, 409] },
      );
      if (response.status === 202) {
        assert(
          response.body?.id === target.id,
          `${label}: product teardown targeted the expected active/failed row`,
          { expected: target.id, returned: response.body?.id },
        );
        last = await pollTeardownTarget(power, project, response.body, label);
      } else {
        await power.page.waitForTimeout(5_000);
      }
      continue;
    }

    const unknown = history.filter(
      (row) =>
        !TEARDOWN_COMPLETE_STATES.has(row.status) &&
        row.status !== "failed" &&
        !ACTIVE_OR_INFLIGHT_DEPLOYMENT_STATES.has(row.status),
    );
    if (unknown.length > 0) {
      throw new Error(
        `${label}: deployment history contains unsupported states: ` +
          unknown.map((row) => `${row.id}:${row.status}`).join(","),
      );
    }
    return last ?? history[0];
  }
  throw new Error(`${label}: teardown did not converge within 900 seconds`);
}

async function revealApiKey(power, projectId) {
  const response = await power.ctx.request.post(api(`/projects/${projectId}/deployment/api-key/reveal`));
  if (!response.ok()) throw new Error(`API-key reveal returned HTTP ${response.status()}`);
  const body = await response.json();
  const value = body?.value;
  if (!value || typeof value !== "string") throw new Error("API-key reveal returned no value");
  SENSITIVE_VALUES.add(value);
  return value;
}

async function deployedJson(ctx, method, url, { key = null, data = undefined, expected = [200] } = {}) {
  const headers = key ? { "x-api-key": key } : undefined;
  const attempts = key && !expected.includes(403) ? 24 : 1;
  let response = null;
  let body = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    response = await ctx.request[method](url, { data, headers, timeout: 45_000 });
    const text = await response.text();
    body = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = text;
      }
    }
    if (response.status() !== 403 || attempt === attempts || expected.includes(403)) break;
    if (attempt === 1) console.log("INFO  waiting for bounded API-key propagation");
    await new Promise((resolve) => setTimeout(resolve, 15_000));
  }
  if (!expected.includes(response.status())) {
    throw new Error(`Deployed ${method.toUpperCase()} ${new URL(url).pathname} returned HTTP ${response.status()}`);
  }
  return { status: response.status(), body };
}

function endpoint(base, route) {
  return `${base.replace(/\/$/, "")}${route}`;
}

async function sentimentDrill(power, fixture, deployment) {
  let key = await revealApiKey(power, deployment.project_id);
  const base = deployment.app_url;
  try {
    const anonymous = await deployedJson(power.ctx, "post", endpoint(base, "/notes"), {
      data: { note: fixture.assets.sample_data_inline.notes[0].note },
      expected: [403],
    });
    assert(anonymous.status === 403, "Sentiment: keyless note call is refused");
    const invalid = await deployedJson(power.ctx, "post", endpoint(base, "/notes"), {
      key: "invalid-demo-key",
      data: { note: fixture.assets.sample_data_inline.notes[0].note },
      expected: [403],
    });
    assert(invalid.status === 403, "Sentiment: invalid API key is refused");
    const auth = {
      keyless_status: anonymous.status,
      keyless_body_sha256: sha256Text(stableJson(anonymous.body)),
      invalid_status: invalid.status,
      invalid_body_sha256: sha256Text(stableJson(invalid.body)),
    };

    const runtime = [];
    let firstResult = null;
    for (const vector of fixture.assets.sample_data_inline.notes) {
      const result = await deployedJson(power.ctx, "post", endpoint(base, "/notes"), {
        key,
        data: { note: vector.note },
      });
      assert(
        result.body.sentiment === vector.expected.sentiment &&
          result.body.score === vector.expected.score &&
          stableJson(result.body.matched_terms) === stableJson(vector.expected.matched_terms),
        `Sentiment: live ${vector.expected.sentiment} vector is exact`,
      );
      if (firstResult === null) firstResult = result.body;
      runtime.push({
        input_sha256: sha256Text(vector.note),
        output_sha256: sha256Text(stableJson(result.body)),
        sentiment: result.body.sentiment,
        score: result.body.score,
      });
    }
    const repeated = await deployedJson(power.ctx, "post", endpoint(base, "/notes"), {
      key,
      data: { note: "  THE ONBOARDING GUIDE WAS CLEAR AND HELPFUL.  " },
    });
    assert(
      stableJson(repeated.body) === stableJson(firstResult),
      "Sentiment: case/whitespace-equivalent note returns the identical ID and response",
    );
    const original = await deployedJson(power.ctx, "get", endpoint(base, "/notes"), { key });
    assert(Boolean(repeated.body.note_id), "Sentiment: digest-derived note ID is present");
    assert(
      Array.isArray(original.body) ||
        Array.isArray(original.body.items) ||
        Array.isArray(original.body.notes),
      "Sentiment: keyed list endpoint responds",
    );

    const consolePage = await power.ctx.newPage();
    await consolePage.goto(endpoint(base, "/app"), { waitUntil: "networkidle", timeout: 60_000 });
    const keyInput = consolePage
      .locator('input[type="password"], input[name*="key" i], input[id*="key" i]')
      .first();
    const noteInput = consolePage
      .locator('textarea, input[name*="note" i], input[id*="note" i]')
      .first();
    assert((await keyInput.count()) > 0, "Sentiment: B19 paste-key field rendered");
    assert((await noteInput.count()) > 0, "Sentiment: B19 note field rendered");
    await keyInput.fill(key);
    await noteInput.fill(fixture.assets.sample_data_inline.notes[0].note);
    const submit = consolePage.locator('button[type="submit"], button').filter({ hasText: /submit|classify|send/i }).first();
    await submit.click();
    await consolePage.getByText("positive", { exact: false }).first().waitFor({ timeout: 30_000 });
    await keyInput.fill("");
    const storageValues = await consolePage.evaluate(() => ({
      local: Object.values(localStorage),
      session: Object.values(sessionStorage),
      html: document.documentElement.outerHTML,
    }));
    const serializedStorage = JSON.stringify({
      local: storageValues.local,
      session: storageValues.session,
    });
    assert(
      !serializedStorage.includes(key) && !storageValues.html.includes(key),
      "Sentiment: API key absent from browser storage and DOM before screenshot",
    );
    const screenshot = await consolePage.screenshot({ fullPage: true, type: "png" });
    await consolePage.close();
    assert(!screenshot.includes(Buffer.from(key)), "Sentiment: screenshot bytes do not contain API key");

    await deployedJson(power.ctx, "delete", endpoint(base, "/notes"), { key, expected: [200, 204] });
    return {
      auth,
      runtime,
      screenshot,
      screenshot_sha256: sha256Bytes(screenshot),
      repeated_note_id_sha256: sha256Text(String(repeated.body.note_id)),
    };
  } finally {
    key = null;
  }
}

async function handbookDrill(power, fixture, deployment) {
  let key = await revealApiKey(power, deployment.project_id);
  try {
    const sample = fixture.assets.sample_data_inline;
    const authQuestion = { question: sample.known_answers[0].question };
    const anonymous = await deployedJson(
      power.ctx,
      "post",
      endpoint(deployment.app_url, "/questions"),
      { data: authQuestion, expected: [403] },
    );
    const invalid = await deployedJson(
      power.ctx,
      "post",
      endpoint(deployment.app_url, "/questions"),
      { key: "invalid-demo-key", data: authQuestion, expected: [403] },
    );
    assert(anonymous.status === 403, "Handbook: keyless question is refused");
    assert(invalid.status === 403, "Handbook: invalid API key is refused");
    const auth = {
      keyless_status: anonymous.status,
      keyless_body_sha256: sha256Text(stableJson(anonymous.body)),
      invalid_status: invalid.status,
      invalid_body_sha256: sha256Text(stableJson(invalid.body)),
    };
    const passageById = Object.fromEntries(sample.passages.map((item) => [item.passage_id, item]));
    const vectors = [];
    for (const expected of sample.known_answers) {
      const response = await deployedJson(power.ctx, "post", endpoint(deployment.app_url, "/questions"), {
        key,
        data: { question: expected.question },
      });
      if (expected.abstained) {
        assert(
          response.body.abstained === true &&
            response.body.answer === sample.abstention &&
            stableJson(response.body.citations) === "[]",
          "Handbook: unknown question produces exact abstention",
        );
      } else {
        const citation = response.body.citations?.[0];
        assert(
          response.body.abstained === false &&
            response.body.answer === passageById[expected.passage_id].text &&
            citation?.passage_id === expected.passage_id &&
            citation?.section === passageById[expected.passage_id].section,
          `Handbook: ${expected.passage_id} live citation vector is exact`,
        );
      }
      vectors.push({
        question_sha256: sha256Text(expected.question),
        output_sha256: sha256Text(stableJson(response.body)),
        passage_id: expected.passage_id,
        abstained: expected.abstained,
      });
    }
    return { auth, runtime: vectors };
  } finally {
    key = null;
  }
}

function canonicalPayloadHash(value) {
  return sha256Text(stableJson(value));
}

async function approvalDrill(power, fixture, project, build, deployment) {
  let key = await revealApiKey(power, deployment.project_id);
  let designRestored = false;
  try {
    const sample = fixture.assets.sample_data_inline;
    const anonymous = await deployedJson(
      power.ctx,
      "post",
      endpoint(deployment.app_url, "/requests"),
      { data: sample.request, expected: [403] },
    );
    const invalid = await deployedJson(
      power.ctx,
      "post",
      endpoint(deployment.app_url, "/requests"),
      { key: "invalid-demo-key", data: sample.request, expected: [403] },
    );
    assert(anonymous.status === 403, "Approval: keyless request is refused");
    assert(invalid.status === 403, "Approval: invalid API key is refused");
    const auth = {
      keyless_status: anonymous.status,
      keyless_body_sha256: sha256Text(stableJson(anonymous.body)),
      invalid_status: invalid.status,
      invalid_body_sha256: sha256Text(stableJson(invalid.body)),
    };
    const created = await deployedJson(power.ctx, "post", endpoint(deployment.app_url, "/requests"), {
      key,
      data: sample.request,
      expected: [200, 201],
    });
    const requestRecord =
      created.body?.request_id || created.body?.id
        ? created.body
        : (created.body?.request ?? created.body);
    const requestId = requestRecord.request_id ?? requestRecord.id;
    assert(Boolean(requestId), "Approval: synthetic request has an ID");
    assert(
      stableJson(requestRecord.policy_result) === stableJson(sample.expected_policy),
      "Approval: deterministic procurement policy is exact",
      requestRecord.policy_result,
    );
    assert(
      requestRecord.human_decision === null || requestRecord.human_decision === undefined,
      "Approval: request starts without a human decision",
    );

    const drafted = await deployedJson(
      power.ctx,
      "post",
      endpoint(deployment.app_url, `/requests/${requestId}/draft`),
      { key, data: {} },
    );
    const draftedRecord = drafted.body.request ?? drafted.body;
    const recommendation =
      draftedRecord.draft_recommendation?.recommendation ?? drafted.body.recommendation;
    assert(
      recommendation === sample.expected_draft_recommendation,
      "Approval: Bedrock produced the expected advisory draft recommendation",
      recommendation,
    );
    assert(
      draftedRecord.human_decision === null || draftedRecord.human_decision === undefined,
      "Approval: Bedrock draft did not set the human decision",
    );

    const decided = await deployedJson(
      power.ctx,
      "post",
      endpoint(deployment.app_url, `/requests/${requestId}/decision`),
      {
        key,
        data: {
          decision: sample.evidence_human_decision,
          approver_ref: "APPROVER-DEMO-7",
          comment: "Synthetic rehearsal rejection to prove human/model separation.",
        },
      },
    );
    const decidedRecord = decided.body.request ?? decided.body;
    const humanDecision = decidedRecord.human_decision?.decision ?? decided.body.decision;
    assert(humanDecision === "rejected", "Approval: distinct human rejection is recorded");
    assert(recommendation !== humanDecision, "Approval: advisory draft and human decision remain distinct");
    const second = await deployedJson(
      power.ctx,
      "post",
      endpoint(deployment.app_url, `/requests/${requestId}/decision`),
      {
        key,
        data: { decision: "approved", approver_ref: "APPROVER-DEMO-8", comment: "must conflict" },
        expected: [409],
      },
    );
    assert(second.status === 409, "Approval: second human decision is refused immutably");

    const audit = await deployedJson(
      power.ctx,
      "get",
      endpoint(deployment.app_url, `/requests/${requestId}/audit`),
      { key },
    );
    const events = audit.body.items ?? audit.body.events ?? audit.body;
    assert(Array.isArray(events) && events.length >= 3, "Approval: immutable audit events are readable");
    const sortKeys = events.map((event) => event.sk ?? event.sort_key ?? event.timestamp);
    assert(
      sortKeys.every((key) => typeof key === "string" && key.length > 0) &&
        new Set(sortKeys).size === sortKeys.length &&
        stableJson(sortKeys) === stableJson([...sortKeys].sort()),
      "Approval: audit events have unique sort keys in strict order",
      sortKeys,
    );
    for (const event of events) {
      const hash = event.payload_sha256 ?? event.sha256 ?? event.hash;
      assert(/^[0-9a-f]{64}$/.test(hash ?? ""), "Approval: audit event carries SHA-256");
      assert(
        Object.prototype.hasOwnProperty.call(event, "payload"),
        "Approval: audit event exposes its canonical payload for independent verification",
      );
      assert(
        canonicalPayloadHash(event.payload) === hash,
        "Approval: stored audit payload SHA-256 recomputes exactly",
      );
    }

    const same = (
      await requestJson(power, "post", `/projects/${project.id}/deployment/preview`, {
        data: { build_id: build.id },
        timeout: 60_000,
      })
    ).body;
    assert(same.no_changes === true && same.changes.length === 0, "Approval: baseline preview has no changes");

    const overlay = fixture.assets.sample_data_inline.sns_preview_overlay;
    const candidateDesign =
      `${fixture.spec_snapshot.design_md}\n\n` +
      "## Rehearsal-only SNS candidate (DO NOT DEPLOY)\n" +
      `Add exactly one SNS topic with CloudFormation logical ID \`${overlay.logical_id}\` ` +
      `and resource type \`${overlay.resource_type}\`. This candidate exists solely ` +
      "for CloudFormation changeset preview and must never be deployed. " +
      "Overlay derivation contract: accepted-baseline-plus-topic v1.\n";
    await requestJson(power, "put", `/projects/${project.id}/specs/design`, {
      data: { content: candidateDesign },
    });
    const candidateSnapshot = { ...fixture.spec_snapshot, design_md: candidateDesign };
    const candidateHash = specHash(candidateSnapshot);
    const candidate = await ensureBuild(power, project, candidateHash, "Approval SNS candidate");
    const artifacts = (await requestJson(power, "get", `/builds/${candidate.id}/artifacts`)).body.items ?? [];
    const templatePath = artifacts.some((item) => item.path === "template.json")
      ? "template.json"
      : "synth/template.json";
    const template = (
      await requestJson(power, "get", `/builds/${candidate.id}/artifacts/${templatePath}`)
    ).body;
    const parsed = JSON.parse(template.content);
    assert(
      parsed.Resources?.[overlay.logical_id]?.Type === overlay.resource_type,
      "Approval: candidate artifact contains the exact SNS topic",
    );

    const preview = (
      await requestJson(power, "post", `/projects/${project.id}/deployment/preview`, {
        data: { build_id: candidate.id },
        timeout: 60_000,
      })
    ).body;
    assert(preview.no_changes === false, "Approval: SNS candidate preview reports changes");
    assert(
      preview.changes.some(
        (change) =>
          change.logical_id === overlay.logical_id &&
          change.resource_type === overlay.resource_type,
      ),
      "Approval: preview names the SNS candidate resource",
      preview.changes,
    );
    const stillLive = await getDeployment(power, project.id, false);
    assert(
      stillLive.status === "active" && stillLive.build_id === build.id,
      "Approval: preview left the live deployment on the baseline build",
    );

    await requestJson(power, "put", `/projects/${project.id}/specs/design`, {
      data: { content: fixture.spec_snapshot.design_md },
    });
    designRestored = true;
    const restoredHash = await ensureCanonicalSpecs(power, project, fixture);
    assert(restoredHash === specHash(fixture.spec_snapshot), "Approval: canonical design restored exactly");

    return {
      auth,
      runtime: {
        request_id_sha256: sha256Text(String(requestId)),
        policy_sha256: sha256Text(stableJson(requestRecord.policy_result)),
        advisory_recommendation: recommendation,
        human_decision: humanDecision,
        audit_event_count: events.length,
        audit_vector_sha256: sha256Text(stableJson(events)),
      },
      previews: {
        no_change: same,
        sns_candidate: preview,
        candidate_build_id: candidate.id,
        candidate_spec_hash: candidateHash,
      },
    };
  } finally {
    key = null;
    if (!designRestored) {
      await requestJson(power, "put", `/projects/${project.id}/specs/design`, {
        data: { content: fixture.spec_snapshot.design_md },
      });
      const restoredHash = await ensureCanonicalSpecs(power, project, fixture);
      assert(
        restoredHash === specHash(fixture.spec_snapshot),
        "Approval: canonical design cleanup restored exactly",
      );
    }
  }
}

async function catalogRows(admin, fixtures) {
  const rows = {};
  for (const fixture of fixtures) {
    const listed = (
      await requestJson(
        admin,
        "get",
        `/admin/marketplace/samples?q=${encodeURIComponent(fixture.title)}&page_size=100`,
      )
    ).body.items;
    const exact = listed.filter((item) => item.title === fixture.title);
    if (exact.length !== 1) {
      throw new Error(`${fixture.title}: expected one catalog row, found ${exact.map((item) => item.id).join(",")}`);
    }
    const full = (
      await requestJson(admin, "get", `/admin/marketplace/samples/${exact[0].id}`)
    ).body;
    assert(full.status === "published", `${fixture.title}: canonical catalog row is published`);
    assert(
      full.metadata_extra?.portfolio_key === fixture.portfolio_key,
      `${fixture.title}: catalog stable portfolio key matches`,
    );
    const expectedPayloadDigest = catalogPayloadDigest(fixture);
    const observedPayloadDigest = sha256Text(stableJson(catalogPayloadFromRow(full)));
    assert(
      observedPayloadDigest === expectedPayloadDigest,
      `${fixture.title}: published catalog payload exactly matches the canonical fixture`,
      { expectedPayloadDigest, observedPayloadDigest },
    );
    rows[fixture.portfolio_key] = full;
  }
  return rows;
}

function editableCatalogPayload(row, metadataExtra, assets, fixture = null) {
  const source = fixture ?? row;
  return {
    title: source.title,
    description: source.description,
    long_description: source.long_description,
    category: source.category,
    complexity: source.complexity,
    models_used: source.models_used ?? [],
    template_id: fixture ? null : row.template_id,
    spec_snapshot: source.spec_snapshot ?? {},
    assets,
    keywords: source.keywords ?? [],
    metadata_extra: metadataExtra,
  };
}

function catalogEditableFingerprint(row) {
  return stableJson({
    status: row.status,
    title: row.title,
    description: row.description,
    long_description: row.long_description,
    category: row.category,
    complexity: row.complexity,
    models_used: row.models_used ?? [],
    template_id: row.template_id ?? null,
    spec_snapshot: row.spec_snapshot ?? {},
    assets: row.assets ?? {},
    keywords: row.keywords ?? [],
    metadata_extra: row.metadata_extra ?? {},
  });
}

async function rollbackCatalogUpdates(admin, mutation) {
  if (!mutation || mutation.finalized || mutation.rolledBack) return;
  const errors = [];
  for (const baseline of [...mutation.baselines].reverse()) {
    let restored = false;
    let lastError = null;
    for (let attempt = 1; attempt <= 3 && !restored; attempt += 1) {
      let putError = null;
      try {
        await requestJson(admin, "put", `/admin/marketplace/samples/${baseline.id}`, {
          data: editableCatalogPayload(
            baseline,
            baseline.metadata_extra ?? {},
            baseline.assets ?? {},
          ),
        });
      } catch (error) {
        // A transport failure can follow a committed PUT. Always perform the
        // fresh fingerprint read before deciding restoration failed.
        putError = error;
      }

      let verificationError = null;
      try {
        const observed = (
          await requestJson(admin, "get", `/admin/marketplace/samples/${baseline.id}`)
        ).body;
        if (catalogEditableFingerprint(observed) === catalogEditableFingerprint(baseline)) {
          restored = true;
          console.log(`PASS  ${baseline.title}: catalog rollback restored the complete baseline`);
          break;
        }
        verificationError = new Error("fresh catalog fingerprint does not match the baseline");
      } catch (error) {
        verificationError = error;
      }
      lastError = new AggregateError(
        [putError, verificationError].filter(Boolean),
        `${baseline.title}: rollback attempt ${attempt} did not verify`,
      );
    }
    if (!restored) {
      errors.push(
        new Error(
          `${baseline.title}: catalog rollback failed after 3 bounded attempts: ` +
            safeError(errorSummary(lastError)),
        ),
      );
    }
  }
  if (errors.length) throw new AggregateError(errors, "catalog rollback did not converge");
  mutation.rolledBack = true;
}

async function invalidateCatalogDeployment(admin, baseline, reason) {
  const assets = {
    ...(baseline.assets ?? {}),
    screenshots: [],
    demo_url: null,
    evidence: {
      status: "unverified",
      private: true,
      required: baseline.assets?.evidence?.required ?? [],
    },
  };
  const metadata = {
    ...(baseline.metadata_extra ?? {}),
    last_verified: null,
    verification: {
      status: "unverified",
      invalidated_at: nowIso(),
      reason,
    },
  };
  const expected = { ...baseline, assets, metadata_extra: metadata };
  let lastError = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    let putError = null;
    try {
      await requestJson(admin, "put", `/admin/marketplace/samples/${baseline.id}`, {
        data: editableCatalogPayload(baseline, metadata, assets),
      });
    } catch (error) {
      putError = error;
    }
    let verificationError = null;
    try {
      const observed = (
        await requestJson(admin, "get", `/admin/marketplace/samples/${baseline.id}`)
      ).body;
      if (catalogEditableFingerprint(observed) === catalogEditableFingerprint(expected)) {
        console.log(
          `PASS  ${baseline.title}: invalidated catalog evidence after active deployment loss`,
        );
        return;
      }
      verificationError = new Error("fresh catalog fingerprint does not match invalidation");
    } catch (error) {
      verificationError = error;
    }
    lastError = new AggregateError(
      [putError, verificationError].filter(Boolean),
      `${baseline.title}: catalog invalidation attempt ${attempt} did not verify`,
    );
  }
  throw new Error(
    `${baseline.title}: catalog invalidation failed after 3 bounded attempts: ` +
      safeError(errorSummary(lastError)),
  );
}

async function updateCatalogAfterEvidence(admin, fixtures, baselines, evidence, mutation) {
  if (!mutation || mutation.baselines.length !== 0) {
    throw new Error("catalog mutation tracker must be caller-owned and empty before updates");
  }
  try {
    for (const fixture of fixtures) {
      const row = baselines[fixture.portfolio_key];
      const journey = evidence.journeys[fixture.portfolio_key];
      const screenshot = evidence.screenshots[fixture.portfolio_key];
      const assets = {
        ...(row.assets ?? {}),
        screenshots: screenshot
          ? [{
              label: "B19 live browser rehearsal",
              s3_key: screenshot.key,
              sha256: screenshot.sha256,
            }]
          : [],
        demo_url:
          fixture.portfolio_key === "sentiment-notes-agent" && KEEP_SENTIMENT_ACTIVE
            ? journey.deployment.app_url
            : null,
        evidence: {
          ...(row.assets?.evidence ?? {}),
          status: "verified",
          private: true,
          bucket: evidence.bucket,
          prefix: evidence.prefix,
          manifest_key: evidence.manifest.key,
          manifest_sha256: evidence.manifest.sha256,
        },
      };
      const metadata = {
        ...(row.metadata_extra ?? {}),
        last_verified: evidence.verified_at,
        verification: {
          status: "verified",
          verified_at: evidence.verified_at,
          run_id: evidence.run_id,
          fixture_sha256: fixtureDigest(fixture),
          catalog_payload_sha256: catalogPayloadDigest(fixture),
          spec_hash: journey.spec_hash,
          build_id: journey.build.id,
          content_hash: journey.build.content_hash,
          b23_summary: journey.build.manifest.conformance.summary,
          semantic_vector_sha256: journey.semantic_vector_sha256,
          manifest_key: evidence.manifest.key,
          manifest_sha256: evidence.manifest.sha256,
          deployment_status: journey.final_deployment_status,
        },
      };
      // Enroll before the mutating request. If the server commits and the
      // response is lost, rollback still restores this baseline idempotently.
      mutation.baselines.push(row);
      const updatedRow = (
        await requestJson(admin, "put", `/admin/marketplace/samples/${row.id}`, {
          data: editableCatalogPayload(row, metadata, assets, fixture),
        })
      ).body;
      assert(
        sha256Text(stableJson(catalogPayloadFromRow(updatedRow))) ===
          catalogPayloadDigest(fixture),
        `${fixture.title}: verified catalog update remains bound to canonical payload`,
      );
    }
    return mutation;
  } catch (error) {
    try {
      await rollbackCatalogUpdates(admin, mutation);
    } catch (rollbackError) {
      throw new AggregateError(
        [error, rollbackError],
        "catalog update failed and rollback did not converge",
      );
    }
    throw error;
  }
}

async function listPublishedCatalog(admin) {
  const items = [];
  for (let page = 1; ; page += 1) {
    const body = (
      await requestJson(
        admin,
        "get",
        `/admin/marketplace/samples?status=published&page=${page}&page_size=100`,
      )
    ).body;
    items.push(...(body.items ?? []));
    if (items.length >= Number(body.total ?? items.length)) return { items, total: body.total };
  }
}

async function verifyExactThreeCatalog(admin, fixtures, uploaded) {
  const published = await listPublishedCatalog(admin);
  assert(
    published.total === 3 && published.items.length === 3,
    "published catalog contains exactly three rows",
    { total: published.total, ids: published.items.map((item) => item.id) },
  );
  assert(
    new Set(published.items.map((item) => item.id)).size === 3,
    "published catalog row IDs are unique",
  );
  const rows = await Promise.all(
    published.items.map(async (card) =>
      (await requestJson(admin, "get", `/admin/marketplace/samples/${card.id}`)).body,
    ),
  );
  const expectedKeys = fixtures.map((fixture) => fixture.portfolio_key).sort();
  const observedKeys = rows.map((item) => item.metadata_extra?.portfolio_key).sort();
  assert(
    stableJson(observedKeys) === stableJson(expectedKeys),
    "published catalog keys are exactly the canonical portfolio keys",
    { expectedKeys, observedKeys },
  );

  const bound = {};
  for (const fixture of fixtures) {
    const row = rows.find(
      (item) => item.metadata_extra?.portfolio_key === fixture.portfolio_key,
    );
    assert(Boolean(row), `${fixture.title}: published catalog row exists`);
    const journey = uploaded.journeys[fixture.portfolio_key];
    const verification = row.metadata_extra?.verification ?? {};
    const evidence = row.assets?.evidence ?? {};
    const screenshots = row.assets?.screenshots ?? [];
    const expectedScreenshot = uploaded.screenshots[fixture.portfolio_key] ?? null;
    const isSentiment = fixture.portfolio_key === "sentiment-notes-agent";
    const expectedDemoUrl =
      journey.final_deployment_status === "active" ? journey.deployment.app_url : null;
    assert(row.status === "published", `${fixture.title}: final catalog row is published`);
    assert(
      sha256Text(stableJson(catalogPayloadFromRow(row))) === catalogPayloadDigest(fixture),
      `${fixture.title}: final catalog payload remains canonical`,
    );
    assert(
      row.metadata_extra?.portfolio_key === fixture.portfolio_key &&
        row.metadata_extra?.last_verified === uploaded.verified_at &&
        verification.status === "verified" &&
        verification.verified_at === uploaded.verified_at &&
        verification.run_id === uploaded.run_id &&
        verification.fixture_sha256 === fixtureDigest(fixture) &&
        verification.catalog_payload_sha256 === catalogPayloadDigest(fixture) &&
        verification.spec_hash === journey.spec_hash &&
        verification.build_id === journey.build.id &&
        verification.content_hash === journey.build.content_hash &&
        stableJson(verification.b23_summary) ===
          stableJson(journey.build.manifest.conformance.summary) &&
        verification.semantic_vector_sha256 === journey.semantic_vector_sha256 &&
        verification.manifest_key === uploaded.manifest.key &&
        verification.manifest_sha256 === uploaded.manifest.sha256 &&
        verification.deployment_status === journey.final_deployment_status,
      `${fixture.title}: every final verification field is bound to this exact run`,
    );
    assert(
      evidence.status === "verified" &&
        evidence.private === true &&
        evidence.bucket === uploaded.bucket &&
        evidence.prefix === uploaded.prefix &&
        evidence.manifest_key === uploaded.manifest.key &&
        evidence.manifest_sha256 === uploaded.manifest.sha256,
      `${fixture.title}: private evidence binding is exact`,
    );
    assert(
      isSentiment
        ? Boolean(expectedScreenshot) &&
            screenshots.length === 1 &&
            screenshots[0]?.label === "B19 live browser rehearsal" &&
            screenshots[0]?.s3_key === expectedScreenshot.key &&
            screenshots[0]?.sha256 === expectedScreenshot.sha256
        : expectedScreenshot === null && screenshots.length === 0,
      `${fixture.title}: screenshot presence, key, and hash are exact`,
      { screenshots, expectedScreenshot },
    );
    assert(
      row.assets?.demo_url === expectedDemoUrl,
      `${fixture.title}: demo URL is exact for active/null posture`,
      { expected: expectedDemoUrl, observed: row.assets?.demo_url },
    );
    bound[fixture.portfolio_key] = row.id;
  }
  return bound;
}

async function verifyZeroReviewResidue(power, projectIds) {
  const residue = await activeReviewRows(power, projectIds);
  assert(
    residue.length === 0,
    "rehearsal leaves zero submitted/draft marketplace residue",
    residue.map((row) => ({ id: row.id, project_id: row.source_project_id, status: row.status })),
  );
}

async function verifyDeploymentConvergence(power, projects, evidence) {
  const expected = {
    "employee-handbook-qa": "torn_down",
    "approval-workflow-copilot": "torn_down",
    "sentiment-notes-agent": KEEP_SENTIMENT_ACTIVE ? "active" : "torn_down",
  };
  const observed = {};
  const activeRows = [];
  for (const [key, status] of Object.entries(expected)) {
    const project = projects[key];
    const latest = await getDeployment(power, project.id, false);
    assert(latest.status === status, `${key}: final deployment status is exactly ${status}`, latest);
    if (key === "sentiment-notes-agent" && status === "active") {
      assert(
        latest.build_id === evidence[key].build.id,
        "Sentiment: optional active deployment remains pinned to the accepted build",
      );
    }
    const history = await deploymentHistory(power, project.id);
    requireExhaustiveDeploymentHistory(history, key);
    console.log(
      `PASS  ${key}: deployment history is exhaustive below the ` +
        `${DEPLOYMENT_HISTORY_LIMIT}-row safety cap`,
    );
    for (const row of history.filter((item) => item.status === "active")) {
      activeRows.push({ key, id: row.id, build_id: row.build_id });
    }
    observed[key] = latest.status;
  }
  const expectedActive = KEEP_SENTIMENT_ACTIVE ? ["sentiment-notes-agent"] : [];
  assert(
    stableJson(activeRows.map((row) => row.key).sort()) === stableJson(expectedActive),
    "only Sentiment is optionally active across rehearsal deployment histories",
    activeRows,
  );
  return observed;
}

function rfc3986(value) {
  return encodeURIComponent(value).replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
}

function amzDates(date = new Date()) {
  const stamp = date.toISOString().replace(/[:-]|\.\d{3}/g, "");
  return { amzDate: stamp, dateStamp: stamp.slice(0, 8) };
}

function hmac(key, value, encoding = undefined) {
  return crypto.createHmac("sha256", key).update(value, "utf8").digest(encoding);
}

async function fetchJsonCredentialUri(uri, headers = {}) {
  const response = await fetch(uri, { headers, signal: AbortSignal.timeout(10_000) });
  if (!response.ok) throw new Error(`credential endpoint returned HTTP ${response.status}`);
  return response.json();
}

async function resolveAwsCredentials() {
  if (process.env.AWS_ACCESS_KEY_ID && process.env.AWS_SECRET_ACCESS_KEY) {
    return {
      accessKeyId: process.env.AWS_ACCESS_KEY_ID,
      secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY,
      sessionToken: process.env.AWS_SESSION_TOKEN ?? null,
    };
  }
  let uri = null;
  if (process.env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI) {
    uri = `http://169.254.170.2${process.env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI}`;
  } else if (process.env.AWS_CONTAINER_CREDENTIALS_FULL_URI) {
    const parsed = new URL(process.env.AWS_CONTAINER_CREDENTIALS_FULL_URI);
    if (!["169.254.170.2", "127.0.0.1", "localhost", "::1"].includes(parsed.hostname)) {
      throw new Error("AWS_CONTAINER_CREDENTIALS_FULL_URI host is not an allowed local task endpoint");
    }
    uri = parsed.toString();
  }
  if (!uri) {
    throw new Error(
      "Private evidence upload requires ECS task credentials via " +
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI/AWS_CONTAINER_CREDENTIALS_FULL_URI, " +
        "or scoped AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (+ AWS_SESSION_TOKEN).",
    );
  }
  let auth = process.env.AWS_CONTAINER_AUTHORIZATION_TOKEN ?? null;
  if (!auth && process.env.AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE) {
    auth = fs.readFileSync(process.env.AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE, "utf8").trim();
  }
  const payload = await fetchJsonCredentialUri(uri, auth ? { Authorization: auth } : {});
  if (!payload.AccessKeyId || !payload.SecretAccessKey || !payload.Token) {
    throw new Error("ECS task credential response was incomplete");
  }
  if (payload.Expiration && Date.parse(payload.Expiration) < Date.now() + 5 * 60_000) {
    throw new Error("ECS task credentials expire in less than five minutes");
  }
  return {
    accessKeyId: payload.AccessKeyId,
    secretAccessKey: payload.SecretAccessKey,
    sessionToken: payload.Token,
  };
}

function signingKey(secret, dateStamp, region, service) {
  const kDate = hmac(`AWS4${secret}`, dateStamp);
  const kRegion = hmac(kDate, region);
  const kService = hmac(kRegion, service);
  return hmac(kService, "aws4_request");
}

async function signedS3Request(credentials, method, key, body = Buffer.alloc(0), extraHeaders = {}, query = {}) {
  const host = `${EVIDENCE_BUCKET}.s3.${REGION}.amazonaws.com`;
  const canonicalUri = `/${key.split("/").map(rfc3986).join("/")}`;
  const queryEntries = Object.entries(query).sort(([a], [b]) => a.localeCompare(b));
  const canonicalQuery = queryEntries.map(([name, value]) => `${rfc3986(name)}=${rfc3986(value)}`).join("&");
  const payloadHash = sha256Bytes(body);
  const { amzDate, dateStamp } = amzDates();
  const headers = {
    host,
    "x-amz-content-sha256": payloadHash,
    "x-amz-date": amzDate,
    ...Object.fromEntries(Object.entries(extraHeaders).map(([name, value]) => [name.toLowerCase(), String(value).trim()])),
  };
  if (credentials.sessionToken) headers["x-amz-security-token"] = credentials.sessionToken;
  const names = Object.keys(headers).sort();
  const canonicalHeaders = names.map((name) => `${name}:${headers[name].replace(/\s+/g, " ")}\n`).join("");
  const signedHeaders = names.join(";");
  const canonicalRequest = [
    method,
    canonicalUri,
    canonicalQuery,
    canonicalHeaders,
    signedHeaders,
    payloadHash,
  ].join("\n");
  const scope = `${dateStamp}/${REGION}/s3/aws4_request`;
  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, scope, sha256Text(canonicalRequest)].join("\n");
  const signature = hmac(signingKey(credentials.secretAccessKey, dateStamp, REGION, "s3"), stringToSign, "hex");
  headers.authorization =
    `AWS4-HMAC-SHA256 Credential=${credentials.accessKeyId}/${scope}, ` +
    `SignedHeaders=${signedHeaders}, Signature=${signature}`;
  const url = `https://${host}${canonicalUri}${canonicalQuery ? `?${canonicalQuery}` : ""}`;
  const response = await fetch(url, {
    method,
    headers,
    body: method === "GET" || method === "HEAD" ? undefined : body,
    signal: AbortSignal.timeout(60_000),
  });
  if (!response.ok) {
    const responseText = await response.text();
    throw new Error(`private S3 ${method} failed HTTP ${response.status}: ${safeError(responseText).slice(0, 300)}`);
  }
  return response;
}

async function preflightEvidence(credentials) {
  await signedS3Request(credentials, "GET", "", Buffer.alloc(0), {}, {
    "list-type": "2",
    "max-keys": "1",
    prefix: "artifacts/demo-portfolio/",
  });
  console.log("PASS  private evidence bucket is reachable with scoped task credentials");
}

async function putEvidence(credentials, key, body, contentType) {
  const buffer = Buffer.isBuffer(body) ? body : Buffer.from(body);
  const digest = sha256Bytes(buffer);
  await signedS3Request(credentials, "PUT", key, buffer, {
    "content-type": contentType,
    "x-amz-checksum-sha256": crypto.createHash("sha256").update(buffer).digest("base64"),
    "x-amz-meta-sha256": digest,
    "x-amz-server-side-encryption": "AES256",
  });
  return { key, sha256: digest, bytes: buffer.length };
}

function runId() {
  return `${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}-${crypto.randomBytes(5).toString("hex")}`;
}

async function uploadEvidence(credentials, fixtures, journeyEvidence) {
  const id = runId();
  const prefix = `artifacts/demo-portfolio/v1/${id}`;
  const verifiedAt = nowIso();
  const screenshots = {};
  const objects = [];
  for (const fixture of fixtures) {
    const key = fixture.portfolio_key;
    const journey = journeyEvidence[key];
    if (journey.screenshot) {
      const uploaded = await putEvidence(
        credentials,
        `${prefix}/screenshots/${key}.png`,
        journey.screenshot,
        "image/png",
      );
      screenshots[key] = uploaded;
      delete journey.screenshot;
    }
    const body = Buffer.from(`${JSON.stringify(journey, null, 2)}\n`, "utf8");
    objects.push(
      await putEvidence(credentials, `${prefix}/journeys/${key}.json`, body, "application/json"),
    );
  }
  const manifestBody = Buffer.from(
    `${JSON.stringify(
      {
        manifest_type: "marshal-demo-portfolio-rehearsal",
        manifest_version: 1,
        run_id: id,
        verified_at: verifiedAt,
        portfolio_fixture_sha256: sha256Text(stableJson(fixtures)),
        journeys: Object.fromEntries(
          Object.entries(journeyEvidence).map(([key, value]) => [
            key,
            {
              fixture_sha256: value.fixture_sha256,
              catalog_payload_sha256: value.catalog_payload_sha256,
              spec_hash: value.spec_hash,
              build_id: value.build.id,
              content_hash: value.build.content_hash,
              semantic_vector_sha256: value.semantic_vector_sha256,
              final_deployment_status: value.final_deployment_status,
            },
          ]),
        ),
        objects,
        screenshots,
        secrets_included: false,
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
  const manifest = await putEvidence(
    credentials,
    `${prefix}/manifest.json`,
    manifestBody,
    "application/json",
  );
  return {
    run_id: id,
    verified_at: verifiedAt,
    bucket: EVIDENCE_BUCKET,
    prefix,
    manifest,
    screenshots,
    journeys: journeyEvidence,
  };
}

function errorSummary(error) {
  if (error instanceof AggregateError) {
    return `${error.message}: ${error.errors.map((item) => errorSummary(item)).join(" | ")}`;
  }
  return String(error?.message ?? error);
}

async function main() {
  const canonical = loadCanonicalFixtures();
  const fixtures = canonical.fixtures;
  const credentials = await resolveAwsCredentials();
  await preflightEvidence(credentials);

  const browser = await chromium.launch();
  let actors = null;
  const projects = {};
  const evidence = {};
  const deploymentBaselines = {};
  const projectMutations = { retired: [], created: [] };
  let catalogBaselines = null;
  let adminMfa = null;
  let catalogMutation = null;
  let uploaded = null;
  let boundCatalog = null;
  let successReport = null;
  let workflowSucceeded = false;
  let workflowError = null;

  try {
    actors = await ensurePersonas(browser);
    // This assertion precedes every admin mutation (review decisions, risk
    // decisions, curation, and catalog updates).
    adminMfa = await assertAdminMfa(actors.admin);
    catalogBaselines = await catalogRows(actors.admin, fixtures);

    for (const fixture of fixtures) {
      const project = await ensureStableProject(actors.power, fixture, projectMutations);
      projects[fixture.portfolio_key] = project;
      deploymentBaselines[fixture.portfolio_key] = await captureDeploymentBaseline(
        actors.power,
        project,
        fixture.title,
      );
      await ensureBusinessViewer(actors.power, actors.business, project, fixture);
      const expectedSpecHash = await ensureCanonicalSpecs(actors.power, project, fixture);
      const review = await exerciseAdminReview(actors.power, actors.admin, project, fixture);
      const build = await ensureBuild(actors.power, project, expectedSpecHash, fixture.title);
      const semanticVector = {
        b23: b23Vector(build),
        live: null,
      };
      evidence[fixture.portfolio_key] = {
        fixture_sha256: fixtureDigest(fixture),
        catalog_payload_sha256: catalogPayloadDigest(fixture),
        project: {
          id: project.id,
          name: projectName(fixture),
          marker: markerFor(fixture.portfolio_key),
          owner: "power",
        },
        business_viewer_id: actors.business.me.id,
        admin_review: review,
        spec_hash: expectedSpecHash,
        build,
        semantic_vector: semanticVector,
      };
    }

    // Scheduled/on-demand journeys always teardown after their live evidence.
    const handbookFixture = fixtures.find((item) => item.portfolio_key === "employee-handbook-qa");
    const handbookEvidence = evidence[handbookFixture.portfolio_key];
    const handbookDeployment = await deployCanonical(
      actors.power,
      actors.admin,
      projects[handbookFixture.portfolio_key],
      handbookEvidence.build,
      handbookFixture,
    );
    handbookEvidence.deployment = handbookDeployment;
    handbookEvidence.semantic_vector.live = await handbookDrill(
      actors.power,
      handbookFixture,
      handbookDeployment,
    );
    handbookEvidence.semantic_vector_sha256 = sha256Text(stableJson(handbookEvidence.semantic_vector));
    const handbookFinal = await teardown(
      actors.power,
      projects[handbookFixture.portfolio_key],
      "Handbook",
    );
    handbookEvidence.final_deployment_status = handbookFinal?.status ?? null;

    const approvalFixture = fixtures.find((item) => item.portfolio_key === "approval-workflow-copilot");
    const approvalEvidence = evidence[approvalFixture.portfolio_key];
    const approvalDeployment = await deployCanonical(
      actors.power,
      actors.admin,
      projects[approvalFixture.portfolio_key],
      approvalEvidence.build,
      approvalFixture,
    );
    approvalEvidence.deployment = approvalDeployment;
    approvalEvidence.semantic_vector.live = await approvalDrill(
      actors.power,
      approvalFixture,
      projects[approvalFixture.portfolio_key],
      approvalEvidence.build,
      approvalDeployment,
    );
    approvalEvidence.semantic_vector_sha256 = sha256Text(stableJson(approvalEvidence.semantic_vector));
    const approvalFinal = await teardown(
      actors.power,
      projects[approvalFixture.portfolio_key],
      "Approval",
    );
    approvalEvidence.final_deployment_status = approvalFinal?.status ?? null;

    const sentimentFixture = fixtures.find((item) => item.portfolio_key === "sentiment-notes-agent");
    const sentimentEvidence = evidence[sentimentFixture.portfolio_key];
    const sentimentDeployment = await deployCanonical(
      actors.power,
      actors.admin,
      projects[sentimentFixture.portfolio_key],
      sentimentEvidence.build,
      sentimentFixture,
    );
    sentimentEvidence.deployment = sentimentDeployment;
    const sentimentLive = await sentimentDrill(actors.power, sentimentFixture, sentimentDeployment);
    sentimentEvidence.screenshot = sentimentLive.screenshot;
    delete sentimentLive.screenshot;
    sentimentEvidence.semantic_vector.live = sentimentLive;
    sentimentEvidence.semantic_vector_sha256 = sha256Text(stableJson(sentimentEvidence.semantic_vector));
    sentimentEvidence.final_deployment_status = KEEP_SENTIMENT_ACTIVE ? "active" : null;
    if (!KEEP_SENTIMENT_ACTIVE) {
      const sentimentFinal = await teardown(
        actors.power,
        projects[sentimentFixture.portfolio_key],
        "Sentiment",
      );
      sentimentEvidence.final_deployment_status = sentimentFinal?.status ?? null;
    }

    const projectIds = [
      ...new Set([
        ...Object.values(projects).map((project) => project.id),
        ...projectMutations.retired.map((retirement) => retirement.project.id),
      ]),
    ];
    await cleanupReviewResidue(actors.power, actors.admin, projectIds);
    await verifyZeroReviewResidue(actors.power, projectIds);
    const lifecycle = await verifyDeploymentConvergence(actors.power, projects, evidence);
    for (const [key, status] of Object.entries(lifecycle)) {
      evidence[key].final_deployment_status = status;
    }

    uploaded = await uploadEvidence(credentials, fixtures, evidence);

    // Complete every product cleanup and lifecycle verdict before the first
    // catalog PUT. Only catalog readback remains after the commit boundary.
    await verifyZeroReviewResidue(actors.power, projectIds);
    const finalLifecycle = await verifyDeploymentConvergence(
      actors.power,
      projects,
      evidence,
    );
    await verifyRetiredProjectsQuiescent(actors.power, projectMutations.retired);
    catalogMutation = { baselines: [], rolledBack: false, finalized: false };
    await updateCatalogAfterEvidence(
      actors.admin,
      fixtures,
      catalogBaselines,
      uploaded,
      catalogMutation,
    );
    boundCatalog = await verifyExactThreeCatalog(actors.admin, fixtures, uploaded);
    successReport = {
      result: "portfolio_rehearsal_verified",
      run_id: uploaded.run_id,
      manifest: {
        bucket: uploaded.bucket,
        key: uploaded.manifest.key,
        sha256: uploaded.manifest.sha256,
      },
      admin_mfa: adminMfa,
      review_residue: { submitted: 0, draft: 0 },
      published_catalog_rows: 3,
      bound_catalog_rows: boundCatalog,
      catalog_updated: true,
      sentiment_status: finalLifecycle["sentiment-notes-agent"],
      handbook_status: finalLifecycle["employee-handbook-qa"],
      approval_status: finalLifecycle["approval-workflow-copilot"],
      only_sentiment_optionally_active: true,
      api_keys_logged_or_stored: false,
    };
    workflowSucceeded = true;
  } catch (error) {
    workflowError = error;
  }

  const productCleanupErrors = [];
  const invalidatedCatalogKeys = new Set();
  if (actors && !workflowSucceeded) {
    const cleanupTargets = new Map();
    for (const fixture of fixtures.filter((item) => projects[item.portfolio_key])) {
      const project = projects[fixture.portfolio_key];
      cleanupTargets.set(project.id, {
        fixture,
        project,
        canonical: true,
        portfolioKey: fixture.portfolio_key,
      });
    }
    for (const retirement of projectMutations.retired) {
      if (!cleanupTargets.has(retirement.project.id)) {
        cleanupTargets.set(retirement.project.id, {
          fixture: retirement.fixture,
          project: retirement.project,
          canonical: false,
          portfolioKey: retirement.fixture.portfolio_key,
        });
      }
    }
    for (const creation of projectMutations.created) {
      if (!cleanupTargets.has(creation.project.id)) {
        cleanupTargets.set(creation.project.id, {
          fixture: creation.fixture,
          project: creation.project,
          canonical: false,
          portfolioKey: creation.fixture.portfolio_key,
        });
      }
    }

    for (const target of cleanupTargets.values()) {
      const { fixture, project, canonical, portfolioKey } = target;
      try {
        await cleanupReviewResidue(actors.power, actors.admin, project.id);
      } catch (error) {
        productCleanupErrors.push(
          new Error(`${fixture.title}: final review cleanup failed: ${errorSummary(error)}`),
        );
      }
      try {
        await cancelActiveBuildsForCleanup(
          actors.power,
          project,
          `${fixture.title} failure cleanup`,
        );
      } catch (error) {
        productCleanupErrors.push(
          new Error(`${fixture.title}: build cleanup failed: ${errorSummary(error)}`),
        );
      }
      if (canonical) {
        try {
          await ensureCanonicalSpecs(actors.power, project, fixture);
        } catch (error) {
          productCleanupErrors.push(
            new Error(`${fixture.title}: canonical spec cleanup failed: ${errorSummary(error)}`),
          );
        }
      }

      let preserveBaselineActive = false;
      const baseline = canonical ? deploymentBaselines[portfolioKey] : null;
      const catalogClaimsActive = Boolean(
        canonical &&
          catalogBaselines?.[portfolioKey]?.assets?.demo_url &&
          catalogBaselines?.[portfolioKey]?.metadata_extra?.verification?.deployment_status ===
            "active",
      );
      if (baseline?.active) {
        try {
          const history = await exhaustiveDeploymentHistoryWithRetries(
            actors.power,
            project.id,
            `${fixture.title} failure baseline`,
          );
          preserveBaselineActive = baselineActiveIsIntact(history, baseline);
          if (preserveBaselineActive) {
            console.log(
              `PASS  ${fixture.title}: preserved the pre-run active deployment during failure cleanup`,
            );
          } else {
            invalidatedCatalogKeys.add(portfolioKey);
          }
        } catch (error) {
          preserveBaselineActive = true;
          invalidatedCatalogKeys.add(portfolioKey);
          productCleanupErrors.push(
            new Error(
              `${fixture.title}: could not prove whether the pre-run active deployment survived: ` +
                errorSummary(error),
            ),
          );
        }
      } else if (catalogClaimsActive && !baseline) {
        // Baseline capture failed, so tearing down could falsify the existing
        // catalog row. Preserve infrastructure and fail closed instead.
        preserveBaselineActive = true;
        invalidatedCatalogKeys.add(portfolioKey);
        productCleanupErrors.push(
          new Error(`${fixture.title}: active catalog posture has no safe deployment baseline`),
        );
      } else if (catalogClaimsActive) {
        // The catalog was already claiming ACTIVE while the exhaustive baseline
        // had no active row. Cleanup may proceed, but the stale claim must not survive.
        invalidatedCatalogKeys.add(portfolioKey);
      }

      if (!preserveBaselineActive) {
        try {
          const final = await teardown(
            actors.power,
            project,
            `${fixture.title} failure cleanup`,
          );
          if (final && !TEARDOWN_COMPLETE_STATES.has(final.status)) {
            throw new Error(`cleanup ended in ${final.status}`);
          }
        } catch (error) {
          productCleanupErrors.push(
            new Error(`${fixture.title}: deployment cleanup failed: ${errorSummary(error)}`),
          );
        }
      }
    }

    for (const retirement of projectMutations.retired) {
      try {
        await convergeRetirementForCleanup(actors.power, retirement);
      } catch (error) {
        productCleanupErrors.push(
          new Error(
            `${retirement.fixture.title}: retirement cleanup failed: ${errorSummary(error)}`,
          ),
        );
      }
    }
    for (const creation of projectMutations.created.filter((item) => !item.stable)) {
      try {
        await retireUncommittedCreatedProject(actors.power, creation);
      } catch (error) {
        productCleanupErrors.push(
          new Error(
            `${creation.fixture.title}: uncommitted project cleanup failed: ${errorSummary(error)}`,
          ),
        );
      }
    }

    // Any failed workflow or product cleanup invalidates catalog verification.
    // Keep the admin context open until compensating rollback has converged.
    if (catalogMutation && !catalogMutation.rolledBack) {
      try {
        await rollbackCatalogUpdates(actors.admin, catalogMutation);
      } catch (error) {
        productCleanupErrors.push(error);
      }
    }
    for (const key of invalidatedCatalogKeys) {
      const baselineRow = catalogBaselines?.[key];
      if (!baselineRow) {
        productCleanupErrors.push(
          new Error(`${key}: missing catalog baseline for required active-posture invalidation`),
        );
        continue;
      }
      try {
        await invalidateCatalogDeployment(
          actors.admin,
          baselineRow,
          "Pre-run active deployment was changed or removed by a failed rehearsal.",
        );
      } catch (error) {
        productCleanupErrors.push(error);
      }
    }
  }

  if (!workflowError && productCleanupErrors.length === 0) {
    assert(workflowSucceeded && successReport && catalogMutation, "final success state is complete");
    catalogMutation.finalized = true;
  }

  const shutdownWarnings = [];
  if (actors) {
    for (const actor of [actors.business, actors.admin, actors.power]) {
      try {
        await actor.ctx.close();
      } catch (error) {
        shutdownWarnings.push(
          `${actor.email}: browser context close warning: ${safeError(errorSummary(error))}`,
        );
      }
    }
  }
  try {
    await browser.close();
  } catch (error) {
    shutdownWarnings.push(`browser close warning: ${safeError(errorSummary(error))}`);
  }
  for (const warning of shutdownWarnings) console.warn(`WARN  ${warning}`);

  if (workflowError || productCleanupErrors.length) {
    throw new AggregateError(
      [workflowError, ...productCleanupErrors].filter(Boolean),
      "portfolio rehearsal or required product cleanup failed",
    );
  }
  console.log(JSON.stringify(successReport, null, 2));
}

main().catch((error) => {
  console.error(`PORTFOLIO REHEARSAL FAILED: ${safeError(errorSummary(error))}`);
  process.exitCode = 1;
});
