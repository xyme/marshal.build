import * as path from "node:path";
import * as cdk from "aws-cdk-lib";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3assets from "aws-cdk-lib/aws-s3-assets";
import { Construct } from "constructs";

/**
 * Image build pipeline — the dev machine has no Docker, so container images are
 * built in CodeBuild from an S3 asset of the repository source.
 * `npm run build-images` (scripts/deploy-cloud.sh) starts the build and waits.
 */
export class MarshalBuildStack extends cdk.Stack {
  public readonly backendRepo: ecr.Repository;
  public readonly frontendRepo: ecr.Repository;
  public readonly runnerRepo: ecr.Repository;
  public readonly workspaceBucket: s3.Bucket;
  public readonly project: codebuild.Project;
  public readonly runnerProject: codebuild.Project;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // S14-03: scan-on-push on every repo — findings are visible per image
    // digest in ECR; the build gate below covers dependency-level issues.
    this.backendRepo = new ecr.Repository(this, "BackendRepo", {
      repositoryName: "marshal/backend",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      imageScanOnPush: true,
      lifecycleRules: [{ maxImageCount: 10 }],
    });
    this.frontendRepo = new ecr.Repository(this, "FrontendRepo", {
      repositoryName: "marshal/frontend",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      imageScanOnPush: true,
      lifecycleRules: [{ maxImageCount: 10 }],
    });
    // S9: reference codegen runner image — the workspace-runner integration
    // point. A real external engine can replace the image; the workspace
    // contract holds (see docs/codegen-workspace-contract.md).
    this.runnerRepo = new ecr.Repository(this, "RunnerRepo", {
      repositoryName: "marshal/runner",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      imageScanOnPush: true,
      lifecycleRules: [{ maxImageCount: 10 }],
    });

