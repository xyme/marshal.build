#!/usr/bin/env bash
# Generates backend/.env and frontend/.env.local from deployed stack outputs
# + Secrets Manager. Never commits secrets (both files are gitignored).
# Honors AWS_PROFILE (e.g. AWS_PROFILE=<your-profile> ./scripts/sync-env.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_REGION=${AWS_REGION:-us-east-1}
if [[ -n "${AWS_PROFILE:-}" ]]; then echo "Using AWS_PROFILE=$AWS_PROFILE"; fi

outputs() { # stack, key
  aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

echo "Reading stack outputs…"
USER_POOL_ID=$(outputs MarshalAuthStack UserPoolId)
CLIENT_ID=$(outputs MarshalAuthStack UserPoolClientId)
HOSTED_UI=$(outputs MarshalAuthStack HostedUiDomain)
ISSUER=$(outputs MarshalAuthStack CognitoIssuer)
CHAT_TABLE=$(outputs MarshalDataStack ChatTableName)
DB_ENDPOINT=$(outputs MarshalDataStack DbEndpoint)
DB_SECRET_ARN=$(outputs MarshalDataStack DbSecretArn)

echo "Reading client secret + DB credentials…"
CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client --region "$AWS_REGION" \
  --user-pool-id "$USER_POOL_ID" --client-id "$CLIENT_ID" \
  --query "UserPoolClient.ClientSecret" --output text)
DB_JSON=$(aws secretsmanager get-secret-value --region "$AWS_REGION" \
  --secret-id "$DB_SECRET_ARN" --query SecretString --output text)
DB_USER=$(printf '%s' "$DB_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['username'])")
DB_PASS=$(printf '%s' "$DB_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['password'])")

AUTH_SECRET_FILE=".auth-secret"
if [[ ! -f $AUTH_SECRET_FILE ]]; then openssl rand -base64 32 > "$AUTH_SECRET_FILE"; fi
AUTH_SECRET=$(cat "$AUTH_SECRET_FILE")

# Optional ISB wiring — set by the ISB deployment flow
ISB_API_BASE_URL_VALUE=${ISB_API_BASE_URL_VALUE:-}
SANDBOX_PROVIDER_VALUE=${SANDBOX_PROVIDER_VALUE:-direct}
ISB_BLUEPRINT_ID_VALUE=${ISB_BLUEPRINT_ID_VALUE:-}

cat > backend/.env <<EOF
AWS_REGION=$AWS_REGION
ENVIRONMENT=dev
COGNITO_USER_POOL_ID=$USER_POOL_ID
COGNITO_CLIENT_ID=$CLIENT_ID
DATABASE_URL=postgresql+asyncpg://$DB_USER:$DB_PASS@$DB_ENDPOINT:5432/marshal
DYNAMO_TABLE_CHAT=$CHAT_TABLE
BEDROCK_MODEL_CHAT=us.anthropic.claude-sonnet-5
BEDROCK_MODEL_SPEC=us.anthropic.claude-sonnet-5
BEDROCK_MODEL_DESIGN=us.anthropic.claude-sonnet-5
BEDROCK_MODEL_FAST=us.anthropic.claude-haiku-4-5-20251001-v1:0
SANDBOX_PROVIDER=$SANDBOX_PROVIDER_VALUE
ISB_API_BASE_URL=$ISB_API_BASE_URL_VALUE
ISB_BLUEPRINT_ID=$ISB_BLUEPRINT_ID_VALUE
ISB_LEASE_DURATION_HOURS=720
ISB_LEASE_BUDGET_USD=1000
SAMPLE_APP_TEMPLATE_PATH=../sample-app/template.json
EOF
echo "wrote backend/.env"

cat > frontend/.env.local <<EOF
AUTH_SECRET=$AUTH_SECRET
AUTH_COGNITO_ID=$CLIENT_ID
AUTH_COGNITO_SECRET=$CLIENT_SECRET
AUTH_COGNITO_ISSUER=$ISSUER
COGNITO_DOMAIN=$HOSTED_UI
BACKEND_URL=http://localhost:8000
NEXT_PUBLIC_COGNITO_DOMAIN=$HOSTED_UI
NEXT_PUBLIC_COGNITO_CLIENT_ID=$CLIENT_ID
AUTH_TRUST_HOST=true
EOF
echo "wrote frontend/.env.local"
echo "Done."
