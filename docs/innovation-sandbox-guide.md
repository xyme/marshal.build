# Using AWS Innovation Sandbox with marshal (the `isb` tier)

In the default `direct` tier, marshal deploys generated agents as
CloudFormation stacks into the installation account. In the `isb` tier,
marshal asks [Innovation Sandbox on AWS](https://aws.amazon.com/solutions/implementations/innovation-sandbox-on-aws/)
(ISB) for a **lease** on an isolated AWS account from a pool, deploys the agent
there, and ends the lease at teardown so ISB recycles the account. The account
boundary is what marshal calls an **Enclave**: a deployment's blast radius is
one disposable account with its own budget and expiry.

This guide is generic. Replace every `<placeholder>` with your own values and
keep your filled-in copy out of the repository.

**Honest effort estimate:** 0.5–1 day of hands-on work plus AWS account
creation lead time, for someone who already administers an AWS Organization.
ISB control infrastructure costs roughly $10–30/month on top of the marshal
control plane; each leased account adds its own spend under the lease budget.

## 1. What you need before starting

| Prerequisite | Why | Notes |
| --- | --- | --- |
| An **AWS Organization** (FeatureSet `ALL`) and access to its **management account** `<management-account-id>` | ISB deploys into the management account (or a delegated administrator) and creates OUs and SCPs | Organizations → Settings → confirm "All features" |
| **Service Control Policies** enabled | ISB attaches SCPs to its OUs | Organizations → Policies |
| **IAM Identity Center** enabled in `us-east-1` | Required by ISB for user/sandbox access; a one-way, region-pinned switch | Decide this with whoever owns identity in your organization |
| **3 or more member accounts** for the pool, each with a unique e-mail | Leases need Available accounts; marshal keeps a capacity buffer | Account closure later takes ~90 days in suspended state and counts against quotas — choose an e-mail alias scheme deliberately (`aws-sandbox-01@<your-domain>`, …) |
| **CDK bootstrap** in the management account | ISB stacks are CDK apps | `npx cdk bootstrap aws://<management-account-id>/us-east-1` |
| The marshal **control plane deployed in the same account as the ISB hub** | The backend reads ISB's JWT secret from Secrets Manager and initiates `sts:AssumeRole` into leased accounts from outside the sandbox OUs (SCPs do not apply there) | The reference installation runs both in the management account |

Things to decide before you start, because they are hard to reverse:
Identity Center enablement, which accounts join the pool (ISB runs **AWS
Nuke** in pooled accounts — a wrongly registered account is wiped), and the
e-mail/billing scheme for pool accounts.

## 2. Deploy Innovation Sandbox

Follow the solution's implementation guide; the outline is:

1. **Enable IAM Identity Center** (console, management account, `us-east-1`).
2. **Create the pool accounts** (Organizations → Add account, 3+). Record
   the account ids.
3. **Deploy the ISB stacks** from the solution repository
   (`github.com/aws-solutions/innovation-sandbox-on-aws`) with a namespace —
   this guide assumes **`marshal`**. In the July 2026 release there are four
   stacks: AccountPool, IDC, Data and Compute. The Data stack is DynamoDB +
   AppConfig (no Timestream dependency).
4. **Register the pool accounts** through the ISB web UI or API
   (`POST /accounts {awsAccountId}`). ISB moves them into its OU structure
   and runs an initial cleanup. If registration quarantines an account
   because cleanup raced the SandboxAccount StackSet rollout, wait for the
   instances to be `CURRENT` and call `POST /accounts/{id}/retryCleanup`.
5. Note the outputs you will need:
   - the ISB REST API base URL: `https://<isb-api-id>.execute-api.us-east-1.amazonaws.com/prod` → `<isb-api-url>`
   - the ISB web UI CloudFront host `<isb-ui-host>` (for humans)
   - the AppConfig application/environment/profile ids (global configuration)

### The STS note

ISB's `InnovationSandboxAwsNukeSupportedServicesScp` allowlists the services
AWS Nuke can clean up. marshal does **not** need `sts:*` added to it: the
`AssumeRole` call is initiated from the hub account, where SCPs do not apply,
and the in-account services marshal deploys with (`cloudformation`, `iam`,
`lambda`, `apigateway`, `logs`, `s3`, `dynamodb`) are all allowlisted. Verify
this against the SCP of the release you deploy; if a newer release narrows the
list, the deployment role in §3 is where you will see `AccessDenied`.

### Global configuration

In the AppConfig global configuration, set `leases.maxBudget` and
`leases.maxDurationHours` **at or above** the values marshal will request for
its lease template (defaults below: $1000 and 720 hours). `maintenanceMode`
must be off. Before humans use the ISB web UI, create the Identity Center
custom SAML application per the ISB post-deployment guide and replace the
`auth.idp*` placeholders; the machine-to-machine API marshal uses works
without that step.

## 3. Register the marshal baseline blueprint

Every leased account needs the cross-account deployment role marshal assumes.
The template is in this repository:
[`infra/blueprints/marshal-baseline.yaml`](../infra/blueprints/marshal-baseline.yaml).
It creates `AIFactoryDeploymentRole` (trusting the control-plane account,
scoped to the services generated stacks use) and the `/marshal-ai/app` log
group. It takes one parameter, **`ControlPlaneAccountId`**, with no default.

1. Create a CloudFormation **StackSet** from the template in the management
   account, permission model **SELF_MANAGED**, administration role
   `InnovationSandbox-marshal-IntermediateRole` and execution role
   `InnovationSandbox-marshal-SandboxAccountRole` (the roles ISB created for
   your namespace), parameter `ControlPlaneAccountId=<control-plane-account-id>`.
   Do not add stack instances yourself; ISB instantiates the StackSet into
   each account at lease time.
2. Register it as an ISB **blueprint**: `GET /blueprints/stacksets` lists
   registrable StackSets; `POST /blueprints {name, stackSetId, regions:
   ["us-east-1"], …}` returns the blueprint id → `<blueprint-id>`.
3. On later template updates, pass `UsePreviousValue=true` for
   `ControlPlaneAccountId` when updating the StackSet.

## 4. The lease template

marshal uses one ISB lease template named **`marshal-default`**
(`isb_lease_template_name` in `backend/app/core/config.py`). You do not have
to create it: on the first lease the backend looks it up and, if absent,
creates it with

- `maxSpend` = `ISB_LEASE_BUDGET_USD` (default **1000**),
- `leaseDurationInHours` = `ISB_LEASE_DURATION_HOURS` (default **720**, 30 days),
- auto-approval, and the blueprint from `ISB_BLUEPRINT_ID` attached.

These are the **lease** bounds (the account's ceiling). Per-deployment TTL and
budget are enforced by marshal's own deployment policies (Admin → Model
Controls → Deployment policies; defaults 72 h / $200) and are typically much
tighter. If you want different lease bounds, set the two environment
variables in `infra/lib/app-stack.ts` before the template is first created,
or edit the template in ISB afterwards.

## 5. How the backend authenticates to ISB

ISB's REST API is protected by a Lambda authorizer that validates **HS256
session JWTs** signed with the shared secret ISB stores in Secrets Manager at
`/InnovationSandbox/<namespace>/Auth/JwtSecret`. marshal mints a short-lived
(15 min) service token per call window with a payload of the form
`{"user": {"email", "roles": ["Admin"]}}` — the same mechanism the ISB web UI
uses after Identity Center sign-in.

The backend expects the secret at **`/InnovationSandbox/marshal/Auth/JwtSecret`**
(`isb_jwt_secret_name`); if you deployed ISB under another namespace, add
`ISB_JWT_SECRET_NAME` to the backend task environment in
`infra/lib/app-stack.ts`. `MarshalAppStack` already grants the
backend task role `secretsmanager:GetSecretValue` on
`/InnovationSandbox/*` and `kms:Decrypt` via Secrets Manager for the CMK that
encrypts it — which is why the control plane and the ISB hub live in the same
account.

Lease lifecycle as the backend drives it: `POST /leases
{leaseTemplateUuid}` → poll `GET /leases/{uuid}` until `Active` (ISB
provisions the account and deploys the blueprint StackSet during
`Provisioning`; marshal waits up to `ISB_LEASE_ACTIVATION_TIMEOUT_S`, default
900 s) → assume `AIFactoryDeploymentRole` in the leased account → deploy →
on teardown `POST /leases/{uuid}/terminate`, after which ISB cleans and
recycles the account. Admission control also reads `GET /accounts` and keeps
`provider_capacity_buffer` accounts (default 1) in `Available` state; with a
pool of three that means at most two concurrent Enclaves.

## 6. Switch marshal to `isb`

Two places, because the deploy scripts and the backend read different files:

```bash
# scripts/deploy-env.local.sh  (gitignored overlay; see scripts/deploy-env.local.sh.example)
export SANDBOX_PROVIDER="isb"

# backend/.env  (gitignored; scripts/deploy-env.sh reads these two values from here)
SANDBOX_PROVIDER=isb
ISB_API_BASE_URL=https://<isb-api-id>.execute-api.us-east-1.amazonaws.com/prod
ISB_BLUEPRINT_ID=<blueprint-id>
```

Then run `./scripts/deploy-cloud.sh`. The preflight refuses to continue if
`SANDBOX_PROVIDER=isb` and either value is empty. The app stack passes both
into the backend task environment; `ISB_LEASE_DURATION_HOURS` and
`ISB_LEASE_BUDGET_USD` come from `infra/lib/app-stack.ts`.

Optional follow-ups that apply to any ISB installation:

- **Cost Explorer**: visit it once in the console (enablement takes ~24 h).
  ISB's budget monitoring needs it; lease mechanics work without it. To show
  Enclave spend inside marshal, set `COST_EXPLORER_ENABLED=true` and
  `COST_READER_ROLE_ARN` to a reader role in the management account.
- **SES**: ISB e-mail notifications need a verified identity; production
  access for non-verified recipients is a separate request.
- **Alarms**: subscribe `OPS_ALERT_EMAIL` so you hear about lease failures
  and capacity exhaustion.

## 7. Known ISB behaviors and how marshal handles them

| Behavior | Handling |
| --- | --- |
| Billing data lags 24–48 h | Spend attribution for a torn-down Enclave settles after a cooling-off window |
| AWS Nuke does not remove every resource type (for example Bedrock imported models) | Generated agents only create the resource types the validation gate allows; keep an eye on the Nuke release notes if you widen the allowlist |
| Nuke only cleans regions it manages | marshal pins deployments to `us-east-1`; keep the SCP region lock |
| Quarantined accounts shrink the pool | Keep a buffer (`provider_capacity_buffer`) and subscribe to ISB's quarantine events; `retryCleanup` returns an account to the pool |
| A stale repair command can delete and recreate ISB stacks | Run ISB operations one at a time; registered accounts are unaffected because they live at the organization root between OU moves |
