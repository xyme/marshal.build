import * as cdk from "aws-cdk-lib";
import * as appscaling from "aws-cdk-lib/aws-applicationautoscaling";
import * as bedrock from "aws-cdk-lib/aws-bedrock";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cwactions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as logs from "aws-cdk-lib/aws-logs";
import * as rds from "aws-cdk-lib/aws-rds";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as sns from "aws-cdk-lib/aws-sns";
import * as subscriptions from "aws-cdk-lib/aws-sns-subscriptions";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";
import type { MarshalTier } from "./tier";

export interface MarshalAppStackProps extends cdk.StackProps {
  /**
   * Sizing tier (G18); default production. evaluate = one-task floors
   * (desiredCount 1, autoscaling minCapacity 1; ceilings unchanged) and
   * Container Insights off. Nothing else moves with it.
   */
  tier?: MarshalTier;
  vpc: ec2.IVpc;
  db: rds.DatabaseInstance;
  dbSecurityGroup: ec2.SecurityGroup;
  chatTable: dynamodb.Table;
  runtimeStateTable: dynamodb.Table;
  backendRepo: ecr.IRepository;
  frontendRepo: ecr.IRepository;
}

/**
 * Cloud runtime (FSD §6.2 as-built):
 *
 *   Internet ──HTTPS──▶ CloudFront ──HTTP(+verify header)──▶ ALB (public subnets)
 *                                                             ├── /healthz ─▶ backend:8000 (ops only)
 *                                                             └── default  ─▶ frontend:3000 (Next.js)
 *   frontend ──Service Connect (marshal-backend:8000, private)──▶ backend (FastAPI)
 *   backend ──▶ RDS (private) · DynamoDB · Bedrock · ISB API · STS AssumeRole (sandboxes)
 *
 * The backend is never internet-reachable except the /healthz path; all app API
 * traffic flows through the Next.js server-side proxy over the private namespace.
 * Note: SSE through CloudFront is capped by the 60s origin response timeout —
 * the UI degrades to its 5s polling fallback for long deploy-log streams.
 */
export class MarshalAppStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MarshalAppStackProps) {
    super(scope, id, props);

    const region = cdk.Stack.of(this).region;

    // Runtime configuration provided by scripts/deploy-cloud.sh (falls back to
    // synth-safe placeholders so CI `cdk synth` needs no live account context)
    const cognitoUserPoolId = process.env.COGNITO_USER_POOL_ID ?? "us-east-1_PLACEHOLDER";
    const cognitoClientId = process.env.COGNITO_CLIENT_ID ?? "placeholder";
    const cognitoDomain = process.env.COGNITO_DOMAIN ?? "https://placeholder.auth.us-east-1.amazoncognito.com";
    const isbApiBaseUrl = process.env.ISB_API_BASE_URL ?? "";
    const isbBlueprintId = process.env.ISB_BLUEPRINT_ID ?? "";
    const originVerifyToken = process.env.ORIGIN_VERIFY_TOKEN ?? "synth-placeholder-token";
    const cognitoIssuer = `https://cognito-idp.${region}.amazonaws.com/${cognitoUserPoolId}`;

    // B22 P0 (FSD §13.5T): domain + certificate are INPUTS (the overlay pins
    // them for this installation). Without a cert the distribution serves on
    // its default *.cloudfront.net name — the fresh-account mode; ACM certs
    // are account-bound, so a baked-in ARN can never work anywhere else.
    const appDomain = process.env.APP_DOMAIN || "";
    const appCertArn = process.env.APP_CERT_ARN || "";
    const hasCustomDomain = Boolean(appDomain && appCertArn);

    // Codegen provider pin (overlay key CODEGEN_PROVIDER via deploy-env.sh).
    // In the cloud the backend treats this env as authoritative (a DB setting
    // cannot select the runner), so the cdk-app profile needs `runner` here
    // and a MarshalAppStack redeploy. `kiro` is the backend's legacy alias
    // for `runner`; anything else is a typo and must fail at synth, not at
    // the first build.
    const codegenProviderRaw = process.env.CODEGEN_PROVIDER ?? "";
    const codegenProvider = codegenProviderRaw.trim().toLowerCase() || "internal";
    if (!["internal", "runner", "kiro"].includes(codegenProvider)) {
      throw new Error(
        `CODEGEN_PROVIDER must be one of internal, runner (or the legacy alias kiro) (got "${codegenProviderRaw}")`
      );
    }

    // G18 sizing tier. Production keeps the S12/S13 posture (two-task floors,
    // Container Insights); evaluate idles on one task per service. S12 relay/
    // election tolerate N=1, so the floor is a cost knob, not a correctness one.
    const evaluateTier = (props.tier ?? "production") === "evaluate";
    const serviceFloor = evaluateTier ? 1 : 2;

