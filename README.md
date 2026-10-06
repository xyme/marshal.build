# marshal — Build · Govern · Deploy AI agents

marshal turns a plain-English description of an agent into a governed,
deployed AI agent on AWS. You describe what you need in chat; marshal shapes
it into a structured specification (requirements, design, tasks), generates a
serverless agent (Lambda + API Gateway + Amazon Bedrock) from that spec,
validates the generated code against deterministic gates, scores its risk
against an admin-tunable rubric, and deploys it into a governed AWS
environment with a time-to-live, a budget cap and a full audit trail.

- **Build** — chat → spec → agent, with opt-in capabilities declared in the
  spec itself: web test console, conversation memory, registered connectors,
  MCP tool loops, local tools, planning, packaged dependencies, and
  agent-to-agent composition.
- **Govern** — deterministic conformance gates on generated artifacts, risk
  scoring with review workflows, platform policies (concurrency, TTLs,
  budgets, endpoint authentication), cost caps, and an audit trail whose
  coverage is enforced by a test.
- **Deploy** — CloudFormation into your own account (`direct`) or into an
  isolated per-deployment account vended by AWS Innovation Sandbox (`isb`),
  with health probes, post-deploy smoke checks, in-place updates, auto-expiry
  and clean teardown.

**Status:** v0.1.0 — first public source release. The hosted beta at
app.marshal.build is private and invitation-only; this repository is how you
run marshal yourself.

**Edition and license:** marshal is released as one edition under Apache-2.0.
There is no Free/Enterprise split in this release: every capability in the
repository is available to every installation, and nothing is license-gated
or entitlement-checked. A commercially supported edition may be offered in
future; nothing in this repository is restricted today.

## Architecture at a glance

Browser → CloudFront (WAF) → ALB → Next.js frontend (ECS Fargate) → FastAPI
backend (ECS Fargate) → PostgreSQL (RDS), DynamoDB, S3, Amazon Bedrock,
Cognito. Three container images (backend, frontend, codegen runner) are built
by CodeBuild from this repository. Generated agents are deployed as
CloudFormation stacks either into the installation account or into an
account leased from AWS Innovation Sandbox.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) walks every functional area
end to end: request path, service, data written, governance hooks, failure
modes.

| Path | What |
| --- | --- |
| `backend/` | FastAPI control plane (Python 3.12, SQLAlchemy, Alembic) |
| `frontend/` | Next.js app (App Router, Auth.js + Cognito hosted UI); in-app docs under `src/content/docs/` |
| `infra/` | AWS CDK stacks (network, auth, data, build, app) and the Enclave baseline template |
| `runner/` | Codegen/packaging CodeBuild image (the `runner` provider) and the generated-app CDK template |
| `sample-app/` | The bundled demo agent template |
| `adapters/` | Reference MCP adapter (SharePoint) |
| `scripts/` | Deploy, seed, smoke, supply-chain audit and release tooling |
| `docs/` | Architecture, operations, security and design documentation |

## Deploying your own installation

### Prerequisites

- An **AWS account** with administrator credentials in a **named AWS CLI
  profile** (`AWS_PROFILE`; default profile name `default`). The deploy scripts
  materialize credentials with `aws configure export-credentials`;
  environment-variable-only credentials are not supported.
