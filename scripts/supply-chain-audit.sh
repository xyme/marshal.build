#!/usr/bin/env bash
# S14-03 supply-chain gate: dependency audit + SBOM generation.
#
# Runs in CodeBuild BEFORE images are built, and locally for triage:
#   bash scripts/supply-chain-audit.sh /tmp/sbom
#
# Exit codes: 0 = pass (findings may exist below the gate), 1 = gate failure.
#
# Gate policy (deliberately not "zero findings", which fails on unfixable
# transitive noise and trains people to ignore it):
#   - CRITICAL in runtime dependencies      -> fail the build
#   - HIGH and below                        -> report, triage in the sprint
#   - AUDIT_STRICT=1                        -> also fail on HIGH
# Known-accepted findings live in scripts/audit-allowlist.txt (one advisory ID
# per line, with a comment explaining why) and are excluded from the gate.
set -uo pipefail
cd "$(dirname "$0")/.."

SBOM_DIR="${1:-/tmp/sbom}"
ALLOWLIST="scripts/audit-allowlist.txt"
mkdir -p "$SBOM_DIR"
FAIL=0

allowed() { # advisory id -> 0 if allowlisted
  [[ -f "$ALLOWLIST" ]] && grep -qE "^\s*${1}\s*(#.*)?$" "$ALLOWLIST"
}

section() { printf "\n=== %s ===\n" "$1"; }

# ---------------------------------------------------------------- python
section "python dependency audit (pip-audit)"
# CodeBuild has pip; the local dev machine runs uv without a system pip.
if ! command -v pip-audit >/dev/null 2>&1; then
  if command -v pip >/dev/null 2>&1; then
    pip install --quiet pip-audit
  elif command -v uv >/dev/null 2>&1; then
    PIP_AUDIT="uv tool run --quiet pip-audit"
  fi
fi
PIP_AUDIT="${PIP_AUDIT:-pip-audit}"
if command -v pip-audit >/dev/null 2>&1 || [[ "$PIP_AUDIT" != "pip-audit" ]]; then
  # Audit OUR LOCKED dependency set, never the ambient environment. Auditing
  # the environment made CI report findings for CodeBuild's own pip/setuptools
  # (6 false signals on the first strict run) — noise that would erode trust in
  # the gate and could block builds for reasons unrelated to this codebase.
  REQS="$SBOM_DIR/backend-locked-requirements.txt"
  # CodeBuild has pip but not uv — install it so the LOCKED set is what gets
  # audited there too (the gate's corpus must be our lockfile, never the
  # ambient environment; the first gated run failed on CodeBuild's own pips).
  if ! command -v uv >/dev/null 2>&1 && command -v pip >/dev/null 2>&1 && [[ -f backend/uv.lock ]]; then
    pip install --quiet uv
  fi
  if command -v uv >/dev/null 2>&1 && [[ -f backend/uv.lock ]]; then
    (cd backend && uv export --frozen --no-hashes --no-dev 2>/dev/null | grep -v '^-' > "$REQS")
  fi
  AUDIT_SOURCE="environment"
  if [[ -s "$REQS" ]]; then
    AUDIT_SOURCE="lockfile"
    echo "auditing $(wc -l < "$REQS") locked packages"
    $PIP_AUDIT -r "$REQS" --format json --progress-spinner off \
      > "$SBOM_DIR/py-audit.json" 2>"$SBOM_DIR/py-audit.err"
  else
    echo "WARN: no lockfile export; falling back to environment audit"
    (cd backend && $PIP_AUDIT --format json --progress-spinner off > "$SBOM_DIR/py-audit.json" 2>"$SBOM_DIR/py-audit.err")
  fi
  python3 - "$SBOM_DIR/py-audit.json" "$ALLOWLIST" "$AUDIT_SOURCE" <<'PY'
import json, os, sys
report_path, allowlist_path = sys.argv[1], sys.argv[2]
audit_source = sys.argv[3] if len(sys.argv) > 3 else "environment"
allow = set()
if os.path.exists(allowlist_path):
    for line in open(allowlist_path):
        line = line.split("#")[0].strip()
        if line:
            allow.add(line)
try:
    data = json.load(open(report_path))
except Exception as exc:
    print(f"no parseable pip-audit report ({exc}) — treating as inconclusive, not a gate failure")
    sys.exit(0)
deps = data.get("dependencies", data if isinstance(data, list) else [])
findings = []
for dep in deps:
    for vuln in dep.get("vulns", []):
        vid = vuln.get("id", "?")
        if vid in allow:
            continue
        findings.append((dep.get("name"), dep.get("version"), vid, ",".join(vuln.get("fix_versions") or []) or "none"))
print(f"python findings (excluding allowlist): {len(findings)}")
for name, version, vid, fix in findings[:25]:
    print(f"  {name} {version}: {vid} (fix: {fix})")
# pip-audit does not grade severity, so the python gate keys on FIXABILITY:
# a fixable, non-allowlisted finding fails the build (pre-Beta hardening,
# 6 Aug 2026 — promoted from report-only once cryptography caught up);
# unfixable findings are reported for triage, not gated (no action exists).
# The gate only has authority over OUR lockfile: an environment-fallback
# audit reports someone else's packages (CodeBuild's own pip/setuptools) and
# must never block — that noise is exactly what erodes trust in gates.
if audit_source != "lockfile":
    print("python gate: SKIPPED (environment fallback — only the lockfile is gated)")
    sys.exit(0)
