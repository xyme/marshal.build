#!/usr/bin/env bash
# Local mirror of .github/workflows/ci.yml — "CI green" evidence until a remote exists.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

step() { printf "\n\033[1;36m=== %s ===\033[0m\n" "$1"; }

step "frontend: lint + typecheck + build"
(cd frontend && npm run lint && npx tsc --noEmit && npm run build)

step "backend: ruff + pytest"
(cd backend && uv run ruff check . && uv run pytest -q)

step "infra: typecheck + synth"
(cd infra && npm run build && npx cdk synth --quiet > /dev/null)

step "sample-app: typecheck + synth + template freshness"
(cd sample-app && npm run build && npm run synth > /dev/null && git diff --quiet -- template.json \
  || { echo "sample-app/template.json changed — review & commit"; })

printf "\n\033[1;32mALL CHECKS GREEN\033[0m\n"
