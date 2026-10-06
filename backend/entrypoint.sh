#!/bin/sh
# Backend container entrypoint:
#  1. Compose DATABASE_URL from ECS-injected DB secret parts (DB_SECRET_JSON + DB_HOST)
#     unless DATABASE_URL is provided directly (local/dev usage).
#  2. Apply alembic migrations (idempotent).
#  3. Start uvicorn.
set -e

if [ -z "$DATABASE_URL" ] && [ -n "$DB_SECRET_JSON" ]; then
  export DATABASE_URL=$(python - <<'PY'
import json
import os
import urllib.parse

secret = json.loads(os.environ["DB_SECRET_JSON"])
user = secret["username"]
password = urllib.parse.quote(secret["password"], safe="")
host = os.environ.get("DB_HOST") or secret.get("host")
port = os.environ.get("DB_PORT") or str(secret.get("port", 5432))
db = os.environ.get("DB_NAME", "marshal")
print(f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}")
PY
)
fi

echo "Applying database migrations..."
alembic upgrade head

# One-off task support (ECS run-task command overrides): exec the override
# instead of the server, e.g. ["python","-c","...seed..."]
if [ "$#" -gt 0 ]; then
  echo "Running override command: $*"
  exec "$@"
fi

echo "Starting marshal API on :8000"
# keep-alive MUST outlive the Service Connect envoy idle timeout (240s in AppStack):
# envoy pools upstream connections; if uvicorn closes first (default 5s), pooled
# requests hit dead sockets -> ECONNRESET at the frontend proxy.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 300
