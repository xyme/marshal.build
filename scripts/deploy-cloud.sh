#!/usr/bin/env bash
# Full cloud deployment for the existing marshal installation (no localhost):
#   0. Load and validate live account/auth/origin/ISB context
#   1. Deploy network + data + auth + build stacks
#   2. Update the existing frontend auth secret bundle
#   3. Build container images in CodeBuild (no local Docker required)
#   4. Deploy the app stack (ECS + ALB + CloudFront)
#   5. Register the CloudFront callback URLs on the Cognito client
#   6. Force both ECS services onto rebuilt :latest images and wait stable
#   7. Fail-closed landing/database-health smoke
# Usage: ./scripts/deploy-cloud.sh
#   Fresh account first run: ./scripts/deploy-cloud.sh --bootstrap
#     (generates .origin-verify/.auth-secret, creates the frontend auth
#      secret, and skips the live-identity cross-check that cannot exist yet)
# Safe context check only: MARSHAL_DEPLOY_PREFLIGHT_ONLY=1 ./scripts/deploy-cloud.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "${1:-}" == "--bootstrap" ]]; then
  export MARSHAL_BOOTSTRAP=1
fi

# The first AuthStack deploy must already carry the live callback/logout lists;
# loading context later can leave auth regressed if a release stops mid-run.
if [[ "${MARSHAL_DEPLOY_ENV_LOADED:-0}" != "1" ]]; then
  # shellcheck source=deploy-env.sh
  source "$SCRIPT_DIR/deploy-env.sh"
fi
cd "$REPO_ROOT"

export AWS_REGION=${AWS_REGION:-us-east-1}
# B22 P0: no baked-in account. The overlay pins it for this installation; a
# fresh operator sets MARSHAL_AWS_ACCOUNT_ID explicitly (their own guard rail
# against deploying into the wrong account).
EXPECTED_ACCOUNT_ID=${MARSHAL_AWS_ACCOUNT_ID:?set MARSHAL_AWS_ACCOUNT_ID (or create scripts/deploy-env.local.sh)}

