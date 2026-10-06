#!/usr/bin/env bash
# Deploy-time env context for marshal (source, don't execute).
#
# The S12 incident: a bare `cdk deploy` bakes synth-time PLACEHOLDER values into
# task definitions and Cognito callback lists, breaking auth platform-wide. This
# file is the single place that context lives, so every deploy/diff carries it.
#
# PORTABILITY (B22 P0, FSD §13.5T): this file is GENERIC. Installation-specific
# pins live in the OPTIONAL, gitignored overlay `scripts/deploy-env.local.sh`
# (sourced first). Identity values not pinned by the overlay are DERIVED from
# the live MarshalAuthStack/MarshalAppStack outputs; on a fresh account run
# `./scripts/deploy-cloud.sh --bootstrap` (see that script's header).
#
# What the overlay can pin (plain `export KEY=value` lines):
#   MARSHAL_AWS_ACCOUNT_ID   account the deploy may touch (deploy-cloud.sh guard)
#   AWS_PROFILE, AWS_REGION  defaults: `default`, us-east-1 (region is pinned)
#   SANDBOX_PROVIDER         direct (default: agents deploy into THIS account) | isb
#   MARSHAL_TIER             production (default: Multi-AZ RDS, two-task floors, Container
#                            Insights) | evaluate (single-AZ, one-task floors, Insights off)
#   CODEGEN_PROVIDER         internal (default) | runner — the CodeBuild workspace runner;
#                            required for the cdk-app profile; `kiro` is a legacy alias
#   COGNITO_DOMAIN_PREFIX    hosted-UI prefix, globally unique; default marshal-ai-<account>
#   APP_DOMAIN, APP_CERT_ARN custom domain + ACM cert (both, or neither = CloudFront URL)
#   OPS_ALERT_EMAIL          CloudWatch alarm subscriber (recommended)
#   NEXT_PUBLIC_SUPPORT_EMAIL support address baked into the frontend image
#   NEXT_PUBLIC_SUPPORT_HOURS support hours shown beside it (optional)
#   APP_BASE_URL             public URL in notification/webhook/chat-ops links; default derived (APP_DOMAIN or CloudFront)
#   EMAIL_FROM               SES sender identity for e-mail notifications (when EMAIL_ENABLED)
#   MARKETING_CERT_ARN, MARKETING_APEX_DOMAIN  owner's marketing site; unset = stack not registered
#   COST_EXPLORER_ENABLED, COST_READER_ROLE_ARN  Cost Explorer spend reader (default off)
#   ENTRA_SAML_METADATA_URL, OKTA_OIDC_ISSUER, IDP_ROLE_MAPPING  enterprise SSO (docs/sso.md)
#   COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, COGNITO_DOMAIN, CLOUDFRONT_URL
#                            identity pins; normally derived from the live stacks
#
#   source scripts/deploy-env.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- installation overlay (owner pins; absent on a fresh clone)
if [[ -f "$REPO_ROOT/scripts/deploy-env.local.sh" ]]; then
  # shellcheck source=/dev/null
  source "$REPO_ROOT/scripts/deploy-env.local.sh"
fi

export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_PAGER=""

# The CDK CLI resolves credentials through the JS SDK, which needs a LIVE SSO
# access token; the AWS CLI meanwhile keeps working off cached role credentials.
# Materialize the CLI's credentials as env vars so both paths agree; if this
# fails, the fix is `aws sso login --profile $AWS_PROFILE` (or static creds).
if _creds=$(aws configure export-credentials --profile "$AWS_PROFILE" --format env 2>/dev/null); then
  eval "$_creds"
  unset _creds
else
  echo "ERROR: could not export credentials for profile '$AWS_PROFILE'." >&2
  echo "       Run: aws sso login --profile $AWS_PROFILE" >&2
  return 1 2>/dev/null || exit 1
fi

_stack_output() { # stack, key -> value or empty
  aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null || true
}

# --- identity: overlay pins win; otherwise derive from the live auth stack.
# Empty values are tolerated ONLY under bootstrap (no stacks exist yet).
if [[ -z "${COGNITO_USER_POOL_ID:-}" ]]; then
  export COGNITO_USER_POOL_ID="$(_stack_output MarshalAuthStack UserPoolId)"
  export COGNITO_CLIENT_ID="$(_stack_output MarshalAuthStack UserPoolClientId)"
  export COGNITO_DOMAIN="$(_stack_output MarshalAuthStack HostedUiDomain)"
fi
if [[ -z "${COGNITO_USER_POOL_ID:-}" && "${MARSHAL_BOOTSTRAP:-0}" != "1" ]]; then
  echo "ERROR: no Cognito identity — MarshalAuthStack not found and no overlay pin." >&2
  echo "       Fresh account? Run ./scripts/deploy-cloud.sh --bootstrap" >&2
  return 1 2>/dev/null || exit 1
fi

# --- OAuth URL lists. Pre-set so the FIRST auth-stack deploy of a run cannot
# regress the client to localhost-only. The app CloudFront URL joins the list
# once the app stack exists (or via the overlay pin).
export APP_DOMAIN="${APP_DOMAIN:-}"
if [[ -z "${CLOUDFRONT_URL:-}" ]]; then
  CLOUDFRONT_URL="$(_stack_output MarshalAppStack CloudFrontUrl)"