- **AWS CLI v2 ≥ 2.9**.
- **Node.js 22** (`.nvmrc`), **Python 3.12** and [uv](https://docs.astral.sh/uv/).
- `python3`, `openssl`, `curl`, `git` on your PATH.
- Optional: the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
  for `aws ecs execute-command` into the backend task — the only way to call
  the API with a Cognito token (see "Scripting the API without a browser").
- **CDK bootstrap** for the account and region:
  `(cd infra && npm ci && npx cdk bootstrap aws://<your-account-id>/us-east-1)`.
  Without `npm ci` first, `npx cdk` aborts with "npx canceled due to missing
  packages".
- **Region: us-east-1.** It is the only region currently supported (the
  CloudFront-scope WAF and the Bedrock inference profiles pin it); the deploy
  refuses any other region.
- No Docker: images are built in CodeBuild.

### Bedrock model access

marshal calls Amazon Bedrock through `us.` cross-region inference profiles.
The defaults, and the model allowlist seeded on first boot, are:

| Model id | Used for |
| --- | --- |
| `us.anthropic.claude-sonnet-5` | chat, spec generation, design (default) |
| `us.anthropic.claude-haiku-4-5-20251001-v1:0` | fast calls (titles, summaries) |
| `us.anthropic.claude-sonnet-4-6` | selectable in Model Controls |
| `us.anthropic.claude-opus-4-8` | selectable in Model Controls |

Check access before deploying. The availability call takes the **foundation
model id** (the `us.` profile id without its prefix); run it for each model
you intend to use and expect `AVAILABLE` for the agreement and `AUTHORIZED`
for the authorization status:

```bash
aws bedrock get-foundation-model-availability --region us-east-1 \
  --model-id anthropic.claude-sonnet-5
aws bedrock get-foundation-model-availability --region us-east-1 \
  --model-id anthropic.claude-haiku-4-5-20251001-v1:0
```

If a status is anything else, open the Bedrock console in **us-east-1** →
*Model access* → *Modify model access*, enable the Anthropic models, and
complete the one-time use-case form (approval is usually immediate; wait for
"Access granted"). A `us.` profile routes requests to several US regions, so
enable access in each region the profile lists as well. Without access, chat
fails with a model-access error after the retry budget. The cloud task
definition pins the model ids (`BEDROCK_MODEL_*` in `infra/lib/app-stack.ts`);
`backend/.env.example` only covers local development.

### Two install tiers

| Tier | Setting | What it means |
| --- | --- | --- |
| **Evaluate** | `SANDBOX_PROVIDER=direct` (default) | Generated agents deploy as CloudFormation stacks **into the installation account**. One AWS account, nothing else to set up. The backend assumes a dedicated deployment role (`MarshalDirectDeployRole`, created by the app stack) for each deployment; the control plane's own data and roles are denied to that role. |
| **Operate** | `SANDBOX_PROVIDER=isb` | [AWS Innovation Sandbox](https://aws.amazon.com/solutions/implementations/innovation-sandbox-on-aws/) vends an **isolated AWS account per deployment** (an "Enclave") with its own budget and lease, recycled at teardown. Needs an AWS Organization, IAM Identity Center and the Innovation Sandbox solution — about **0.5–1 day** of setup plus account-creation lead time. See [docs/innovation-sandbox-guide.md](docs/innovation-sandbox-guide.md). |

Pooled Enclaves (isolated per-deployment AWS accounts) require an AWS
Innovation Sandbox installation and `SANDBOX_PROVIDER=isb`; the default
`direct` mode deploys into the installation's own account. In direct mode
the deployment role can create IAM roles and the allow-listed services inside
the installation account; run evaluations in a dedicated account, or use
Enclave (`isb`) mode for per-deployment isolation. The deployment role may
also create the API Gateway service-linked role
(`AWSServiceRoleForAPIGateway`) on first use, so the first agent deploy in a
brand-new account needs no manual preparation. Likewise, e-mail
notifications (SES), Enclave spend surfacing (Cost Explorer), Slack/Teams
chat-ops, custom OpenAI-compatible model endpoints and Entra/Okta federation
are all built and ship in this repository; each needs configuration in your
account before it does anything.

### Install

```bash
git clone https://github.com/xyme/marshal.build.git && cd marshal.build
cp scripts/deploy-env.local.sh.example scripts/deploy-env.local.sh
$EDITOR scripts/deploy-env.local.sh          # AWS_PROFILE, MARSHAL_AWS_ACCOUNT_ID, OPS_ALERT_EMAIL, …
(cd infra && npm ci)
./scripts/deploy-cloud.sh --bootstrap
```

`scripts/deploy-env.local.sh` is gitignored; the example file lists every
variable the overlay supports with placeholder values, and the header of
`scripts/deploy-env.sh` lists every key it can pin. The first run takes
**about 40 minutes** (measured 41 on a fresh account, 19 of them RDS
creation) and performs seven steps:
deploy the foundation stacks (network, auth, data, build), write the frontend
auth secret, build the three images in **CodeBuild** (no local Docker),
deploy the app stack (ECS + ALB + CloudFront), register the callback URLs on
the Cognito client, roll the services onto the new images, and finish with a
**fail-closed health smoke**: the landing page, then
`GET <CloudFront URL>/healthz`, which must return
`{"status":"ok","checks":{"database":"ok"}}`. The health path is `/healthz`;
`/api/healthz` is a Next.js 404 page. `--bootstrap` is the **first-run flag**:
it also generates the local secrets (`.auth-secret`, `.origin-verify`),
creates the frontend auth secret and defers the live-identity cross-check
that cannot exist yet. Subsequent releases are plain
`./scripts/deploy-cloud.sh`.

Keep `.auth-secret` and `.origin-verify`. They are the installation's secrets
(the Auth.js session key and the CloudFront → ALB origin-verify header), they
are gitignored, and every checkout that deploys needs the same pair in the
repo root: back them up next to the overlay and copy them into any new clone
before running `./scripts/deploy-cloud.sh`, which otherwise stops at
"`.origin-verify` is missing or empty; refusing a live deploy" and then
"`.auth-secret` is missing/empty; refusing to rotate the live Auth.js secret".
If they are lost, run `./scripts/deploy-cloud.sh --bootstrap` again: it
regenerates both, which rotates the values and signs every user out.

### First admin

Self-signup is off: an administrator provisions every user. The platform role
is the first match of `admin` > `power` > `business` over the user's Cognito
groups; no group means `business`. Create the first administrator with the
CLI (the pool id comes from the auth stack's outputs):

```bash
POOL_ID=$(aws cloudformation describe-stacks --region us-east-1 --stack-name MarshalAuthStack \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
ADMIN_EMAIL=admin@example.com

aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username "$ADMIN_EMAIL" \
  --user-attributes Name=email,Value="$ADMIN_EMAIL" Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" --username "$ADMIN_EMAIL" \
  --password '<at least 12 chars, upper + lower + digit>' --permanent
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" --username "$ADMIN_EMAIL" \
  --group-name admin
```

Sign in at the CloudFront URL printed by the deploy. The platform user record
is created on the first authenticated request (the first admin still walks
through `/onboarding` once). Add further users the same way with
`--group-name power` or `business`; their role and persona can then be
changed under **Admin → Users**. `scripts/seed_users.py` creates the demo
roster if you want one.

### First run

- **Deployment admissions stay paused** on a fresh cloud installation until an
  admin saves the deployment policy once: open **Admin → Model Controls**
  (`/admin/models`) and click **Save changes**, even with nothing changed.
  Until then every deploy is refused with `Deployment admissions are paused by
  platform policy or because the cloud policy snapshot is unavailable`
  (code `admissions_paused`); saving writes the complete policy snapshot with
  admissions open. Review the defaults while you are there (TTL 72 h default /
  168 h max, $200 per deployment, 3 concurrent per user, endpoint
  authentication `default`). This applies to both providers: in `direct` mode
  the concurrency limits on that page are the deployment capacity; in `isb`
  mode the Innovation Sandbox account pool is checked as well. The same
  unlock as an API request: `GET /api/v1/admin/model-controls`, then `PUT
  /api/v1/admin/model-controls` with the returned `model_allowlist`,
  `param_bounds`, `rate_limits`, `cost` and `codegen` plus
  `"deployment_policies": {"admissions_paused": false}` (the server fills the
  other policy keys with their defaults); the response's
  `deployment_policies.admissions_paused` is `false` once the snapshot is
  stored.
- **Supply-chain gate.** Every image build runs
  `AUDIT_STRICT=1 scripts/supply-chain-audit.sh` first; a HIGH/CRITICAL
  advisory published since the last triage fails the build. If step 3/7 fails
  with audit findings: run `bash scripts/supply-chain-audit.sh /tmp/sbom`
  locally, upgrade the dependency where a fix exists (`uv lock` /
  `npm install`), or add the advisory to `scripts/audit-allowlist.txt` with a
  rationale, then re-run the deploy. See CONTRIBUTING.md for the triage rules.
- **Code generation profiles.** `inline-cfn` (default) and `packaged-cfn`
  (chosen when a spec says "SHALL use packaged dependencies", or automatically
  when the generated handlers exceed the 4 KB inline ceiling) work with the
  default in-process `internal` provider; packaging runs in CodeBuild using
  the workspace bucket the deploy created. The `cdk-app` profile is
  experimental and requires switching the codegen provider to `runner`. On a
  cloud installation the backend task's `CODEGEN_PROVIDER` pin is
  authoritative (Admin → Model Controls cannot override it), so set
  `CODEGEN_PROVIDER=runner` in `scripts/deploy-env.local.sh` and redeploy
  `MarshalAppStack` with `./scripts/deploy-cloud.sh`; `kiro` is accepted as a
  legacy alias for `runner`.
- **Generation timeouts.** A single stalled Bedrock call can take the full
  180 s read timeout; the spec-generation budget is 420 s so one stall
  survives. If a document is missing afterwards, regenerate only that
  document.

### Scripting the API without a browser

The public path `https://<your-cloudfront-domain>/api/backend/v1/...` accepts
two credentials only: the browser session cookie, or a service-account token
(`Authorization: Bearer mat_...`, minted under **Admin → Integrations →
Service accounts**). Service accounts hold the `power` or `business` role,
never `admin`, and cannot own projects, so they cover automation on projects
shared with them but not admin actions such as the first-run unlock above. A
Cognito access token is refused on that path.

To script the API as a Cognito user (including an administrator), call the
backend directly from inside its container: the load balancer forwards only
CloudFront's requests, and only to the frontend and `/healthz`; the backend
listens on `localhost:8000` inside the cluster. `USER_PASSWORD_AUTH` is
enabled on the web app client, and `scripts/get_token.py` performs that flow
for any user in the pool (it reads the client secret with
`cognito-idp:DescribeUserPoolClient` to compute the `SECRET_HASH`, so run it
with your deploying credentials):

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name MarshalAuthStack \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name MarshalAuthStack \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" --output text)
TOKEN=$(cd backend && COGNITO_USER_POOL_ID="$POOL_ID" COGNITO_CLIENT_ID="$CLIENT_ID" \
  AWS_REGION=us-east-1 DEMO_USER_PASSWORD='<the password of that user>' \
  uv run python ../scripts/get_token.py admin@example.com)      # valid for 1 hour

TASK=$(aws ecs list-tasks --cluster marshal --service-name marshal-backend \
  --desired-status RUNNING --query 'taskArns[0]' --output text)
SNIPPET=$(printf '%s' "import json,urllib.request
r=urllib.request.urlopen(urllib.request.Request('http://localhost:8000/api/v1/users/me',
  headers={'Authorization':'Bearer $TOKEN'}))
print(json.load(r)['role'])" | base64 | tr -d '\n')
aws ecs execute-command --cluster marshal --task "$TASK" --container backend --interactive \
  --command "python -c 'import base64,sys;exec(base64.b64decode(sys.argv[1]).decode())' $SNIPPET"
```

The backend image has Python but no `curl`; embedding the snippet as base64
keeps quoting intact and the token off the shell history. The token still
appears in the `ExecuteCommand` record in CloudTrail, so use a dedicated
account for scripted sessions and disable it afterwards
(`aws cognito-idp admin-disable-user`). The first call as a new user creates
the platform user record, exactly as a browser sign-in would.

### Acceptance smoke

`scripts/ui-smoke.mjs` is the authenticated browser acceptance test of the
reference installation, and what step 7/7 of the deploy points at. On a new
installation it needs the repo-root dependencies, the seeded demo users and
one template first:

```bash
npm ci && npx playwright install chromium     # repo root: playwright + axe
cd backend && COGNITO_USER_POOL_ID=<pool id> AWS_REGION=us-east-1 \
  DEMO_USER_PASSWORD='<12+ chars>' uv run python ../scripts/seed_users.py
```

Sign in once as each demo user (`power`, `admin`, `business@marshal.demo`)
and finish the onboarding screens, then create one template under **Admin →
Templates**. Run it against your URL; `SMOKE_ADMIN_EMAIL` replaces the
TOTP-enrolled `smoke-admin@marshal.demo` that only the reference installation
has:

```bash
APP_URL=https://<your-cloudfront-domain> SMOKE_ADMIN_EMAIL=admin@marshal.demo \
  DEMO_USER_PASSWORD='<the seeded password>' node scripts/ui-smoke.mjs
```

### What it costs to leave running

Estimated idle floor in us-east-1, on-demand pricing, before any Bedrock usage
and before Innovation Sandbox accounts, for the default `production` sizing
tier:

| Component | Why | ≈ $/month |
| --- | --- | --- |
| NAT gateway ×1 (+ 3 public IPv4 addresses with the ALB) | private subnets for ECS/RDS | 33 + 11 |
| Application Load Balancer | origin for CloudFront | 16–20 |
| ECS Fargate, 4 tasks minimum (backend ×2, frontend ×2; 0.5 vCPU / 1 GiB each) | autoscaling floor; up to 8 tasks under load (≈ 144) | 72 |
| RDS `db.t4g.micro` Multi-AZ, 2 × 20 GiB gp3, 7-day backups | system of record with PITR | 28 |
| WAF web ACL + 4 rules | edge protection | 9–10 |
| CloudWatch (Container Insights ≈ 18, 9 alarms, metric filters, logs) | operations | ≈ 21 |
| CloudFront, Secrets Manager, DynamoDB on-demand + PITR, ECR | — | 1–4 |
| CodeBuild | per deploy, not per month | 0.15–0.30 per deploy |
| **Total idle** | measured on a fresh-account install (`≈ $6.3/day`) | **≈ 190–195** |

Bedrock spend is additional and governed by the platform cost caps (Admin →
Model Controls → Cost). Each Innovation Sandbox account adds its own spend
under the lease budget.

**`MARSHAL_TIER`** (overlay key, default `production`) is the sizing knob.
`MARSHAL_TIER=evaluate` deploys a single-AZ RDS instance, a floor of one task
per service (`desiredCount` 1, autoscaling `minCapacity` 1; the ceilings and
the NAT count stay) and turns Container Insights off, which lowers the idle
floor to **≈ $120–125/month**; `production` is today's exact sizing at
**≈ $190–195/month**. Nothing else moves with the tier. Switching an existing
installation re-sizes the RDS instance and the service floors on the next
deploy. The Multi-AZ change is applied in place: `cdk diff` labels the
instance "may be replaced" because `MultiAZ` is a conditional-replacement
property, but the instance keeps its identifier and data (measured on a live
install: standby removed in 3 minutes, status `available` throughout, no
failover, no connection errors). What does interrupt is the service floor:
with one task per service and `minHealthyPercent` 0, ECS stops the old
backend tasks before the new one is registered, so the API is unreachable for
2–3 minutes during the switch (and for under a minute on each later deploy
at the evaluate tier). Verify a tier after the deploy with
`aws rds describe-db-instances --query 'DBInstances[].MultiAZ'`,
`aws ecs describe-services --cluster marshal --services marshal-backend marshal-frontend --query 'services[].desiredCount'`,
`aws application-autoscaling describe-scalable-targets --service-namespace ecs --query 'ScalableTargets[].MinCapacity'`
and `aws ecs describe-clusters --clusters marshal --include SETTINGS --query 'clusters[0].settings'`;
the tier is a synth-time value and is not an environment variable of the
backend task. The same value is accepted as CDK context:
`npx cdk synth -c marshalTier=evaluate`.

### Custom domain (optional)

Set `APP_DOMAIN` and `APP_CERT_ARN` (an ACM certificate in us-east-1 in your
account) in the overlay to serve the app on your own domain; otherwise it runs
on the CloudFront default domain, including the Cognito callback URLs. The
Cognito hosted-UI prefix defaults to `marshal-ai-<account-id>` and can be set
with `COGNITO_DOMAIN_PREFIX`.

## Local development

```bash
docker compose up -d postgres
cd backend && cp .env.example .env   # fill Cognito values after first deploy
uv sync --all-groups && uv run alembic upgrade head && uv run uvicorn app.main:app --reload
cd frontend && cp .env.example .env.local && npm ci && npm run dev
```

`./scripts/sync-env.sh` fills both env files from your deployed stacks.
The test suite needs no AWS credentials and no Postgres:
`cd backend && uv run pytest -q`. `scripts/ci-local.sh` runs every CI job
locally. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full developer loop.

## Documentation

- In-app docs (served under `/docs` in the product; source in
  `frontend/src/content/docs/`): quickstart, chat and specs, code generation,
  deployment, governance, admin, troubleshooting, release notes.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — engineering walkthrough of
  every functional area.
- [ROADMAP.md](ROADMAP.md) — what is planned and what triggers it.
- [CHANGELOG.md](CHANGELOG.md) — release history.
- [docs/ENGINEERING_LOG.md](docs/ENGINEERING_LOG.md) — excerpt of the
  delivery log (recent fixes, root causes, evidence).
- [docs/innovation-sandbox-guide.md](docs/innovation-sandbox-guide.md) —
  the `isb` tier.
- [docs/security.md](docs/security.md) — threat model and control map;
  [docs/reliability.md](docs/reliability.md), [docs/runbook.md](docs/runbook.md),
  [docs/sso.md](docs/sso.md), [docs/accessibility.md](docs/accessibility.md).
- [docs/codegen-workspace-contract.md](docs/codegen-workspace-contract.md) —
  the S3/CodeBuild contract any external codegen engine can implement.
- [SECURITY.md](SECURITY.md) — reporting vulnerabilities.
- [CONTRIBUTING.md](CONTRIBUTING.md) — developer setup, checks, CLA.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
Copyright 2026 marshal.build.
