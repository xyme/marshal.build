# Contributing to marshal

Thanks for considering a contribution. This document covers the local setup,
the checks a change must pass, the conventions the codebase follows, and the
Contributor License Agreement (CLA) every contributor signs once.

If you found a security issue, do not open an issue or pull request; follow
[SECURITY.md](SECURITY.md).

## Prerequisites

| Tool | Version | Needed for |
| --- | --- | --- |
| Node.js | **22** (`.nvmrc`; `nvm use`) | frontend, infra, sample-app, scripts |
| Python | **3.12** with [uv](https://docs.astral.sh/uv/) | backend |
| AWS CLI | v2 **≥ 2.9** (for `aws configure export-credentials`) | only for deploying an installation |
| Docker | not required | images are built in CodeBuild; the test suite runs on SQLite. Local development uses `docker compose` for a Postgres 16 container, or point `DATABASE_URL` at any Postgres you already have |

Also useful: `python3`, `openssl`, `curl`, `git` (the deploy scripts call
them), and Playwright's Chromium for the browser smoke
(`npx playwright install chromium`).

## Local development

Mirrors the README's "Local development" section.

```bash
docker compose up -d postgres                      # local Postgres 16 (or bring your own)
cd backend && cp .env.example .env                 # Cognito values can stay empty for tests
uv sync --all-groups && uv run alembic upgrade head
uv run uvicorn app.main:app --reload               # http://localhost:8000
cd ../frontend && cp .env.example .env.local && npm ci && npm run dev   # http://localhost:3000
```

Signing in locally needs a Cognito user pool, so the frontend is normally
developed against a deployed installation: `./scripts/sync-env.sh` fills both
env files from your deployed stacks (see the README for deploying one). The
backend test suite needs neither Postgres nor AWS credentials.

## Checks every change must pass

`scripts/ci-local.sh` runs the same jobs as `.github/workflows/ci.yml`. The
individual commands:

```bash
# backend — lint + tests (SQLite-backed; no AWS credentials needed)
cd backend && uv run ruff check app tests && uv run pytest -q

# frontend — lint, strict typecheck, production build
cd frontend && npm ci && npm run lint && npx tsc --noEmit && npm run build

# infra — strict typecheck + account-agnostic synth
cd infra && npm ci && npx tsc --noEmit && npx cdk synth --quiet

# sample-app — if you touched it, regenerate and commit template.json
cd sample-app && npm ci && npm run synth
```

The frontend build needs placeholder env values when run outside `next dev`;
copy them from the `frontend` job in `.github/workflows/ci.yml`.

### Browser smoke (needs a deployed installation)

`scripts/ui-smoke.mjs` signs in through the real Cognito hosted UI and walks
the product with Playwright. It is intentionally not part of CI. Run it after
deploying anything that touches the frontend, the `/api/backend` proxy, or
networking:

```bash
npm ci                                             # repo root: playwright + axe
npx playwright install chromium                    # once per machine
APP_URL=https://<your-cloudfront-or-domain> SMOKE_EMAIL=<a power user> \
DEMO_USER_PASSWORD='…' node scripts/ui-smoke.mjs
```

Admin-context checks sign in as a dedicated smoke admin whose TOTP seed the
script reads from `SMOKE_ADMIN_TOTP_SECRET`; see the header of the script.

### Supply-chain gate

Every image build runs `AUDIT_STRICT=1 bash scripts/supply-chain-audit.sh`
in CodeBuild before anything is built: `pip-audit` over the locked backend
set and `npm audit --omit=dev` over the frontend's runtime dependencies. A
new HIGH or CRITICAL advisory fails the build. To triage a finding:

1. Reproduce locally: `bash scripts/supply-chain-audit.sh /tmp/sbom`.
2. Prefer upgrading: bump the dependency in `backend/pyproject.toml` +
   `uv lock`, or in the relevant `package.json` + `npm install`, and re-run.
3. Only if no fix exists, add the advisory id to `scripts/audit-allowlist.txt`
   **with a rationale comment** that names the exposure you assessed (is the
   vulnerable code on a request path? at build time only?) and the condition
   that should re-open it. Entries without a rationale will not be accepted.

## Conventions

- **Python**: `ruff` (configuration in `backend/pyproject.toml`, line length
  100, `E F I W UP B`). Type hints on public functions. Settings come from
  `app/core/config.py`, never from `os.environ` reads scattered in services.
- **TypeScript**: `strict` mode in `frontend/` and `infra/`; ESLint config in
  `frontend/`. No `any` unless the surrounding code already uses it.
- **Tests go into the existing suite file for the area** (for example,
  deployment behavior into `backend/tests/test_deployment_service.py`, admin
  routes into `test_projects_admin.py`). Do not create a new test file unless
  you are adding a genuinely new component. Every mutating API route must be
  registered for audit; `tests/test_audit.py` fails the build otherwise.
- **Governance seams are not bypassed**: model calls go through
  `services/bedrock.py`, access decisions through `collab.resolve_role`,
  audit rows through `services/audit.py`. See
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §0 for the design principles.
- **Infra**: `cdk synth` must stay account-agnostic (no `fromLookup`, no
  hard-coded identifiers). Installation-specific values come from the
  overlay (`scripts/deploy-env.local.sh`) or stack outputs.
- **No identifiers in the tree**: no AWS account ids (use `123456789012`),
  Cognito pool/client ids, hostnames, certificate ARNs, e-mail addresses or
  phone numbers in code, fixtures or docs. `scripts/export-public-snapshot.sh
  --dry-run` runs the same identifier gate the release uses.
- **Commits**: imperative subject, conventional prefix (`feat:`, `fix:`,
  `docs:`, `refactor:`, `chore:`), body explains why.

## Contributor License Agreement

marshal.build uses a CLA rather than a DCO because a commercially supported
edition may be offered in future; the CLA keeps relicensing possible while
leaving you full rights to your own work. Read [CLA.md](CLA.md) before your
first pull request.

How signing works:

1. You open a pull request. The CLA bot (`.github/workflows/cla.yml`)
   comments if your GitHub account has not signed.
2. You reply to the pull request with the exact sentence the bot asks for:
   `I have read the CLA Document and I hereby sign the CLA`.
3. The bot records your account, the date and the CLA version in
   `.github/cla-signatures.json` on the `cla-signatures` branch, marks the
   check green, and never asks you again. Comment `recheck` to re-run it.

Contributions made on behalf of an employer need a corporate CLA; request it
at security@marshal.build (see the last section of `CLA.md`).

## Pull requests

- One change per pull request; keep refactors separate from behavior changes.
- Fill in the template: what and why, tests appended, docs updated, CLA
  signed, no secrets or identifiers.
- CI must be green (`frontend`, `backend`, `infra`, `secret-scan`, `CLA`).
- Expect a review within a few days. Review feedback is about the change,
  not the person; the [Code of Conduct](CODE_OF_CONDUCT.md) applies.

## Welcome follow-up contributions

- **SPDX license headers.** Source files do not yet carry
  `SPDX-License-Identifier: Apache-2.0` headers (the license is declared in
  `LICENSE` and every manifest). Adding them consistently across
  `backend/app`, `frontend/src`, `infra/lib`, `runner/` and `scripts/` is a
  welcome, self-contained first contribution — do it in one pull request
  that changes nothing else.
- Items in [ROADMAP.md](ROADMAP.md) marked as evidence-triggered need an
  issue with the evidence before code.

## Maintainer notes

One-time setup after creating the public repository:

```bash
# the CLA bot stores signatures on an unprotected branch of this repository
git checkout --orphan cla-signatures
git rm -rf . && git commit --allow-empty -m "chore: CLA signature store"
git push origin cla-signatures
git checkout main
```

Do not enable branch protection on `cla-signatures`; the bot commits to it
with the workflow's `GITHUB_TOKEN`.