    // Frontend server-side secrets (AUTH_SECRET + Cognito client secret) are
    // written by the deploy script before this stack deploys.
    const frontendAuthSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "FrontendAuthSecret",
      "marshal/frontend/auth"
    );

    const cluster = new ecs.Cluster(this, "Cluster", {
      vpc: props.vpc,
      clusterName: "marshal",
      containerInsightsV2: evaluateTier
        ? ecs.ContainerInsights.DISABLED
        : ecs.ContainerInsights.ENABLED,
      defaultCloudMapNamespace: { name: "marshal.local", useForServiceConnect: true },
    });

    const logGroup = new logs.LogGroup(this, "AppLogs", {
      logGroupName: "/marshal-ai/control-plane",
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ---------------------------------------------------------------- backend
    const backendTask = new ecs.FargateTaskDefinition(this, "BackendTask", {
      cpu: 512,
      memoryLimitMiB: 1024,
    });

    // Scoped task role (FSD §5.2): only what the control plane actually calls
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "BedrockRuntimeAndCatalog",
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:ListFoundationModels",
          "bedrock:ListInferenceProfiles",
          "bedrock:GetInferenceProfile",
        ],
        resources: ["*"],
      })
    );
    props.chatTable.grantReadWriteData(backendTask.taskRole);
    props.runtimeStateTable.grantReadWriteData(backendTask.taskRole); // S12 shared counters
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "IsbJwtSecret",
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${region}:${this.account}:secret:/InnovationSandbox/*`,
        ],
      })
    );
    // S17 custom model endpoints: API keys live at marshal/models/<slug>.
    // Full lifecycle scoped to that prefix ONLY (create-or-put on save,
    // read at call time; admins never read keys back through the API).
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "CustomModelEndpointKeys",
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${region}:${this.account}:secret:marshal/models/*`,
        ],
      })
    );
    // C1 connector consumption (COPY custody, owner decision 7 Sep 2026):
    // in DIRECT provider mode the deployer provisions per-deployment copies
    // at marshal/agent-connectors/<slug> in THIS account. Distinct prefix —
    // the registry's own custody above must never be overwritten by copies.
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "AgentConnectorCopies",
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:DeleteSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${region}:${this.account}:secret:marshal/agent-connectors/*`,
          // Composable agents R2.2: dependency endpoint+key copies (same
          // custody pattern, same lifecycle)
          `arn:aws:secretsmanager:${region}:${this.account}:secret:marshal/agent-dependencies/*`,
        ],
      })
    );
    // C0 connector registry (external-import-connectors spec): connector
    // credentials live at marshal/connectors/<slug> — same custody pattern
    // (create-or-put on save, read at probe time, delete with the entry;
    // admins never read credentials back through the API).
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "ConnectorRegistryCredentials",
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
          "secretsmanager:DeleteSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${region}:${this.account}:secret:marshal/connectors/*`,
        ],
      })
    );
    // B10/B11 (integration-wave): webhook HMAC secrets + the chat-ops URL —
    // same custody pattern (create-or-put on save, read at dispatch time,
    // never read back through the API).
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "IntegrationSecrets",
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${region}:${this.account}:secret:marshal/webhooks/*`,
          `arn:aws:secretsmanager:${region}:${this.account}:secret:marshal/chatops/*`,
        ],
      })
    );
    // Demo reset schedules deletion only for exact endpoint-derived webhook
    // secrets, with a seven-day recovery window. Keep delete authority off the
    // broader chat-ops integration prefix.
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "WebhookSecretCleanup",
        actions: [
          "secretsmanager:DeleteSecret",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:${region}:${this.account}:secret:marshal/webhooks/*`,
        ],
      })
    );
    // The ISB JwtSecret is CMK-encrypted (its rotator re-encrypted it after S4 —
    // live S8 drill finding: GetSecretValue → "Access to KMS is not allowed").
    // Decrypt is allowed ONLY via Secrets Manager, not directly.
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "IsbJwtSecretKmsViaSecretsManager",
        actions: ["kms:Decrypt"],
        resources: ["*"],
        conditions: {
          StringEquals: { "kms:ViaService": `secretsmanager.${region}.amazonaws.com` },
        },
      })
    );
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "AssumeSandboxDeploymentRole",
        actions: ["sts:AssumeRole"],
        resources: ["arn:aws:iam::*:role/AIFactoryDeploymentRole"],
      })
    );
    // Direct mode (SANDBOX_PROVIDER=direct, the generic default): agent stacks
    // deploy into THIS account, so the deployment permissions the Enclave
    // blueprint grants AIFactoryDeploymentRole inside a pooled account must
    // exist here too. They live on a dedicated role — NOT on the task role —
    // that only the backend task role may assume, one STS session per
    // deployment (RoleSessionName marshal-deploy-<deployment id>, CloudTrail
    // attribution). Policy mirrors infra/blueprints/marshal-baseline.yaml
    // statement for statement, with two differences because this account is
    // the control plane rather than a disposable pooled account:
    //   1. cloudformation:* is scoped to the stacks the deployer creates
    //      (backend _stack_name: "<DEPLOYMENT_STACK_PREFIX>-<12 hex>") and its
    //      change sets ("marshal-preview-<8 hex>"). Every CloudFormation call
    //      the deployer makes passes StackName (create/update/delete_stack,
    //      describe_stacks, describe_stack_events, create/describe/
    //      delete_change_set), so no action needs Resource "*".
    //   2. Explicit Deny on the platform's own data (ProtectControlPlaneData
    //      below), so a deploy session can never reach it even through the
    //      broad service wildcards, and on the platform's own IAM roles
    //      (ProtectControlPlaneRoles), so a generated template cannot attach
    //      policies to, re-trust or delete this role or the task roles.
    // The IAM statement stays blueprint-equivalent (no PermissionsBoundary /
    // path conditions): generated templates set neither, so that tightening
    // needs deployer-side injection or a codegen change first — deferred.
    const deploymentStackPrefix = "marshal"; // pinned into the backend env below
    const directDeployRole = new iam.Role(this, "DirectDeployRole", {
      roleName: "MarshalDirectDeployRole",
      description:
        "Assumed by the marshal backend task role to deploy agent stacks into this account (direct mode)",
      assumedBy: new iam.ArnPrincipal(backendTask.taskRole.roleArn).withSessionTags(),
      maxSessionDuration: cdk.Duration.hours(1),
    });
    directDeployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudFormationDeployerStacks",
        actions: ["cloudformation:*"],
        resources: [
          `arn:aws:cloudformation:*:${this.account}:stack/${deploymentStackPrefix}-*/*`,
          `arn:aws:cloudformation:*:${this.account}:changeSet/marshal-preview-*/*`,
        ],
      })
    );
    directDeployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AppServices",
        actions: [
          "lambda:*",
          "apigateway:*",
          "logs:*",
          "s3:*",
          "dynamodb:*",
          "events:*",
          "sqs:*",
          "sns:*",
          "states:*",
        ],
        resources: ["*"],
      })
    );
    // Deny beats Allow: the control plane's own data stays out of reach of a
    // deploy session regardless of the service wildcards above. The deploy
    // path never needs any of these — asset bodies are read with the task
    // role's own S3 client and staged into marshal-assets-<account>.
    directDeployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ProtectControlPlaneData",
        effect: iam.Effect.DENY,
        actions: ["s3:*", "dynamodb:*", "secretsmanager:*", "rds:*", "cloudformation:*"],
        resources: [
          // codegen workspace / artifact bucket (BuildStack)
          `arn:aws:s3:::marshal-codegen-workspace-${this.account}`,
          `arn:aws:s3:::marshal-codegen-workspace-${this.account}/*`,
          // chat + shared runtime-state tables (DataStack), incl. indexes/streams
          props.chatTable.tableArn,
          `${props.chatTable.tableArn}/*`,
          props.runtimeStateTable.tableArn,
          `${props.runtimeStateTable.tableArn}/*`,
          // Postgres credential + instance (DataStack), frontend auth secret,
          // connector-registry custody (marshal/connectors/*)
          props.db.secret!.secretArn,
          props.db.instanceArn,
          `arn:aws:secretsmanager:${region}:${this.account}:secret:marshal/frontend/auth-*`,
          `arn:aws:secretsmanager:${region}:${this.account}:secret:marshal/connectors/*`,
          // marshal's own stacks (Marshal*Stack). Cannot collide with the
          // deployer's "marshal-<12 hex>" names: hex never spells "Stack".
          `arn:aws:cloudformation:*:${this.account}:stack/Marshal*Stack/*`,
        ],
      })
    );
    // The platform's own ROLES, not just its data: IamForAppRoles below grants
    // PutRolePolicy/AttachRolePolicy/DeleteRole/PassRole on "*" so generated
    // templates can create and wire their own roles, which would otherwise
    // also let a template attach a policy to this role (fixed, guessable
    // name) or to the task roles. Every role the Marshal*Stack stacks create
    // is CDK-named "<StackName>-<LogicalId>-<random>" (BackendTask task +
    // execution roles, the frontend pair, BuildStack's ImageBuild and
    // CodegenRunner roles), so one pattern covers them; the deploy role names
    // itself. Agent-stack roles are "marshal-<12 hex>-…" (lowercase; ARN
    // matching is case-sensitive) and stay creatable/passable. The deploy
    // path never touches a platform role, so nothing on it changes.
    directDeployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ProtectControlPlaneRoles",
        effect: iam.Effect.DENY,
        actions: ["iam:*"],
        resources: [
          directDeployRole.roleArn,
          backendTask.taskRole.roleArn,
          `arn:aws:iam::${this.account}:role/Marshal*Stack-*`,
        ],
      })
    );
    directDeployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IamForAppRoles",
        actions: [
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:GetRole",
          "iam:PassRole",
          "iam:TagRole",
          "iam:UntagRole",
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:GetRolePolicy",
          "iam:AttachRolePolicy",
          "iam:DetachRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
        ],
        resources: ["*"],
      })
    );
    // G27: the first API Gateway API ever created in an account makes API
    // Gateway create its service-linked role under the caller's session; a
    // fresh account's first agent deploy therefore fails CreateRestApi
    // unless the deploy session may create that one SLR. Scoped to the exact
    // role ARN plus the AWSServiceName condition — nothing else. SLRs carry
    // their AWS-managed policy themselves, so no Attach/PutRolePolicy is
    // needed. Outside ProtectControlPlaneRoles: that Deny matches this role,
    // the task role and role/Marshal*Stack-*, never role/aws-service-role/*.
    directDeployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ApiGatewayServiceLinkedRole",
        actions: ["iam:CreateServiceLinkedRole"],
        resources: [
          `arn:aws:iam::${this.account}:role/aws-service-role/ops.apigateway.amazonaws.com/AWSServiceRoleForAPIGateway`,
        ],
        conditions: {
          StringEquals: { "iam:AWSServiceName": "ops.apigateway.amazonaws.com" },
        },
      })
    );
    // C1 connector copies + composable-agent dependency copies: the deploy
    // session writes them before create_stack and deletes them at teardown
    // (same two prefixes the blueprint scopes to).
    directDeployRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AgentConnectorCopies",
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:DeleteSecret",
          "secretsmanager:DescribeSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:*:${this.account}:secret:marshal/agent-connectors/*`,
          `arn:aws:secretsmanager:*:${this.account}:secret:marshal/agent-dependencies/*`,
        ],
      })
    );
    cdk.Tags.of(directDeployRole).add("marshal-ai:managed", "true");
    cdk.Tags.of(directDeployRole).add("marshal-ai:role", "deployment");
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "AssumeDirectDeployRole",
        actions: ["sts:AssumeRole", "sts:TagSession"],
        resources: [directDeployRole.roleArn],
      })
    );
    // B20 R2: testbed credential vending — the backend mints time-boxed user
    // sessions on the role it bootstraps into Testbed-mode leased accounts.
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "AssumeTestbedUserRole",
        actions: ["sts:AssumeRole"],
        resources: ["arn:aws:iam::*:role/MarshalTestbedUserRole"],
      })
    );
    // Admin user management (S3-08): role-group mirroring + suspend/enable
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "CognitoAdminUserMgmt",
        actions: [
          "cognito-idp:AdminAddUserToGroup",
          "cognito-idp:AdminRemoveUserFromGroup",
          "cognito-idp:AdminDisableUser",
          "cognito-idp:AdminEnableUser",
        ],
        resources: [
          `arn:aws:cognito-idp:${region}:${this.account}:userpool/${cognitoUserPoolId}`,
        ],
      })
    );
    // Notification emails (S5): dark until EMAIL_ENABLED=true after SES
    // production access + marshal.build DKIM (owner action; docs runbook)
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "SesSendNotifications",
        actions: ["ses:SendEmail"],
        resources: [`arn:aws:ses:${region}:${this.account}:identity/*`],
      })
    );
    // S9 external codegen: dispatch/ingest on the workspace bucket + runner control.
    // The RUNNER's own role (BuildStack) is scoped per-build-prefix; the backend
    // legitimately touches every build's prefix.
    const workspaceBucketArn = `arn:aws:s3:::marshal-codegen-workspace-${this.account}`;
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "CodegenWorkspaceObjects",
        actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        resources: [
          `${workspaceBucketArn}/builds/*`,
          `${workspaceBucketArn}/artifacts/*`, // S10 durable artifact store
        ],
      })
    );
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "CodegenWorkspaceList",
        actions: ["s3:ListBucket"],
        resources: [workspaceBucketArn],
        conditions: { StringLike: { "s3:prefix": ["builds/*", "artifacts/*"] } },
      })
    );
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "CodegenWorkspaceSecurityRead",
        actions: ["s3:GetBucketPublicAccessBlock"],
        resources: [workspaceBucketArn],
      })
    );
    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "CodegenRunnerControl",
        actions: [
          "codebuild:StartBuild",
          "codebuild:BatchGetBuilds",
          "codebuild:StopBuild",
          "codebuild:BatchGetProjects",
        ],
        resources: [
          `arn:aws:codebuild:${region}:${this.account}:project/marshal-codegen-runner`,
        ],
      })
    );

    const backendContainer = backendTask.addContainer("backend", {
      image: ecs.ContainerImage.fromEcrRepository(props.backendRepo, "latest"),
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "backend" }),
      portMappings: [{ name: "marshal-backend", containerPort: 8000 }],
      environment: {
        AWS_REGION: region,
        ENVIRONMENT: "cloud",
        COGNITO_USER_POOL_ID: cognitoUserPoolId,
        COGNITO_CLIENT_ID: cognitoClientId,
        DYNAMO_TABLE_CHAT: props.chatTable.tableName,
        RUNTIME_STATE_TABLE: props.runtimeStateTable.tableName, // S12 shared counters
        BEDROCK_MODEL_CHAT: "us.anthropic.claude-sonnet-5",
        BEDROCK_MODEL_SPEC: "us.anthropic.claude-sonnet-5",
        BEDROCK_MODEL_DESIGN: "us.anthropic.claude-sonnet-5",
        BEDROCK_MODEL_FAST: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        SANDBOX_PROVIDER: process.env.SANDBOX_PROVIDER ?? "direct", // overlay pins isb here
        DIRECT_DEPLOY_ROLE_ARN: directDeployRole.roleArn, // direct-mode deploy session
        // Same value the backend defaults to; pinned so the deploy role's
        // CloudFormation scope (stack/<prefix>-*) can never drift from it.
        DEPLOYMENT_STACK_PREFIX: deploymentStackPrefix,
        ISB_API_BASE_URL: isbApiBaseUrl,
        ISB_BLUEPRINT_ID: isbBlueprintId,
        ISB_LEASE_DURATION_HOURS: "720",
        ISB_LEASE_BUDGET_USD: "1000",
        EMAIL_ENABLED: "false", // flip after SES production access approval + DKIM verify (S5 runbook; request submitted 27 Jul)
        // Sender identity for the (gated) e-mail channel — an installation
        // input (overlay); the backend default is a neutral localhost sender.
        ...(process.env.EMAIL_FROM ? { EMAIL_FROM: process.env.EMAIL_FROM } : {}),
        // S13/B22 P0: Cost Explorer wiring is an INPUT — the overlay pins the
        // owner's management-account reader role; fresh accounts run without.
        COST_EXPLORER_ENABLED: process.env.COST_EXPLORER_ENABLED ?? "false",
        COST_READER_ROLE_ARN: process.env.COST_READER_ROLE_ARN ?? "",
        // Private-beta mandatory posture: settings failures block privileged/model
        // work, and new cloud builds stay internal unless the overlay pins the
        // provider. The workspace runner (`runner`, the workspace-runner
        // integration point — a real external engine can replace the image; see
        // docs/codegen-workspace-contract.md) remains provisioned as a capability
        // but cannot be selected by a DB setting — only by this deploy-time pin.
        SECURITY_FAIL_CLOSED: "true",
        CODEGEN_PROVIDER: codegenProvider, // overlay: internal (default) | runner
        CODEGEN_WORKSPACE_BUCKET: `marshal-codegen-workspace-${this.account}`,
        CODEGEN_RUNNER_PROJECT: "marshal-codegen-runner",
        DB_HOST: props.db.dbInstanceEndpointAddress,
        DB_NAME: "marshal",
      },
      secrets: {
        DB_SECRET_JSON: ecs.Secret.fromSecretsManager(props.db.secret!),
      },
      healthCheck: {
        command: ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')\" || exit 1"],
        interval: cdk.Duration.seconds(30),
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    const backendSg = new ec2.SecurityGroup(this, "BackendSg", {
      vpc: props.vpc,
      // As-deployed wording retained deliberately — GroupDescription is
      // immutable, so a reword replaces the SG and tears down the Service
      // Connect ingress rule the frontend depends on (see data-stack.ts).
      description: "Marshal backend service",
    });
    // Declared HERE (not on the DataStack SG object) to keep the cross-stack
    // dependency one-directional: AppStack -> DataStack.
    new ec2.CfnSecurityGroupIngress(this, "DbIngressFromBackend", {
      groupId: props.dbSecurityGroup.securityGroupId,
      sourceSecurityGroupId: backendSg.securityGroupId,
      ipProtocol: "tcp",
      fromPort: 5432,
      toPort: 5432,
      description: "Backend to Postgres",
    });

    const backendService = new ecs.FargateService(this, "BackendService", {
      cluster,
      serviceName: "marshal-backend",
      taskDefinition: backendTask,
      enableExecuteCommand: true, // ops/smoke access to the private backend (ECS exec)
      // S12: shared state (DDB counters), event relay (LISTEN/NOTIFY) and
      // advisory-lock scheduling make N tasks safe. Bumped 1→2 after the
      // two-task drill validated cross-task SSE + single-fire ticks.
      // S13-04: autoscaling below manages the count from here (floor 2;
      // 1 on the evaluate tier).
      desiredCount: serviceFloor,
      minHealthyPercent: 0,
      maxHealthyPercent: 200,
      securityGroups: [backendSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      serviceConnectConfiguration: {
        services: [{ portMappingName: "marshal-backend", dnsName: "marshal-backend", port: 8000 }],
        logDriver: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "sc-backend" }),
      },
    });

    // Service Connect idle timeout (L1 override — not yet surfaced by the L2
    // construct): envoy must drop idle upstream connections BEFORE uvicorn does
    // (uvicorn --timeout-keep-alive 300 > 240s), else pooled requests can hit
    // dead sockets and surface as ECONNRESET/500 at the frontend proxy.
    // NOTE: no appProtocol is set on the port mapping, so envoy proxies at L4
    // (plain TCP). Only IdleTimeoutSeconds is valid there —
    // PerRequestTimeoutSeconds is L7-only and gets rejected by ECS
    // ("Per request timeout can't be set for tcp application"). L4 passthrough
    // is also the safer mode for SSE streaming.
    const backendCfnService = backendService.node.defaultChild as ecs.CfnService;
    backendCfnService.addPropertyOverride(
      "ServiceConnectConfiguration.Services.0.Timeout",
      { IdleTimeoutSeconds: 240 }
    );

    // S13-04: target-tracking autoscaling. Floor 2 (relay/election make N
    // tasks safe per S12; 1 on the evaluate tier); CPU target sized off the
    // S13 load baseline.
    const backendScaling = backendService.autoScaleTaskCount({
      minCapacity: serviceFloor,
      maxCapacity: 4,
    });
    backendScaling.scaleOnCpuUtilization("BackendCpuScaling", {
      targetUtilizationPercent: 60,
      scaleInCooldown: cdk.Duration.minutes(5),
      // S13-04: 60s out-cooldown. The 5x baseline held 84% CPU for ~3 min and
      // the run ended before target-tracking's alarm evaluation completed —
      // faster reaction, and the scale-out drill runs long enough to prove it.
      scaleOutCooldown: cdk.Duration.seconds(60),
    });


    // ---------------------------------------------------------------- frontend
    const frontendTask = new ecs.FargateTaskDefinition(this, "FrontendTask", {
      cpu: 512,
      memoryLimitMiB: 1024,
    });

    const frontendContainer = frontendTask.addContainer("frontend", {
      image: ecs.ContainerImage.fromEcrRepository(props.frontendRepo, "latest"),
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "frontend" }),
      portMappings: [{ name: "marshal-frontend", containerPort: 3000 }],
      environment: {
        BACKEND_URL: "http://marshal-backend:8000",
        AUTH_COGNITO_ISSUER: cognitoIssuer,
        AUTH_COGNITO_ID: cognitoClientId,
        COGNITO_DOMAIN: cognitoDomain,
        AUTH_TRUST_HOST: "true",
        // S13-02: Node's default 5s keep-alive is SHORTER than the ALB's 60s
        // idle timeout, so the ALB reuses sockets the target is closing →
        // sporadic ELB-generated 502s with no target error (3 in 17k requests
        // during the 5x load run; TargetConnectionErrorCount stayed 0).
        // Next's standalone server reads this env var (start-server.js).
        // Must exceed the ALB idle timeout below.
        KEEP_ALIVE_TIMEOUT: "65000",
      },
      secrets: {
        AUTH_SECRET: ecs.Secret.fromSecretsManager(frontendAuthSecret, "AUTH_SECRET"),
        AUTH_COGNITO_SECRET: ecs.Secret.fromSecretsManager(frontendAuthSecret, "AUTH_COGNITO_SECRET"),
      },
    });

    const frontendSg = new ec2.SecurityGroup(this, "FrontendSg", {
      vpc: props.vpc,
      // As-deployed wording retained deliberately (immutable GroupDescription).
      description: "Marshal frontend service",
    });

    // Service Connect data path: the frontend task's envoy dials the backend
    // task ENI directly on 8000 (it does NOT go through the ALB), so the
    // backend SG must admit the frontend SG. Without this rule every
    // /api/backend/* proxy call dies as a silent connect timeout while
    // ALB-path health checks keep passing.
    backendSg.addIngressRule(
      frontendSg,
      ec2.Port.tcp(8000),
      "Frontend proxy (Service Connect envoy) to backend"
    );

    const frontendService = new ecs.FargateService(this, "FrontendService", {
      cluster,
      serviceName: "marshal-frontend",
      taskDefinition: frontendTask,
      desiredCount: 1,
      minHealthyPercent: 0,
      maxHealthyPercent: 200,
      securityGroups: [frontendSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      serviceConnectConfiguration: {
        logDriver: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "sc-frontend" }),
      },
    });

    // S13-04: frontend scales with the backend (SSR proxy is the user path).
    // desiredCount above is already 1 in both tiers; the floor is the knob.
    const frontendScaling = frontendService.autoScaleTaskCount({
      minCapacity: serviceFloor,
      maxCapacity: 4,
    });
    frontendScaling.scaleOnCpuUtilization("FrontendCpuScaling", {
      targetUtilizationPercent: 60,
      scaleInCooldown: cdk.Duration.minutes(5),
      scaleOutCooldown: cdk.Duration.minutes(2),
    });

    // ---------------------------------------------------------------- ALB
    const alb = new elbv2.ApplicationLoadBalancer(this, "Alb", {
      vpc: props.vpc,
      internetFacing: true,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      // S13-02: pinned so the invariant is explicit — target keep-alive
      // (KEEP_ALIVE_TIMEOUT=65s on the frontend) MUST exceed this, or the ALB
      // reuses sockets the target has closed and emits 502s.
      idleTimeout: cdk.Duration.seconds(60),
    });

    // S13-04: request-count as the SECOND scaling signal — I/O-bound
    // saturation (DB waits, Bedrock streams) shows up as latency well before
    // CPU moves, so CPU alone can miss a real overload.
    backendScaling.scaleOnMetric("BackendRequestScaling", {
      metric: alb.metrics.requestCount({ period: cdk.Duration.minutes(1) }),
      scalingSteps: [
        { upper: 2000, change: 0 },
        { lower: 2000, change: +1 },
        { lower: 5000, change: +2 },
      ],
      adjustmentType: appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
      cooldown: cdk.Duration.minutes(1),
    });

    const listener = alb.addListener("Http", {
      port: 80,
      // Requests lacking CloudFront's verify header get a 403 (origin lockdown)
      defaultAction: elbv2.ListenerAction.fixedResponse(403, {
        contentType: "text/plain",
        messageBody: "Forbidden",
      }),
    });

    const frontendTargets = listener.addTargets("Frontend", {
      priority: 10,
      conditions: [
        elbv2.ListenerCondition.httpHeader("x-origin-verify", [originVerifyToken]),
      ],
      port: 3000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [frontendService],
      healthCheck: {
        path: "/",
        healthyHttpCodes: "200-399",
        interval: cdk.Duration.seconds(30),
      },
      deregistrationDelay: cdk.Duration.seconds(15),
    });

    const backendHealthTargets = listener.addTargets("BackendHealth", {
      priority: 5,
      conditions: [
        elbv2.ListenerCondition.pathPatterns(["/healthz"]),
        elbv2.ListenerCondition.httpHeader("x-origin-verify", [originVerifyToken]),
      ],
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [backendService],
      healthCheck: { path: "/healthz", interval: cdk.Duration.seconds(30) },
      deregistrationDelay: cdk.Duration.seconds(15),
    });

    // ---------------------------------------------------------------- CloudFront
    const origin = new origins.LoadBalancerV2Origin(alb, {
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      readTimeout: cdk.Duration.seconds(60),
      customHeaders: { "x-origin-verify": originVerifyToken },
    });

    // ---- S14-04: PII redaction guardrail.
    // Entity choice is deliberately NARROW. Masking names/emails/addresses
    // would wreck legitimate spec text ("send an email to the approver",
    // "store the customer's address"), train users to fight the tool, and buy
    // little: the platform is internal and specs are tenant-scoped. What is
    // masked instead are values that must NEVER enter a model conversation or
    // a stored spec — card numbers, bank details, government IDs, secrets.
    const piiGuardrail = new bedrock.CfnGuardrail(this, "PiiGuardrail", {
      name: "marshal-pii-redaction",
      description: "S14-04: masks high-risk PII/secrets in prompts and completions",
      blockedInputMessaging:
        "That request contained sensitive data (card, bank, government ID or credential) and was blocked. Remove it and try again.",
      blockedOutputsMessaging:
        "The response was withheld because it contained sensitive data.",
      sensitiveInformationPolicyConfig: {
        piiEntitiesConfig: [
          "CREDIT_DEBIT_CARD_NUMBER",
          "CREDIT_DEBIT_CARD_CVV",
          "CREDIT_DEBIT_CARD_EXPIRY",
          "PIN",
          "US_BANK_ACCOUNT_NUMBER",
          "US_BANK_ROUTING_NUMBER",
          "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
          "SWIFT_CODE",
          "US_SOCIAL_SECURITY_NUMBER",
          "US_PASSPORT_NUMBER",
          "DRIVER_ID",
          "PASSWORD",
          "AWS_ACCESS_KEY",
          "AWS_SECRET_KEY",
        ].map((type) => ({ type, action: "ANONYMIZE" })),
      },
    });
    const guardrailVersion = new bedrock.CfnGuardrailVersion(this, "PiiGuardrailVersion", {
      guardrailIdentifier: piiGuardrail.attrGuardrailId,
      description: "S14-04 initial version",
    });

    backendTask.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "ApplyPiiGuardrail",
        actions: ["bedrock:ApplyGuardrail"],
        resources: [piiGuardrail.attrGuardrailArn],
      })
    );
    // Wired but INERT until Admin → Model Controls → Security turns
    // pii_redaction on (§7.0 practice #5: owner-visible switch, no release).
    backendContainer.addEnvironment("BEDROCK_GUARDRAIL_ID", piiGuardrail.attrGuardrailId);
    backendContainer.addEnvironment("BEDROCK_GUARDRAIL_VERSION", guardrailVersion.attrVersion);

    // ---- S14-01: WAF (CLOUDFRONT scope must be created in us-east-1; this
    // stack already is). Managed core rules + a rate-based rule tuned ABOVE the
    // S13 load numbers (5x sustained ≈ 59 rps ≈ 3.5k/5min per fleet; the rule
    // counts per source IP over a 5-minute window, so 3000 leaves real users
    // ample room while stopping single-source floods).
    const webAcl = new wafv2.CfnWebACL(this, "AppWebAcl", {
      name: "marshal-app-waf",
      scope: "CLOUDFRONT",
      defaultAction: { allow: {} },
      // WAF descriptions reject parentheses — keep this plain
      description: "marshal edge protections - S14-01",
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: "marshal-app-waf",
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: "RateLimitPerIp",
          priority: 0,
          action: { block: {} },
          statement: {
            rateBasedStatement: { limit: 3000, aggregateKeyType: "IP" },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "RateLimitPerIp",
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "AWSManagedCommonRuleSet",
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesCommonRuleSet",
              // SizeRestrictions_BODY (8KB) would reject legitimate spec/build
              // payloads (spec documents alone run to 400k chars). Enforcement
              // lives in the app where the limit can be right: pydantic field
              // caps + the 2MiB BodySizeLimitMiddleware backstop (core/limits).
              ruleActionOverrides: [
                { name: "SizeRestrictions_BODY", actionToUse: { count: {} } },
              ],
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "CommonRuleSet",
            sampledRequestsEnabled: true,
          },
        },
        {
          // Defense in depth: the application uses parameterised SQL only
          // (SQLAlchemy), but a WAF-level SQLi rule set costs nothing and the
          // live drill asserts it fires. The core rule set does NOT include
          // SQLi rules — that surprised the first drill run.
          name: "AWSManagedSqliRuleSet",
          priority: 3,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesSQLiRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "SqliRuleSet",
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "AWSManagedKnownBadInputs",
          priority: 2,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesKnownBadInputsRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "KnownBadInputs",
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    // ---- S14-01: security response headers at the edge.
    // CSP allows 'unsafe-inline'/'unsafe-eval' for scripts because Next.js
    // hydration + the Monaco editor require them without a nonce pipeline;
    // tightening to nonces is tracked for GA. Everything else is locked down.
    const securityHeaders = new cloudfront.ResponseHeadersPolicy(this, "SecurityHeaders", {
      responseHeadersPolicyName: "marshal-security-headers",
      comment: "S14-01 security headers",
      securityHeadersBehavior: {
        strictTransportSecurity: {
          accessControlMaxAge: cdk.Duration.days(365),
          includeSubdomains: true,
          preload: true,
          override: true,
        },
        contentTypeOptions: { override: true },
        frameOptions: {
          frameOption: cloudfront.HeadersFrameOption.DENY,
          override: true,
        },
        referrerPolicy: {
          referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
          override: true,
        },
        contentSecurityPolicy: {
          contentSecurityPolicy: [
            "default-src 'self'",
            // Pre-Beta hardening: 'unsafe-eval' removed — production Next.js
            // bundles don't eval (dev-mode does; dev never runs behind this
            // policy) and Monaco runs in workers, not eval. 'unsafe-inline'
            // stays until nonce plumbing (Next inline runtime scripts).
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            // Cognito hosted UI (token/refresh) + same-origin API proxy
            `connect-src 'self' ${cognitoDomain} https://cognito-idp.${region}.amazonaws.com`,
            "worker-src 'self' blob:", // Monaco web workers
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
          ].join("; "),
          override: true,
        },
      },
    });

    const distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: `marshal app (${appDomain || "cloudfront default domain"})`,
      ...(hasCustomDomain
        ? {
            domainNames: [appDomain],
            certificate: acm.Certificate.fromCertificateArn(this, "AppCert", appCertArn),
          }
        : {}),
      webAclId: webAcl.attrArn,
      defaultBehavior: {
        origin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
        responseHeadersPolicy: securityHeaders,
      },
      additionalBehaviors: {
        "/_next/static/*": {
          origin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
          responseHeadersPolicy: securityHeaders,
        },
        // S14-01: self-hosted Monaco (~27MB of immutable static assets). Without
        // its own behaviour it lands on the default CACHING_DISABLED path and
        // re-fetches from the origin on every editor open.
        "/monaco/*": {
          origin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
          responseHeadersPolicy: securityHeaders,
        },
      },
    });

    // Frontend needs its own public URL for Auth.js redirects — the custom
    // domain when one exists, else the distribution's own name (P0 mode).
    const publicAppUrl = hasCustomDomain
      ? `https://${appDomain}`
      : `https://${distribution.distributionDomainName}`;
    frontendContainer.addEnvironment("AUTH_URL", publicAppUrl);
    // Backend deep links (notification e-mails, webhooks, chat-ops) point at
    // the same public URL; the overlay may pin APP_BASE_URL explicitly.
    backendContainer.addEnvironment("APP_BASE_URL", process.env.APP_BASE_URL || publicAppUrl);

    new cdk.CfnOutput(this, "CloudFrontUrl", {
      value: `https://${distribution.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, "AlbDnsName", { value: alb.loadBalancerDnsName });

    // ============================================================ S16-01 ops
    // One dashboard + alarms → SNS ops topic. Alarm thresholds are calibrated
    // from the S13 load baseline (5x sustained ≈ 59 rps, p95 well under 1s on
    // the API read path) — they fire on "worse than the tested envelope", not
    // on guesses. Subscribe the ops channel via OPS_ALERT_EMAIL at deploy.
    const opsTopic = new sns.Topic(this, "OpsAlerts", {
      topicName: "marshal-ops-alerts",
      displayName: "marshal operational alarms",
    });
    if (process.env.OPS_ALERT_EMAIL) {
      opsTopic.addSubscription(
        new subscriptions.EmailSubscription(process.env.OPS_ALERT_EMAIL)
      );
    }
    const alertOn = (alarm: cloudwatch.Alarm) => {
      alarm.addAlarmAction(new cwactions.SnsAction(opsTopic));
      return alarm;
    };

    // ---- log-derived metrics (relay, election, audit) — the app logs these
    // through the awslogs driver; metric filters turn the S12 machinery's
    // behavior into graphable/alarmable numbers without touching app code.
    const opsNamespace = "MarshalOps";
    const relayReconnects = new logs.MetricFilter(this, "RelayReconnectFilter", {
      logGroup,
      filterPattern: logs.FilterPattern.literal('"event relay connection lost"'),
      metricNamespace: opsNamespace,
      metricName: "RelayReconnects",
      metricValue: "1",
    }).metric({ statistic: "Sum", period: cdk.Duration.minutes(5) });
    const ticksRan = new logs.MetricFilter(this, "TickRanFilter", {
      logGroup,
      filterPattern: logs.FilterPattern.literal('"tick ran (elected)"'),
      metricNamespace: opsNamespace,
      metricName: "SchedulerTicksRan",
      metricValue: "1",
    }).metric({ statistic: "Sum", period: cdk.Duration.minutes(15) });
    const ticksSkipped = new logs.MetricFilter(this, "TickSkippedFilter", {
      logGroup,
      // Both skip variants ("lock held by peer" / "window owned by peer") —
      // skips are HEALTHY with N tasks; the dashboard shows the ratio.
      filterPattern: logs.FilterPattern.literal('"tick skipped"'),
      metricNamespace: opsNamespace,
      metricName: "SchedulerTicksSkipped",
      metricValue: "1",
    }).metric({ statistic: "Sum", period: cdk.Duration.minutes(15) });
    const auditWriteFailures = new logs.MetricFilter(this, "AuditFailFilter", {
      logGroup,
      filterPattern: logs.FilterPattern.literal('"audit write failed"'),
      metricNamespace: opsNamespace,
      metricName: "AuditWriteFailures",
      metricValue: "1",
    }).metric({ statistic: "Sum", period: cdk.Duration.minutes(5) });

    // ---- AWS-native metrics
    const alb5xxTarget = alb.metrics.httpCodeTarget(elbv2.HttpCodeTarget.TARGET_5XX_COUNT, {
      statistic: "Sum", period: cdk.Duration.minutes(5),
    });
    const alb5xxElb = alb.metrics.httpCodeElb(elbv2.HttpCodeElb.ELB_5XX_COUNT, {
      statistic: "Sum", period: cdk.Duration.minutes(5),
    });
    const albP95 = alb.metrics.targetResponseTime({
      statistic: "p95", period: cdk.Duration.minutes(5),
    });
    const ddbThrottles = (table: dynamodb.Table, label: string) =>
      table.metric("ThrottledRequests", {
        statistic: "Sum", period: cdk.Duration.minutes(5), label,
      });
    const bedrockMetric = (name: string) =>
      new cloudwatch.Metric({
        namespace: "AWS/Bedrock", metricName: name,
        statistic: "Sum", period: cdk.Duration.minutes(5),
      });

    // ---- alarms (routed to the ops channel)
    alertOn(new cloudwatch.Alarm(this, "AlbTarget5xxAlarm", {
      alarmName: "marshal-alb-target-5xx",
      alarmDescription: "Backend/frontend returned 5xx through the ALB (runbook: docs/runbook.md#alb-5xx)",
      metric: alb5xxTarget, threshold: 10, evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alertOn(new cloudwatch.Alarm(this, "AlbLatencyAlarm", {
      alarmName: "marshal-alb-p95-latency",
      alarmDescription: "p95 above the S13 SLO envelope (runbook: docs/runbook.md#latency)",
      metric: albP95, threshold: 2, evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alertOn(new cloudwatch.Alarm(this, "FrontendUnhealthyAlarm", {
      alarmName: "marshal-frontend-unhealthy-hosts",
      alarmDescription: "Frontend target group has unhealthy hosts (runbook: docs/runbook.md#task-health)",
      metric: frontendTargets.metrics.unhealthyHostCount({ period: cdk.Duration.minutes(1) }),
      threshold: 0, evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alertOn(new cloudwatch.Alarm(this, "BackendUnhealthyAlarm", {
      alarmName: "marshal-backend-unhealthy-hosts",
      alarmDescription: "Backend target group has unhealthy hosts (runbook: docs/runbook.md#task-health)",
      metric: backendHealthTargets.metrics.unhealthyHostCount({ period: cdk.Duration.minutes(1) }),
      threshold: 0, evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alertOn(new cloudwatch.Alarm(this, "ChatDdbThrottleAlarm", {
      alarmName: "marshal-ddb-chat-throttles",
      alarmDescription: "Chat table throttling (runbook: docs/runbook.md#ddb-throttles)",
      metric: ddbThrottles(props.chatTable, "chat"), threshold: 0, evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alertOn(new cloudwatch.Alarm(this, "StateDdbThrottleAlarm", {
      alarmName: "marshal-ddb-state-throttles",
      alarmDescription: "Runtime-state table throttling — spend counters/leases at risk (runbook: docs/runbook.md#ddb-throttles)",
      metric: ddbThrottles(props.runtimeStateTable, "runtime-state"), threshold: 0, evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alertOn(new cloudwatch.Alarm(this, "BedrockThrottleAlarm", {
      alarmName: "marshal-bedrock-throttles",
      alarmDescription: "Bedrock throttling — chat/spec/codegen degrading (runbook: docs/runbook.md#bedrock)",
      metric: bedrockMetric("InvocationThrottles"), threshold: 5, evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alertOn(new cloudwatch.Alarm(this, "RelayFlapAlarm", {
      alarmName: "marshal-relay-flapping",
      alarmDescription: "Event relay reconnecting repeatedly — cross-task SSE degraded (runbook: docs/runbook.md#relay)",
      metric: relayReconnects, threshold: 3, evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alertOn(new cloudwatch.Alarm(this, "AuditFailAlarm", {
      alarmName: "marshal-audit-write-failures",
      alarmDescription: "Audit rows failing to persist — compliance evidence loss (runbook: docs/runbook.md#audit)",
      metric: auditWriteFailures, threshold: 0, evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));

    // ---- the dashboard (S16-01 AC list, one row per concern)
    const dashboard = new cloudwatch.Dashboard(this, "OpsDashboard", {
      dashboardName: "marshal-ops",
    });
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "ALB — requests & 5xx", width: 12,
        left: [alb.metrics.requestCount({ period: cdk.Duration.minutes(5) })],
        right: [alb5xxTarget, alb5xxElb],
      }),
      new cloudwatch.GraphWidget({
        title: "ALB — target response time (p50/p95/p99)", width: 12,
        left: ["p50", "p95", "p99"].map(
          (stat) => alb.metrics.targetResponseTime({ statistic: stat, period: cdk.Duration.minutes(5) })
        ),
      }),
    );
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Task health — healthy hosts", width: 8,
        left: [
          frontendTargets.metrics.healthyHostCount({ period: cdk.Duration.minutes(1), label: "frontend" }),
          backendHealthTargets.metrics.healthyHostCount({ period: cdk.Duration.minutes(1), label: "backend" }),
        ],
      }),
      new cloudwatch.GraphWidget({
        title: "Service CPU %", width: 8,
        left: [
          backendService.metricCpuUtilization({ label: "backend" }),
          frontendService.metricCpuUtilization({ label: "frontend" }),
        ],
      }),
      new cloudwatch.GraphWidget({
        title: "DynamoDB throttles", width: 8,
        left: [
          ddbThrottles(props.chatTable, "chat"),
          ddbThrottles(props.runtimeStateTable, "runtime-state"),
        ],
      }),
    );
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Bedrock — invocations / throttles / errors", width: 12,
        left: [bedrockMetric("Invocations")],
        right: [
          bedrockMetric("InvocationThrottles"),
          bedrockMetric("InvocationServerErrors"),
          bedrockMetric("InvocationClientErrors"),
        ],
      }),
      new cloudwatch.GraphWidget({
        title: "Relay & scheduler (S12 machinery)", width: 12,
        left: [relayReconnects],
        right: [ticksRan, ticksSkipped],
      }),
    );
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Audit write failures (must stay 0)", width: 12,
        left: [auditWriteFailures],
      })
    );

    new cdk.CfnOutput(this, "OpsTopicArn", { value: opsTopic.topicArn });
    new cdk.CfnOutput(this, "OpsDashboardName", { value: dashboard.dashboardName });
    void frontendTargets;
  }
}