fi
_callbacks="http://localhost:3000/api/auth/callback/cognito"
_logouts="http://localhost:3000"
if [[ -n "$CLOUDFRONT_URL" ]]; then
  _callbacks="$_callbacks,${CLOUDFRONT_URL}/api/auth/callback/cognito"
  _logouts="$_logouts,${CLOUDFRONT_URL}"
fi
if [[ -n "$APP_DOMAIN" ]]; then
  _callbacks="$_callbacks,https://${APP_DOMAIN}/api/auth/callback/cognito"
  _logouts="$_logouts,https://${APP_DOMAIN}"
fi
export OAUTH_CALLBACK_URLS="$_callbacks"
export OAUTH_LOGOUT_URLS="$_logouts"
unset _callbacks _logouts

# --- origin lockdown token (generated by --bootstrap on a fresh clone)
if [[ ! -s "$REPO_ROOT/.origin-verify" ]]; then
  if [[ "${MARSHAL_BOOTSTRAP:-0}" == "1" ]]; then
    openssl rand -hex 24 > "$REPO_ROOT/.origin-verify"
    chmod 600 "$REPO_ROOT/.origin-verify"
    echo "bootstrap: generated .origin-verify"
  else
    echo "ERROR: $REPO_ROOT/.origin-verify is missing or empty; refusing a live deploy." >&2
    return 1 2>/dev/null || exit 1
  fi
fi
export ORIGIN_VERIFY_TOKEN="$(cat "$REPO_ROOT/.origin-verify")"

# --- Enclave provider. Generic default is `direct` (deploy into this
# account); an ISB installation pins SANDBOX_PROVIDER=isb in the overlay and
# must then supply the ISB endpoint + blueprint via backend/.env.
export SANDBOX_PROVIDER="${SANDBOX_PROVIDER:-direct}"
if [[ "$SANDBOX_PROVIDER" == "isb" ]]; then
  if [[ ! -f "$REPO_ROOT/backend/.env" ]]; then
    echo "ERROR: SANDBOX_PROVIDER=isb but $REPO_ROOT/backend/.env is missing." >&2
    return 1 2>/dev/null || exit 1
  fi
  export ISB_API_BASE_URL="$(grep '^ISB_API_BASE_URL=' "$REPO_ROOT/backend/.env" | cut -d= -f2-)"
  export ISB_BLUEPRINT_ID="$(grep '^ISB_BLUEPRINT_ID=' "$REPO_ROOT/backend/.env" | cut -d= -f2-)"
  if [[ -z "$ISB_API_BASE_URL" || -z "$ISB_BLUEPRINT_ID" ]]; then
    echo "ERROR: ISB_API_BASE_URL and ISB_BLUEPRINT_ID must both be non-empty (isb provider)." >&2
    return 1 2>/dev/null || exit 1
  fi
fi

# --- sizing tier (G18). infra/bin/marshal.ts reads MARSHAL_TIER; production
# is today's exact sizing, evaluate trims RDS Multi-AZ, the two-task service
# floors and Container Insights (README "What it costs to leave running").
export MARSHAL_TIER="${MARSHAL_TIER:-production}"
case "$MARSHAL_TIER" in
  production|evaluate) ;;
  *)
    echo "ERROR: MARSHAL_TIER must be 'production' or 'evaluate' (got '$MARSHAL_TIER')." >&2
    return 1 2>/dev/null || exit 1 ;;
esac

# --- codegen provider pin. infra/lib/app-stack.ts bakes it into the backend
# task env, where it is authoritative in the cloud (no DB setting can select
# the runner). internal = in-process Bedrock synthesis (default); runner = the
# CodeBuild workspace runner, required for the cdk-app profile; `kiro` is a
# legacy alias for runner. Changing it needs a MarshalAppStack redeploy.
export CODEGEN_PROVIDER="${CODEGEN_PROVIDER:-internal}"
case "$CODEGEN_PROVIDER" in
  internal|runner|kiro) ;;
  *)
    echo "ERROR: CODEGEN_PROVIDER must be 'internal' or 'runner' (got '$CODEGEN_PROVIDER')." >&2
    return 1 2>/dev/null || exit 1 ;;
esac

# --- ops alarm destination (optional but strongly recommended)
if [[ -z "${OPS_ALERT_EMAIL:-}" ]]; then
  echo "WARN: OPS_ALERT_EMAIL unset — CloudWatch alarms will have no email subscriber." >&2
fi

# --- installation copy + outbound links (all optional; overlay pins them).
# Frontend build args (inlined at image build): the profile page shows a
# Support block only when NEXT_PUBLIC_SUPPORT_EMAIL is set.
export NEXT_PUBLIC_SUPPORT_EMAIL="${NEXT_PUBLIC_SUPPORT_EMAIL:-}"
export NEXT_PUBLIC_SUPPORT_HOURS="${NEXT_PUBLIC_SUPPORT_HOURS:-}"
# Backend task env: APP_BASE_URL is the public URL used in notification /
# webhook / chat-ops deep links — AppStack derives it from APP_DOMAIN or the
# CloudFront URL unless pinned here. EMAIL_FROM is the SES sender identity
# (only meaningful once EMAIL_ENABLED=true on the installation).
export APP_BASE_URL="${APP_BASE_URL:-}"
export EMAIL_FROM="${EMAIL_FROM:-}"
export MARSHAL_DEPLOY_ENV_LOADED=1