outputs() { aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$1" \
  --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }

step() { printf "\n\033[1;36m=== %s ===\033[0m\n" "$1"; }

require_value() {
  local name=$1
  [[ -n "${!name:-}" ]] || { echo "ERROR: required deploy value $name is empty" >&2; exit 1; }
}

step "Preflight live deploy context"
[[ "$AWS_REGION" == "us-east-1" ]] || {
  # Structural, not preference: the CLOUDFRONT-scope WAF must live in us-east-1.
  echo "ERROR: marshal deploys pin us-east-1 (CloudFront WAF scope), got $AWS_REGION" >&2
  exit 1
}
CALLER_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
[[ "$CALLER_ACCOUNT" == "$EXPECTED_ACCOUNT_ID" ]] || {
  echo "ERROR: refusing account $CALLER_ACCOUNT; expected $EXPECTED_ACCOUNT_ID" >&2
  exit 1
}
_required=(OAUTH_CALLBACK_URLS OAUTH_LOGOUT_URLS ORIGIN_VERIFY_TOKEN)
if [[ "${MARSHAL_BOOTSTRAP:-0}" != "1" ]]; then
  _required+=(COGNITO_USER_POOL_ID COGNITO_CLIENT_ID COGNITO_DOMAIN)
fi
if [[ "${SANDBOX_PROVIDER:-direct}" == "isb" ]]; then
  _required+=(ISB_API_BASE_URL ISB_BLUEPRINT_ID)
fi
for name in "${_required[@]}"; do
  require_value "$name"
done
if [[ ! -s .auth-secret ]]; then
  if [[ "${MARSHAL_BOOTSTRAP:-0}" == "1" ]]; then
    openssl rand -base64 48 | tr -d '\n' > .auth-secret
    chmod 600 .auth-secret
    echo "bootstrap: generated .auth-secret"
  else
    echo "ERROR: .auth-secret is missing/empty; refusing to rotate the live Auth.js secret" >&2
    exit 1
  fi
fi
LIVE_POOL_ID=$(outputs MarshalAuthStack UserPoolId 2>/dev/null || true)
LIVE_CLIENT_ID=$(outputs MarshalAuthStack UserPoolClientId 2>/dev/null || true)
LIVE_DOMAIN=$(outputs MarshalAuthStack HostedUiDomain 2>/dev/null || true)
if [[ -z "$LIVE_POOL_ID" && "${MARSHAL_BOOTSTRAP:-0}" == "1" ]]; then
  echo "bootstrap: MarshalAuthStack not deployed yet — identity cross-check deferred"
  LIVE_POOL_ID="" LIVE_CLIENT_ID="" LIVE_DOMAIN=""
fi
[[ "$COGNITO_USER_POOL_ID" == "$LIVE_POOL_ID" \
  && "$COGNITO_CLIENT_ID" == "$LIVE_CLIENT_ID" \
  && "$COGNITO_DOMAIN" == "$LIVE_DOMAIN" ]] || {
  echo "ERROR: deploy-env Cognito identity does not match MarshalAuthStack" >&2
  exit 1
}
echo "context verified: account=$CALLER_ACCOUNT region=$AWS_REGION pool=${LIVE_POOL_ID:-<bootstrap>} tier=${MARSHAL_TIER:-production}"
if [[ "${MARSHAL_DEPLOY_PREFLIGHT_ONLY:-0}" == "1" ]]; then
  echo "preflight-only requested; no cloud mutation performed"
  exit 0
fi

step "1/7 Deploy foundation stacks (network, auth, data, build)"
(cd infra && npx cdk deploy MarshalNetworkStack MarshalAuthStack MarshalDataStack MarshalBuildStack \
  --require-approval never --concurrency 2)

USER_POOL_ID=$(outputs MarshalAuthStack UserPoolId)
CLIENT_ID=$(outputs MarshalAuthStack UserPoolClientId)
COGNITO_DOMAIN=$(outputs MarshalAuthStack HostedUiDomain)
BUILD_PROJECT=$(outputs MarshalBuildStack BuildProjectName)

step "2/7 Write frontend auth secret bundle"
CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client --region "$AWS_REGION" \
  --user-pool-id "$USER_POOL_ID" --client-id "$CLIENT_ID" \
  --query "UserPoolClient.ClientSecret" --output text)
SECRET_JSON=$(python3 - "$CLIENT_SECRET" "$(cat .auth-secret)" <<'PY'
import json, sys
print(json.dumps({"AUTH_COGNITO_SECRET": sys.argv[1], "AUTH_SECRET": sys.argv[2]}))
PY
)
if aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id marshal/frontend/auth >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --region "$AWS_REGION" --secret-id marshal/frontend/auth \
    --secret-string "$SECRET_JSON" > /dev/null && echo "secret updated"
elif [[ "${MARSHAL_BOOTSTRAP:-0}" == "1" ]]; then
  # B22 P0: the formerly out-of-repo bootstrap step, now the --bootstrap path.
  aws secretsmanager create-secret --region "$AWS_REGION" --name marshal/frontend/auth \
    --secret-string "$SECRET_JSON" > /dev/null && echo "bootstrap: secret created"
else
  echo "ERROR: marshal/frontend/auth is missing; run ./scripts/deploy-cloud.sh --bootstrap" >&2
  exit 1
fi

