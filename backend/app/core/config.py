"""Application settings loaded from environment / .env file."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    app_name: str = "marshal"
    environment: str = "dev"
    aws_region: str = "us-east-1"
    # S16-06 release surface: baked into the image by CodeBuild
    # (--build-arg GIT_SHA/BUILD_TIME); empty in local dev (git fallback).
    git_sha: str = ""
    build_time: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://marshal:marshal@localhost:5432/marshal"

    # Cognito
    cognito_user_pool_id: str = ""
    cognito_client_id: str = ""

    # DynamoDB
    dynamo_table_chat: str = "marshal-chat-messages"

    # Bedrock models (env-configurable; FSD OQ-3 tiering amended to all-Sonnet-5
    # per product owner cost decision, 24 Jul 2026)
    bedrock_model_chat: str = "us.anthropic.claude-sonnet-5"
    bedrock_model_spec: str = "us.anthropic.claude-sonnet-5"
    bedrock_model_design: str = "us.anthropic.claude-sonnet-5"
    bedrock_model_fast: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    # S14-04: PII redaction guardrail (created by AppStack). Inert until the
    # security.pii_redaction platform setting is turned on.
    bedrock_guardrail_id: str = ""
    bedrock_guardrail_version: str = ""
    bedrock_max_tokens: int = 4096  # chat replies
    # Full documents (esp. design.md) regularly exceed 4k output tokens
    bedrock_max_tokens_generation: int = 8192

    # Chat context budget (chars ~= tokens * 4), FSD: 100k token window
    chat_context_char_budget: int = 400_000

    # Notifications / email (S5). Email is gated on SES production access
    # (owner action); in-app is the guaranteed channel.
    # Installation-neutral defaults: the cloud task receives APP_BASE_URL
    # (CloudFront URL or APP_DOMAIN) and EMAIL_FROM from AppStack / the overlay.
    email_enabled: bool = False
    email_from: str = "marshal <no-reply@localhost>"
    app_base_url: str = "http://localhost:3000"

    # Sandbox spend surfacing (S5 R6) — gated on Cost Explorer enablement
    cost_explorer_enabled: bool = False

    # Mandatory security-setting posture. Local/dev stays fail-open for
    # bootstrap convenience; cloud explicitly pins this true in AppStack.
    security_fail_closed: bool = False

    # External codegen (S9): workspace bucket + CodeBuild runner project.
    # CODEGEN_PROVIDER is authoritative in cloud and a bootstrap default locally.
    # `runner` = the S3/CodeBuild workspace runner; "kiro" is accepted as a
    # legacy alias for "runner".
    codegen_provider: str = "internal"  # internal | runner
    codegen_workspace_bucket: str = ""
    codegen_runner_project: str = "marshal-codegen-runner"
    codegen_token_budget: int = 60_000  # per-build hard stop, enforced runner-side
    codegen_external_timeout_s: int = 900

    # Sandbox / deployment
    sandbox_provider: str = "direct"  # direct | isb
    # Direct mode: the scoped in-account deployment role AppStack creates
    # (MarshalDirectDeployRole). When set, the direct provider assumes it per
    # deployment instead of deploying on the control plane's own credentials;
    # unset (local/dev) falls back to ambient credentials.
    direct_deploy_role_arn: str = ""
    isb_api_base_url: str = ""
    # Product-owner decision (24 Jul 2026): TTL 30 days, budget $1000/deployment
    isb_lease_duration_hours: int = 720
    isb_lease_budget_usd: int = 1000
    isb_jwt_secret_name: str = "/InnovationSandbox/marshal/Auth/JwtSecret"
    isb_lease_template_name: str = "marshal-default"
    isb_deployment_role_name: str = "AIFactoryDeploymentRole"
    isb_blueprint_id: str = ""  # set after blueprint registration
    isb_lease_activation_timeout_s: int = 900
    sample_app_template_path: str = "../sample-app/template.json"
    deployment_stack_prefix: str = "marshal"
    # B20 R0.6: management-account MarshalCostReader role for the CE spend
    # poller (was a raw os.environ read in sandbox_costs; env var unchanged)
    cost_reader_role_arn: str = ""

    @model_validator(mode="after")
    def _default_security_posture(self) -> "Settings":
        """Cloud fails closed unless SECURITY_FAIL_CLOSED is explicitly set."""
        if "security_fail_closed" not in self.model_fields_set:
            self.security_fail_closed = self.environment.strip().lower() == "cloud"
        return self

    @property
    def cognito_issuer(self) -> str:
        return f"https://cognito-idp.{self.aws_region}.amazonaws.com/{self.cognito_user_pool_id}"

    @property
    def cognito_jwks_url(self) -> str:
        return f"{self.cognito_issuer}/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