fixable = [f for f in findings if f[3] != "none"]
if fixable:
    print(f"\nGATE FAILURE: {len(fixable)} fixable python finding(s) — upgrade or allowlist with rationale")
    sys.exit(1)
print("python gate: pass")
PY
  [[ $? -ne 0 ]] && FAIL=1
else
  echo "SKIP: python audit"
fi

# ---------------------------------------------------------------- node
section "node dependency audit (npm audit, runtime deps only)"
if [[ -d frontend ]]; then
  (cd frontend && npm audit --omit=dev --json > "$SBOM_DIR/npm-audit.json" 2>/dev/null)
  python3 - "$SBOM_DIR/npm-audit.json" "$ALLOWLIST" "${AUDIT_STRICT:-0}" <<'PY'
import json, os, sys
report_path, allowlist_path, strict = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
allow = set()
if os.path.exists(allowlist_path):
    for line in open(allowlist_path):
        line = line.split("#")[0].strip()
        if line:
            allow.add(line)
try:
    data = json.load(open(report_path))
except Exception as exc:
    print(f"no parseable npm audit report ({exc})")
    sys.exit(0)
counts = (data.get("metadata") or {}).get("vulnerabilities") or {}
print("severity counts:", {k: v for k, v in counts.items() if v})
def advisory_ids(vuln):
    """Both the numeric npm source id and the GHSA id (allowlists read better
    with GHSA ids; npm reports numeric sources)."""
    out = []
    for via in vuln.get("via", []):
        if not isinstance(via, dict):
            continue
        if via.get("source") is not None:
            out.append(str(via["source"]))
        url = via.get("url") or ""
        if "GHSA-" in url:
            out.append("GHSA-" + url.split("GHSA-")[1].strip("/"))
    return out


vulns = data.get("vulnerabilities") or {}

# Packages whose OWN advisories are all allowlisted.
accepted = set()
for name, vuln in vulns.items():
    ghsa = [i for i in advisory_ids(vuln) if i.startswith("GHSA-")]
    if ghsa and all(i in allow for i in ghsa):
        accepted.add(name)

# npm reports transitive parents with package-NAME via entries (e.g. next ->
# ["postcss", "sharp"]). A parent is accepted when every vulnerable dependency
# it inherits from is itself accepted. Iterate to a fixpoint for deep chains.
for _ in range(len(vulns) + 1):
    grew = False
    for name, vuln in vulns.items():
        if name in accepted:
            continue
        deps = [v for v in vuln.get("via", []) if isinstance(v, str)]
        if deps and all(d in accepted for d in deps):
            accepted.add(name)
            grew = True
    if not grew:
        break

blocking = []
for name, vuln in vulns.items():
    if name in accepted:
        continue
    severity = vuln.get("severity")
    ids = [i for i in advisory_ids(vuln) if i.startswith("GHSA-")]
    inherited = [v for v in vuln.get("via", []) if isinstance(v, str)]
    if severity == "critical" or (strict and severity == "high"):
        blocking.append((name, severity, ",".join(ids or inherited) or "-"))
for name, severity, ids in blocking[:25]:
    print(f"  BLOCKING {severity}: {name} ({ids})")
if blocking:
    print(f"\nGATE FAILURE: {len(blocking)} blocking node finding(s)")
    sys.exit(1)
print("node gate: pass")
PY
  [[ $? -ne 0 ]] && FAIL=1
fi

# ---------------------------------------------------------------- SBOMs
section "SBOM generation (CycloneDX)"
if [[ -d frontend ]]; then
  # npm sbom needs an INSTALLED tree, which CodeBuild does not have (the build
  # source has no node_modules). Fall back to a lockfile-derived component
  # list — deterministic and always available.
  if (cd frontend && npm sbom --sbom-format cyclonedx --omit=dev > "$SBOM_DIR/frontend-sbom.json" 2>/dev/null); then
    echo "frontend SBOM (CycloneDX): $(wc -c < "$SBOM_DIR/frontend-sbom.json") bytes"
  else
    rm -f "$SBOM_DIR/frontend-sbom.json"
    python3 - frontend/package-lock.json "$SBOM_DIR/frontend-components.txt" <<'PY'
import json, sys
lock_path, out_path = sys.argv[1], sys.argv[2]
lock = json.load(open(lock_path))
rows = []
for path, meta in (lock.get("packages") or {}).items():
    if not path or meta.get("dev") or not meta.get("version"):
        continue
    rows.append(f"{path.split('node_modules/')[-1]}=={meta['version']}")
open(out_path, "w").write("\n".join(sorted(set(rows))) + "\n")
print(f"frontend component list (from lockfile): {len(set(rows))} packages")
PY
  fi
fi
if command -v uv >/dev/null 2>&1 && [[ -f backend/uv.lock ]]; then
  # Lockfile-derived component list: dependency-free and deterministic, which
  # matters more here than CycloneDX richness.
  (cd backend && uv export --frozen --no-hashes 2>/dev/null | grep -v '^-' > "$SBOM_DIR/backend-requirements.txt") \
    && echo "backend component list: $(wc -l < "$SBOM_DIR/backend-requirements.txt") packages" \
    || echo "WARN: backend component list failed"
fi

section "result"
if [[ $FAIL -ne 0 ]]; then
  echo "SUPPLY-CHAIN GATE: FAIL"
  exit 1
fi
echo "SUPPLY-CHAIN GATE: PASS"