step "3/7 Build container images in CodeBuild"
# S16-06: the version surface must report the commit this image was built from.
# CodeBuild's source is an S3 asset (not a git checkout), so pass it explicitly;
# a dirty tree is marked so build-info can never imply a clean commit.
GIT_SHA=$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
git diff --quiet 2>/dev/null || GIT_SHA="$GIT_SHA-dirty"
echo "stamping images with GIT_SHA=$GIT_SHA"
BUILD_ID=$(aws codebuild start-build --region "$AWS_REGION" --project-name "$BUILD_PROJECT" \
  --environment-variables-override \
    "name=NEXT_PUBLIC_COGNITO_DOMAIN,value=$COGNITO_DOMAIN,type=PLAINTEXT" \
    "name=NEXT_PUBLIC_COGNITO_CLIENT_ID,value=$CLIENT_ID,type=PLAINTEXT" \
    "name=NEXT_PUBLIC_SUPPORT_EMAIL,value=${NEXT_PUBLIC_SUPPORT_EMAIL:-},type=PLAINTEXT" \
    "name=NEXT_PUBLIC_SUPPORT_HOURS,value=${NEXT_PUBLIC_SUPPORT_HOURS:-},type=PLAINTEXT" \
    "name=GIT_SHA,value=$GIT_SHA,type=PLAINTEXT" \
  --query "build.id" --output text)
echo "build: $BUILD_ID"
while true; do
  STATUS=$(aws codebuild batch-get-builds --region "$AWS_REGION" --ids "$BUILD_ID" \
    --query "builds[0].buildStatus" --output text)
  echo "  build status: $STATUS"
  case "$STATUS" in
    SUCCEEDED) break ;;
    FAILED|FAULT|STOPPED|TIMED_OUT)
      echo "CodeBuild failed — logs:"
      aws codebuild batch-get-builds --region "$AWS_REGION" --ids "$BUILD_ID" \
        --query "builds[0].logs.deepLink" --output text
      exit 1 ;;
    *) sleep 20 ;;
  esac
done

step "4/7 Deploy app stack (ECS + ALB + CloudFront)"
export COGNITO_USER_POOL_ID="$USER_POOL_ID" COGNITO_CLIENT_ID="$CLIENT_ID" COGNITO_DOMAIN="$COGNITO_DOMAIN"
# deploy-env.sh exports the ISB values only under SANDBOX_PROVIDER=isb; a bare
# reference here is an unbound-variable abort for every `direct` installation.
echo "SANDBOX_PROVIDER=${SANDBOX_PROVIDER:-direct} ISB_API_BASE_URL=${ISB_API_BASE_URL:-}"
(cd infra && npx cdk deploy MarshalAppStack --require-approval never)

CLOUDFRONT_URL=$(outputs MarshalAppStack CloudFrontUrl)
require_value CLOUDFRONT_URL
echo "CloudFront: $CLOUDFRONT_URL"

step "5/7 Register callback URLs on the Cognito client"
# B22 P0: the custom domain is an overlay INPUT (same rule as deploy-env.sh);
# a fresh installation without one runs on the CloudFront URL alone.
# Start from the lists deploy-env.sh computed (localhost + any pinned
# CloudFront/APP_DOMAIN entries) and make sure THIS run's CloudFront URL is
# registered. A custom-domain callback is appended ONLY when APP_DOMAIN is set
# — there is no baked-in domain, so a foreign installation never registers
# someone else's host.
_cb="$OAUTH_CALLBACK_URLS"
_lo="$OAUTH_LOGOUT_URLS"
if [[ ",$_cb," != *",$CLOUDFRONT_URL/api/auth/callback/cognito,"* ]]; then
  _cb="$_cb,$CLOUDFRONT_URL/api/auth/callback/cognito"
  _lo="$_lo,$CLOUDFRONT_URL"
fi
if [[ -n "${APP_DOMAIN:-}" && ",$_cb," != *",https://${APP_DOMAIN}/api/auth/callback/cognito,"* ]]; then
  _cb="$_cb,https://${APP_DOMAIN}/api/auth/callback/cognito"
  _lo="$_lo,https://${APP_DOMAIN}"
fi
export OAUTH_CALLBACK_URLS="$_cb" OAUTH_LOGOUT_URLS="$_lo"
unset _cb _lo
(cd infra && npx cdk deploy MarshalAuthStack --require-approval never)