    // S9: codegen workspace — the wire format between platform and engine
    // (docs/codegen-workspace-contract.md). Lifecycle: builds/* (wire format)
    // expires in 30d; artifacts/* (S10 durable store for cdk-app content)
    // retains 180d — the Beta approximation of the spec's build-deletion sweep.
    this.workspaceBucket = new s3.Bucket(this, "CodegenWorkspace", {
      bucketName: `marshal-codegen-workspace-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      lifecycleRules: [
        { prefix: "builds/", expiration: cdk.Duration.days(30) },
        { prefix: "artifacts/", expiration: cdk.Duration.days(180) },
        // S14-03: SBOM evidence retained a year (audit-window friendly)
        { prefix: "sbom/", expiration: cdk.Duration.days(365) },
      ],
      // Private build/reset/rehearsal evidence survives stack deletion. Object
      // expiry remains governed by the explicit prefix lifecycles above.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const source = new s3assets.Asset(this, "SourceAsset", {
      path: path.join(__dirname, "..", ".."),
      // GIT ignore semantics so a leading slash means "root only". With the
      // default glob mode, the bare pattern `docs` also matched NESTED
      // directories and silently removed the in-app documentation feature
      // (`frontend/src/app/(app)/docs` + `frontend/src/content/docs`) from the
      // source uploaded to CodeBuild — the route was simply absent from the
      // built image, with no error anywhere. Found live: /docs 404'd in
      // production while every local check passed (S16 deploy).
      ignoreMode: cdk.IgnoreMode.GIT,
      exclude: [
        "node_modules",
        "**/node_modules",
        ".git",
        "**/.next",
        "**/.venv",
        "**/cdk.out",
        "**/__pycache__",
        "**/.pytest_cache",
        "**/.ruff_cache",
        ".env",
        "**/.env",
        "**/.env.*",
        ".auth-secret",
        "/docs", // the repository's own docs/, NOT app route dirs named docs
        "/.kiro",
        // Local-only material that was reaching the S3 source zip (private
        // bucket, but still a copy of a secret token, private notes and the
        // deploy overlay that nothing in the build reads). Excluding it also
        // keeps the asset hash stable across overlay edits, so flipping an
        // overlay value no longer re-uploads the whole source bundle.
        ".origin-verify",
        "/private",
        "/.agents",
        "/.worktrees",
        "/scripts/*.local.sh",
        "/scripts/*.local.txt",
        "/scripts/*.bak",
      ],
    });

    this.project = new codebuild.Project(this, "ImageBuild", {
      projectName: "marshal-image-build",
      description: "Builds backend + frontend container images and pushes to ECR",
      source: codebuild.Source.s3({
        bucket: source.bucket,
        path: source.s3ObjectKey,
      }),
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        computeType: codebuild.ComputeType.MEDIUM,
        privileged: true, // docker build
      },
      environmentVariables: {
        AWS_ACCOUNT_ID: { value: this.account },
        BACKEND_REPO_URI: { value: this.backendRepo.repositoryUri },
        FRONTEND_REPO_URI: { value: this.frontendRepo.repositoryUri },
        RUNNER_REPO_URI: { value: this.runnerRepo.repositoryUri },
        WORKSPACE_BUCKET: { value: `marshal-codegen-workspace-${this.account}` },
        // NEXT_PUBLIC_* build args supplied per-build via startBuild overrides
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: "0.2",
        phases: {
          pre_build: {
            commands: [
              "aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com",
              "export IMAGE_TAG=${CODEBUILD_RESOLVED_SOURCE_VERSION:-manual}-$(date +%s)",
              "echo Image tag: $IMAGE_TAG",
              // S14-03 supply-chain gate: dependency audit + SBOM BEFORE the
              // images are built. CRITICAL runtime findings fail the build;
              // lower severities are reported for sprint triage. Logic lives
              // in the repo (runnable locally) rather than inline here.
              // AUDIT_STRICT=1: any NEW high/critical finding fails the build.
              // The accepted baseline is documented per-advisory in
              // scripts/audit-allowlist.txt (triaged 29 Jul 2026).
              "AUDIT_STRICT=1 bash scripts/supply-chain-audit.sh /tmp/sbom",
            ],
          },
          build: {
            commands: [
              "echo Building backend image",
              // S16-06: stamp the image with what it was built from. The
              // source is an S3 asset, not a git checkout, so
              // CODEBUILD_RESOLVED_SOURCE_VERSION is an object version, not a
              // commit — the deploy script passes the real SHA as a GIT_SHA
              // build-environment override (falls back to the object version).
              "docker build -f backend/Dockerfile --build-arg GIT_SHA=${GIT_SHA:-${CODEBUILD_RESOLVED_SOURCE_VERSION:-unknown}} --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) -t $BACKEND_REPO_URI:latest -t $BACKEND_REPO_URI:$IMAGE_TAG .",
              "echo Building frontend image",
              "docker build -f frontend/Dockerfile --build-arg NEXT_PUBLIC_COGNITO_DOMAIN=$NEXT_PUBLIC_COGNITO_DOMAIN --build-arg NEXT_PUBLIC_COGNITO_CLIENT_ID=$NEXT_PUBLIC_COGNITO_CLIENT_ID --build-arg NEXT_PUBLIC_SUPPORT_EMAIL=$NEXT_PUBLIC_SUPPORT_EMAIL --build-arg NEXT_PUBLIC_SUPPORT_HOURS=\"$NEXT_PUBLIC_SUPPORT_HOURS\" -t $FRONTEND_REPO_URI:latest -t $FRONTEND_REPO_URI:$IMAGE_TAG .",
              "echo Building codegen runner image",
              "docker build -f runner/Dockerfile -t $RUNNER_REPO_URI:latest -t $RUNNER_REPO_URI:$IMAGE_TAG .",
            ],
          },
          post_build: {
            commands: [
              "docker push --all-tags $BACKEND_REPO_URI",
              "docker push --all-tags $FRONTEND_REPO_URI",
              "docker push --all-tags $RUNNER_REPO_URI",
              "echo Pushed tags latest + $IMAGE_TAG",
              // S14-03: SBOMs kept as durable evidence, keyed by image tag
              "aws s3 cp /tmp/sbom s3://$WORKSPACE_BUCKET/sbom/$IMAGE_TAG/ --recursive || echo 'SBOM upload skipped'",
            ],
          },
        },
      }),
    });

    this.backendRepo.grantPullPush(this.project);
    this.frontendRepo.grantPullPush(this.project);
    this.runnerRepo.grantPullPush(this.project);
    // S14-03: SBOM evidence lands under sbom/<image-tag>/ (write-only prefix)
    this.project.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "SbomEvidenceWrite",
        actions: ["s3:PutObject"],
        resources: [`${this.workspaceBucket.bucketArn}/sbom/*`],
      })
    );

    // ---- S9: the codegen runner execution project -------------------------
    // Runs the runner image as its build environment; one build = one codegen
    // job, parameterized ONLY by WORKSPACE_URI. Isolation posture (spec R3):
    // Bedrock invoke + the workspace builds/* prefix — no DB, no platform APIs.
    this.runnerProject = new codebuild.Project(this, "CodegenRunner", {
      projectName: "marshal-codegen-runner",
      description: "Executes one external codegen build against the workspace contract",
      source: codebuild.Source.s3({
        // CodeBuild demands a source even when unused; reuse the repo asset
        // (the runner reads everything from the workspace, not the source).
        bucket: source.bucket,
        path: source.s3ObjectKey,
      }),
      environment: {
        buildImage: codebuild.LinuxBuildImage.fromEcrRepository(this.runnerRepo, "latest"),
        computeType: codebuild.ComputeType.SMALL,
        privileged: false,
      },
      timeout: cdk.Duration.minutes(20),
      environmentVariables: {
        WORKSPACE_URI: { value: "" }, // per-build via startBuild override
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: "0.2",
        phases: { build: { commands: ["cd /app && python runner_main.py"] } },
      }),
    });
    this.runnerProject.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "RunnerBedrockInvoke",
        actions: ["bedrock:InvokeModel"],
        resources: ["*"],
      })
    );
    this.runnerProject.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "RunnerWorkspaceObjects",
        actions: ["s3:GetObject", "s3:PutObject"],
        resources: [`${this.workspaceBucket.bucketArn}/builds/*`],
      })
    );
    this.runnerProject.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "RunnerWorkspaceList",
        actions: ["s3:ListBucket"],
        resources: [this.workspaceBucket.bucketArn],
        conditions: { StringLike: { "s3:prefix": "builds/*" } },
      })
    );

    new cdk.CfnOutput(this, "BuildProjectName", { value: this.project.projectName });
    new cdk.CfnOutput(this, "BackendRepoUri", { value: this.backendRepo.repositoryUri });
    new cdk.CfnOutput(this, "FrontendRepoUri", { value: this.frontendRepo.repositoryUri });
    new cdk.CfnOutput(this, "RunnerRepoUri", { value: this.runnerRepo.repositoryUri });
    new cdk.CfnOutput(this, "CodegenWorkspaceBucket", { value: this.workspaceBucket.bucketName });
    new cdk.CfnOutput(this, "CodegenRunnerProject", { value: this.runnerProject.projectName });
  }
}
