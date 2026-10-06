#!/usr/bin/env bash
# Build the PUBLIC fresh-history snapshot of marshal.
#
#   usage: PUBLIC_GIT_EMAIL=<public address> [PUBLIC_GIT_NAME=marshal.build] \
#          scripts/export-public-snapshot.sh <output-dir> [--dry-run] [--allow-dirty] [--no-literal-list]
#
#   PUBLIC_GIT_EMAIL is REQUIRED: it is the author/committer of the single public
#   commit (git would otherwise use the maintainer's private global identity).
#   Both identity strings are checked against the literal gate before anything runs.
#
# What it does, in order:
#   0. Refuses to run on a dirty checkout (`git status --porcelain` non-empty:
#      modified, staged or untracked files) unless --allow-dirty is passed. The
#      snapshot is taken from HEAD either way — the working tree is never read.
#   1. Extracts `git archive HEAD` into a temporary directory, lists the files
#      tracked at HEAD (gitignored material is never a candidate) and drops
#      everything matching scripts/public-export-denylist.txt.
#   2. Runs the IDENTIFIER GATE over the remaining files: generic shapes for
#      AWS account ids, Cognito pool ids, CloudFront / API Gateway hosts,
#      Organizations ids, ACM certificate ARNs, the maintainer's handle,
#      beta-tester names and phone numbers, PLUS the literals in the gitignored
#      scripts/export-gate.local.txt (one per line). That list carries the
#      installation values no shape can catch, so its ABSENCE is an error
#      unless --no-literal-list is passed explicitly (a loud warning then).
#      Hits are printed as path:line:<shape> — never the matching text or line.
#   3. Without --dry-run: copies the files into <output-dir> (which must not
#      exist or must be empty), re-runs the gate on the copy, and only if it is
#      clean runs `git init -b main`, `git add -A` and a single commit
#      "marshal.build v0.1.0 — first public source release" whose body names
#      the source commit. Prints a tree summary and the file count.
#
# Exit codes: 0 = snapshot written (or dry-run clean) · 2 = gate hit(s) · 1 = usage/error.
# On a gate hit in a real run the copied tree is LEFT IN PLACE for inspection
# but is NOT a git repository — do not publish it.
#
# Requires: bash, git >= 2.28, tar, python3 (same prerequisites as the deploy scripts).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DENYLIST="$SCRIPT_DIR/public-export-denylist.txt"
LOCAL_LITERALS="$SCRIPT_DIR/export-gate.local.txt"
COMMIT_MESSAGE="marshal.build v0.1.0 — first public source release"

usage() {
  echo "usage: $(basename "$0") <output-dir> [--dry-run] [--allow-dirty] [--no-literal-list]" >&2
  exit 1
}

OUT_DIR=""
DRY_RUN=0
ALLOW_DIRTY=0
NO_LITERAL_LIST=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --no-literal-list) NO_LITERAL_LIST=1 ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $arg" >&2; usage ;;
    *) [[ -z "$OUT_DIR" ]] || usage; OUT_DIR="$arg" ;;
  esac
done
[[ -n "$OUT_DIR" ]] || usage
[[ -f "$DENYLIST" ]] || { echo "ERROR: denylist missing: $DENYLIST" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 is required" >&2; exit 1; }
command -v tar >/dev/null || { echo "ERROR: tar is required" >&2; exit 1; }
git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "ERROR: $REPO_ROOT is not a git checkout" >&2; exit 1; }

# The literal list is the only defence for identifiers without a shape
# (client ids, ISB ids, RDS prefixes, names). Never run without it silently.
if [[ ! -f "$LOCAL_LITERALS" ]]; then
  if [[ "$NO_LITERAL_LIST" == "1" ]]; then
    echo "WARNING: $LOCAL_LITERALS is absent — the identifier gate runs on generic" >&2
    echo "         shapes ONLY (--no-literal-list given). Installation literals with no" >&2
    echo "         shape (client ids, ISB ids, RDS prefixes, names) are NOT checked." >&2
  else
    echo "ERROR: $LOCAL_LITERALS is absent. The identifier gate would run on generic" >&2
    echo "       shapes only and miss every installation literal that has no shape." >&2
    echo "       Create the list (one literal per line), or pass --no-literal-list to" >&2
    echo "       accept that explicitly." >&2
    exit 1
  fi
fi