step "6/7 Force rebuilt images onto ECS and wait for stability"
for service in marshal-backend marshal-frontend; do
  aws ecs update-service --region "$AWS_REGION" --cluster marshal \
    --service "$service" --force-new-deployment >/dev/null
  echo "rollout requested: $service"
done
aws ecs wait services-stable --region "$AWS_REGION" --cluster marshal \
  --services marshal-backend marshal-frontend
# services-stable returns once running==desired with a single PRIMARY
# deployment; ECS marks that deployment's rolloutState COMPLETED a few seconds
# LATER. A one-shot assertion here aborted a healthy fresh-account install at
# 6/7 with "rollout incomplete: … ['IN_PROGRESS']" (install drill). Poll
# briefly; a FAILED rollout (circuit breaker) or a count mismatch still aborts.
verify_rollout() { # -> 0 complete, 2 still finalizing, 1 unhealthy
  local state
  state=$(aws ecs describe-services --region "$AWS_REGION" --cluster marshal \
    --services marshal-backend marshal-frontend \
    --query 'services[].{name:serviceName,desired:desiredCount,running:runningCount,pending:pendingCount,rollouts:deployments[].rolloutState}' \
    --output json)
  python3 - "$state" <<'PY'
import json, sys
rows = json.loads(sys.argv[1])
if {row["name"] for row in rows} != {"marshal-backend", "marshal-frontend"}:
    raise SystemExit("expected both ECS services in rollout verification")
for row in rows:
    if row["running"] != row["desired"] or row["pending"] != 0:
        raise SystemExit(f"{row['name']} is not stable: {row}")
    if not row["rollouts"] or "FAILED" in row["rollouts"]:
        raise SystemExit(f"{row['name']} rollout failed: {row}")
if any(state != "COMPLETED" for row in rows for state in row["rollouts"]):
    print("rollout still finalizing: " + ", ".join(f"{r['name']}={r['rollouts']}" for r in rows))
    sys.exit(2)
print(json.dumps(rows, indent=2, sort_keys=True))
PY
}
for ((attempt=1; attempt<=18; attempt++)); do
  verify_rollout && break
  rc=$?
  [[ $rc -eq 2 ]] || exit 1
  [[ $attempt -lt 18 ]] || { echo "ERROR: ECS rollout not COMPLETED after $attempt checks" >&2; exit 1; }
  sleep 10
done

probe() {
  local label=$1 url=$2 attempt
  for ((attempt=1; attempt<=12; attempt++)); do
    if curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
      --output /dev/null "$url"; then
      echo "$label: passed (attempt $attempt)"
      return 0
    fi
    sleep 5
  done
  echo "ERROR: $label failed after 12 attempts: $url" >&2
  return 1
}

probe_health() {
  local url=$1 attempt body
  for ((attempt=1; attempt<=12; attempt++)); do
    if body=$(curl --fail --silent --show-error --connect-timeout 5 --max-time 20 "$url") \
      && python3 - "$body" <<'PY'
import json, sys
body = json.loads(sys.argv[1])
if body.get("status") != "ok" or body.get("checks", {}).get("database") != "ok":
    raise SystemExit(1)
PY
    then
      echo "healthz: passed (attempt $attempt) — $body"
      return 0
    fi
    sleep 5
  done
  echo "ERROR: healthz failed after 12 attempts: $url" >&2
  return 1
}

step "7/7 Basic deployment smoke (fail-closed)"
probe "landing" "$CLOUDFRONT_URL/"
probe_health "$CLOUDFRONT_URL/healthz"
echo "Authenticated browser acceptance is separate and still required (README 'Acceptance smoke'):"
echo "  APP_URL='$CLOUDFRONT_URL' SMOKE_ADMIN_EMAIL=admin@marshal.demo DEMO_USER_PASSWORD='…' node scripts/ui-smoke.mjs"
echo
echo "DONE. Basic deploy health passed. App: $CLOUDFRONT_URL"