# The ONE public commit carries an author/committer identity, and git takes it
# from the maintainer's global config — i.e. the private work address the whole
# snapshot exists to keep out of the public history. Require an explicit public
# identity instead (PUBLIC_GIT_EMAIL, e.g. the GitHub noreply address from
# Settings -> Emails) and run it through the same literal gate as file content.
PUBLIC_GIT_NAME="${PUBLIC_GIT_NAME:-marshal.build}"
if [[ -z "${PUBLIC_GIT_EMAIL:-}" ]]; then
  echo "ERROR: PUBLIC_GIT_EMAIL is not set. The snapshot commit would be authored with" >&2
  echo "       your global git identity ($(git config --get user.email || echo '<unset>'))." >&2
  echo "       Set PUBLIC_GIT_EMAIL (and optionally PUBLIC_GIT_NAME, default" >&2
  echo "       'marshal.build') to the identity the PUBLIC commit should carry, e.g." >&2
  echo "       PUBLIC_GIT_EMAIL='<id>+<user>@users.noreply.github.com' $(basename "$0") ..." >&2
  exit 1
fi
if [[ -f "$LOCAL_LITERALS" ]]; then
  identity_lc="$(printf '%s <%s>' "$PUBLIC_GIT_NAME" "$PUBLIC_GIT_EMAIL" | tr '[:upper:]' '[:lower:]')"
  while IFS= read -r lit; do
    [[ -n "$lit" && "$lit" != \#* ]] || continue
    lit_lc="$(printf '%s' "$lit" | tr '[:upper:]' '[:lower:]')"
    if [[ "$identity_lc" == *"$lit_lc"* ]]; then
      echo "ERROR: the public git identity contains a gated literal; refusing." >&2
      echo "       (PUBLIC_GIT_NAME / PUBLIC_GIT_EMAIL must not carry installation or" >&2
      echo "       maintainer identifiers — use a public-safe name and address.)" >&2
      exit 1
    fi
  done < "$LOCAL_LITERALS"
fi
echo "author : $PUBLIC_GIT_NAME <$PUBLIC_GIT_EMAIL> (public snapshot identity)"

# Snapshot HEAD, not the working tree: uncommitted edits must never ship and
# the provenance line below must be true. --allow-dirty only lifts the refusal;
# the content still comes from HEAD.
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  if [[ "$ALLOW_DIRTY" == "1" ]]; then
    echo "WARNING: working tree is dirty; exporting HEAD regardless (--allow-dirty)." >&2
  else
    echo "ERROR: working tree is dirty (git status --porcelain is non-empty)." >&2
    echo "       Commit or stash first — the snapshot is taken from HEAD, so" >&2
    echo "       uncommitted work would be silently left out. --allow-dirty overrides." >&2
    exit 1
  fi
fi

if [[ "$DRY_RUN" == "0" ]]; then
  if [[ -e "$OUT_DIR" ]]; then
    [[ -d "$OUT_DIR" ]] || { echo "ERROR: $OUT_DIR exists and is not a directory" >&2; exit 1; }
    [[ -z "$(ls -A "$OUT_DIR")" ]] || { echo "ERROR: $OUT_DIR is not empty" >&2; exit 1; }
  fi
  mkdir -p "$OUT_DIR"
  OUT_DIR="$(cd "$OUT_DIR" && pwd)"
fi

SOURCE_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
SOURCE_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
echo "source : $REPO_ROOT ($SOURCE_BRANCH @ ${SOURCE_COMMIT:0:12}, HEAD content — not the working tree)"
echo "mode   : $([[ "$DRY_RUN" == "1" ]] && echo dry-run || echo "write -> $OUT_DIR")"

# Materialize HEAD's tree once; every read below (gate, copy) comes from here.
SRC_TREE="$(mktemp -d "${TMPDIR:-/tmp}/marshal-export-src.XXXXXX")"
cleanup() { rm -rf "$SRC_TREE"; }
trap cleanup EXIT
git -C "$REPO_ROOT" archive --format=tar HEAD | tar -x -C "$SRC_TREE"

# Select (git ls-tree HEAD), copy (unless dry-run) and gate. The Python half
# owns all pattern logic so the shapes are applied identically in every mode.
GATE_STATUS=0
python3 - "$REPO_ROOT" "$SRC_TREE" "$DENYLIST" "$LOCAL_LITERALS" "$DRY_RUN" "$OUT_DIR" <<'PY' || GATE_STATUS=$?
import os, re, shutil, subprocess, sys

repo_root, src_tree, denylist_path, literals_path, dry_run, out_dir = sys.argv[1:7]
dry_run = dry_run == "1"
listing = subprocess.run(
    ["git", "-C", repo_root, "ls-tree", "-r", "-z", "--name-only", "HEAD"],
    check=True, capture_output=True,
).stdout
files = [f for f in listing.decode("utf-8", "surrogateescape").split("\0") if f]

# ------------------------------------------------------------------ denylist
def pattern_to_regex(pat: str) -> re.Pattern:
    # `**` spans directories, `*` stays within one segment; a pattern without
    # `/` is a basename match anywhere (gitignore semantics).
    anchored = "/" in pat
    out, i = "", 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out += "(?:.*/)?"; i += 3; continue
        if pat.startswith("**", i):
            out += ".*"; i += 2; continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    return re.compile(("^" if anchored else "(^|.*/)") + out + "$")

patterns = []
with open(denylist_path, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append((line, pattern_to_regex(line)))

included, excluded = [], []
for f in files:
    hit = next((p for p, rx in patterns if rx.match(f)), None)
    (excluded if hit else included).append((f, hit))

print(f"tracked: {len(files)} · excluded by denylist: {len(excluded)} · included: {len(included)}")
print("excluded files:")
for f, p in sorted(excluded):
    print(f"  - {f}    [{p}]")

# ------------------------------------------------------------------ copy
# Sources are the extracted HEAD archive, never the working tree.
if not dry_run:
    for f, _ in included:
        src = os.path.join(src_tree, f)
        if not os.path.isfile(src):  # not a regular file at HEAD (symlink, gitlink)
            continue
        dst = os.path.join(out_dir, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    base = out_dir
else:
    base = src_tree

# ------------------------------------------------------------------ gate
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# Documentation placeholders and AWS's own example ids; repeated digits are
# handled separately below.
ACCOUNT_ALLOW = {"123456789012", "111122223333", "210987654321"}
TIMESTAMP_RE = re.compile(r"^20[2-9][0-9](0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])[0-9]{4}$")  # YYYYMMDDhhmm
TEXT_EXT_FOR_PHONE = {
    ".md", ".tsx", ".ts", ".py", ".html",
    ".mjs", ".sh", ".yml", ".yaml", ".json", ".txt",
}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip"}

def repeated(s: str) -> bool:
    return len(set(s)) == 1

def account_hits(line):
    scrubbed = UUID_RE.sub("<uuid>", line)
    for m in re.finditer(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])", scrubbed):
        v = m.group(0)
        # 12345678xxxx covers the documentation placeholder and sequential
        # fixtures derived from it; real account ids are never that regular.
        if v in ACCOUNT_ALLOW or v.startswith("12345678") or repeated(v) or TIMESTAMP_RE.match(v):
            continue
        yield "aws-account-id"

def pool_hits(line):
    for m in re.finditer(r"us-east-1_([A-Za-z0-9]{9})(?![A-Za-z0-9])", line):
        if repeated(m.group(1)):
            continue
        yield "cognito-user-pool-id"

def cloudfront_hits(line):
    for m in re.finditer(r"(?<![a-z0-9])([a-z0-9]{14})\.cloudfront\.net", line):
        if repeated(m.group(1)):
            continue
        yield "cloudfront-host"

def apigw_hits(line):
    for m in re.finditer(r"(?<![a-z0-9])([a-z0-9]{10})\.execute-api\.[a-z0-9-]+\.amazonaws\.com", line):
        if repeated(m.group(1)):
            continue
        yield "api-gateway-host"

def org_hits(line):
    for _ in re.finditer(r"(?<![A-Za-z0-9])o-[a-z0-9]{10}(?![a-z0-9])", line):
        yield "aws-organization-id"

# Assembled at runtime so this script's own source never trips the gate.
ACM_ARN_PREFIX = ":".join(("arn", "aws", "acm", ""))
MAINTAINER_HANDLE = "".join(("aws", "shen"))

def acm_hits(line):
    if ACM_ARN_PREFIX in line:
        yield "acm-certificate-arn"

def handle_hits(line):
    if MAINTAINER_HANDLE in line.lower():
        yield "maintainer-handle"

def tester_hits(line):
    for _ in re.finditer(r"(?i)tester[1-5](?![0-9])", line):
        yield "beta-tester-name"

def phone_hits(line):
    # Shape from the audit: \+?\d[\d \-]{8,}\d. Filtered so that dates, model
    # ids (…-haiku-4-5-20251001-v1), UUIDs, PDF/geometry numbers and the
    # classic sequential test fixtures do not fire; real phone numbers (8–13
    # digits, at least one run of four) do.
    scrubbed = UUID_RE.sub("<uuid>", line)
    # The anchors keep the match out of identifier tokens (…-haiku-4-5-20251001-v1).
    for m in re.finditer(r"(?<![0-9A-Za-z._/\-+])\+?\d[\d \-]{8,}\d(?![0-9A-Za-z._/\-])", scrubbed):
        v = m.group(0)
        digits = re.sub(r"\D", "", v)
        if not 8 <= len(digits) <= 13:
            continue                                        # too short, or a card/timestamp run
        if re.search(r"\d{4}-\d{2}-\d{2}", v):
            continue                                        # date or date-time
        if len(digits) == 12 and digits == v:
            continue                                        # account-id shape, handled above
        if digits.startswith("1234567") or digits in {"123456789", "1234567890"}:
            continue                                        # sequential fixture
        if not re.search(r"\d{4}", v):
            continue                                        # no run of four digits anywhere
        yield "phone-number-shape"

literals = []
if os.path.isfile(literals_path):
    with open(literals_path, encoding="utf-8") as fh:
        for raw in fh:
            s = raw.strip()
            if s and not s.startswith("#"):
                literals.append(s.lower())

def gate_file(rel: str):
    path = os.path.join(base, rel)
    ext = os.path.splitext(rel)[1].lower()
    if ext in BINARY_EXT or not os.path.isfile(path):
        return
    with open(path, "rb") as fh:
        raw = fh.read()
    if b"\0" in raw[:8000]:
        return
    text = raw.decode("utf-8", "replace")
    phone_ok = ext in TEXT_EXT_FOR_PHONE
    for no, line in enumerate(text.splitlines(), 1):
        shapes = []
        for fn in (account_hits, pool_hits, cloudfront_hits, apigw_hits, org_hits, acm_hits, handle_hits, tester_hits):
            shapes.extend(fn(line))
        if phone_ok:
            shapes.extend(phone_hits(line))
        low = line.lower()
        for i, lit in enumerate(literals, 1):
            if lit in low:
                shapes.append(f"local-literal#{i}(len={len(lit)})")
        for s in shapes:
            yield f"{rel}:{no}:{s}"

hits = []
for f, _ in included:
    hits.extend(gate_file(f))

print()
if literals:
    print(f"identifier gate: {len(literals)} local literal(s) loaded from {os.path.basename(literals_path)}")
else:
    # The shell half already refused or warned; repeat it next to the verdict
    # so a PASS below can never read as a full check.
    print(f"identifier gate: GENERIC SHAPES ONLY — no local literal list ({os.path.basename(literals_path)} absent)")
    print(f"WARNING: identifier gate ran without {os.path.basename(literals_path)}; installation literals unchecked",
          file=sys.stderr)
if hits:
    print(f"identifier gate: FAIL — {len(hits)} hit(s) in {len({h.split(':')[0] for h in hits})} file(s)")
    for h in hits:
        print(f"  {h}")
    sys.exit(2)
print("identifier gate: PASS — 0 hits")
PY
if [[ "$GATE_STATUS" != "0" ]]; then
  if [[ "$DRY_RUN" == "0" ]]; then
    echo
    echo "Snapshot NOT initialized: identifier gate failed. The copied tree was left at" >&2
    echo "  $OUT_DIR" >&2
    echo "for inspection; it is NOT a git repository. Fix the sources and re-run." >&2
  fi
  exit 2
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo
  echo "dry-run complete: nothing written."
  exit 0
fi

echo
echo "initializing fresh-history repository"
git -C "$OUT_DIR" init -q -b main
# Public identity for this commit AND for anything committed later from this
# checkout (e.g. the cla-signatures branch) — never the global config.
git -C "$OUT_DIR" config user.name "$PUBLIC_GIT_NAME"
git -C "$OUT_DIR" config user.email "$PUBLIC_GIT_EMAIL"
git -C "$OUT_DIR" add -A
# Provenance travels with the snapshot: the private commit it was cut from.
GIT_AUTHOR_NAME="$PUBLIC_GIT_NAME" GIT_AUTHOR_EMAIL="$PUBLIC_GIT_EMAIL" \
GIT_COMMITTER_NAME="$PUBLIC_GIT_NAME" GIT_COMMITTER_EMAIL="$PUBLIC_GIT_EMAIL" \
git -C "$OUT_DIR" commit -q -m "$COMMIT_MESSAGE" \
  -m "Exported from the marshal.build source repository at ${SOURCE_BRANCH} @ ${SOURCE_COMMIT}."
echo "author : $(git -C "$OUT_DIR" log -1 --format='%an <%ae>')"
echo "commit : $(git -C "$OUT_DIR" rev-parse --short=12 HEAD) — $COMMIT_MESSAGE"
echo "         (source ${SOURCE_BRANCH} @ ${SOURCE_COMMIT})"
echo
echo "tree summary ($OUT_DIR):"
git -C "$OUT_DIR" ls-files | awk -F/ '{ if (NF == 1) print "(root)"; else print $1 }' \
  | sort | uniq -c | sort -rn | awk '{ printf "  %5d  %s\n", $1, $2 }'
echo "files  : $(git -C "$OUT_DIR" ls-files | wc -l | tr -d ' ')"
echo "DONE. Snapshot at $OUT_DIR (branch main, 1 commit)."
