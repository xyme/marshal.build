"""Validation gate on generated artifacts (codegen-handoff spec R3).

Pure-local, deterministic, no model or AWS calls. ANY finding fails the build
— there is no "deploy anyway" at Alpha. Checks:
  template_json      — parses as a CloudFormation JSON document
  service_allowlist  — every resource type within the §4.1.4-derived prefix set
  inline_packaging   — Lambdas inline (ZipFile ≤ MAX_INLINE), allowed runtime
  guardrail_patterns — template §4.2 forbidden patterns over all artifacts
  deploy_contract    — ApiUrl output present; template ≤ CFN body limit;
                       source files referenced by the plan exist
"""

import json
import re
from dataclasses import asdict, dataclass

# §4.1.4 allowlist projected onto CloudFormation type prefixes. IAM roles/
# policies are wiring, not services — the real isolation boundary is the
# sandbox account + scoped cross-account role (spec scope stance).
ALLOWED_TYPE_PREFIXES = (
    "AWS::Lambda::",
    "AWS::ApiGateway::",
    "AWS::ApiGatewayV2::",
    "AWS::DynamoDB::",
    "AWS::S3::",
    "AWS::SQS::",
    "AWS::SNS::",
    "AWS::StepFunctions::",
    "AWS::Events::",
    "AWS::Logs::",
    "AWS::CloudWatch::",
    "AWS::KMS::",
    "AWS::Cognito::",
    "AWS::IAM::Role",
    "AWS::IAM::Policy",
    "AWS::IAM::ManagedPolicy",
)
ALLOWED_RUNTIMES = ("python3.12", "python3.13")
MAX_INLINE_CHARS = 4000  # CloudFormation ZipFile hard limit is 4096 bytes
MAX_TEMPLATE_BYTES = 51000  # CFN TemplateBody limit is 51,200


@dataclass(frozen=True)
class Finding:
    check: str
    path: str
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def _template_findings(content: str, path: str = "template.json") -> tuple[dict | None, list[Finding]]:
    findings: list[Finding] = []
    try:
        doc = json.loads(content)
    except json.JSONDecodeError as exc:
        return None, [Finding("template_json", path, f"Not valid JSON: {exc}")]
    if not isinstance(doc, dict) or not isinstance(doc.get("Resources"), dict) or not doc["Resources"]:
        findings.append(
            Finding("template_json", path, "Template has no Resources section")
        )
        return doc if isinstance(doc, dict) else None, findings
    return doc, findings


def _allowlist_findings(doc: dict, path: str = "template.json") -> list[Finding]:
    findings = []
    for logical_id, resource in (doc.get("Resources") or {}).items():
        rtype = str((resource or {}).get("Type", ""))
        if not rtype.startswith(ALLOWED_TYPE_PREFIXES):
            findings.append(
                Finding(
                    "service_allowlist",
                    path,
                    f"{logical_id}: resource type '{rtype}' is outside the platform allowlist (§4.1.4)",
                )
            )
    return findings


def _packaging_findings(doc: dict) -> list[Finding]:
    findings = []
    for logical_id, resource in (doc.get("Resources") or {}).items():
        if str((resource or {}).get("Type")) != "AWS::Lambda::Function":
            continue
        props = (resource or {}).get("Properties") or {}
        code = props.get("Code") or {}
        zipfile = code.get("ZipFile")
        if not isinstance(zipfile, str):
            findings.append(
                Finding(
                    "inline_packaging",
                    "template.json",
                    f"{logical_id}: Lambda code must be inline ZipFile (S3/ECR assets are Beta scope)",
                )
            )
            continue
        if len(zipfile) > MAX_INLINE_CHARS:
            findings.append(
                Finding(
                    "inline_packaging",
                    "template.json",
                    f"{logical_id}: inline code is {len(zipfile)} chars — over the "
                    f"{MAX_INLINE_CHARS}-char inline ceiling (CloudFormation's "
                    "ZipFile hard limit). In-process builds re-plan as a packaged "
                    "deployment automatically when this is the only finding; "
                    "seeing it means that path was unavailable. Rebuild — "
                    "generation variance usually fits — or declare 'SHALL use "
                    "packaged dependencies' in the requirements document: the "
                    "packaged profile ships code as S3 assets with no inline "
                    "ceiling. The cdk-app profile also lifts the ceiling but "
                    "needs the workspace-runner codegen provider.",
                )
            )
        runtime = str(props.get("Runtime", ""))
        if runtime not in ALLOWED_RUNTIMES:
            findings.append(
                Finding(
                    "inline_packaging",
                    "template.json",
                    f"{logical_id}: runtime '{runtime}' not allowed ({', '.join(ALLOWED_RUNTIMES)})",
                )
            )
    return findings


# §4.2 forbidden patterns are NAMED rules on templates (as-built:
# guardrails.architecture.forbidden_patterns). Each name maps to a concrete
# detector; names without a detector are recorded as unenforceable in the
# manifest rather than silently ignored (or failing builds spuriously).
_CREDENTIAL_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|(?:password|passwd|secret|api_key|apikey|token)\s*[=:]\s*['\"][^'\"\s]{8,}['\"])",
    re.IGNORECASE,
)


def _detect_hardcoded_credentials(artifacts: dict[str, str]) -> list[Finding]:
    findings = []
    for path, content in artifacts.items():
        for match in _CREDENTIAL_RE.finditer(content):
            findings.append(
                Finding(
                    "guardrail_patterns",
                    path,
                    f"hardcoded_credentials: '{match.group(0)[:40]}…' looks like an embedded secret",
                )
            )
    return findings


def _detect_public_db_access(artifacts: dict[str, str]) -> list[Finding]:
    findings = []
    template = artifacts.get("template.json", "")
    try:
        doc = json.loads(template)
    except json.JSONDecodeError:
        return []
    for logical_id, resource in (doc.get("Resources") or {}).items():
        props = (resource or {}).get("Properties") or {}
        rtype = str((resource or {}).get("Type", ""))
        if rtype.startswith("AWS::RDS::") and props.get("PubliclyAccessible") is True:
            findings.append(
                Finding(
                    "guardrail_patterns", "template.json",
                    f"public_internet_access_to_db: {logical_id} sets PubliclyAccessible",
                )
            )
        if rtype == "AWS::EC2::SecurityGroupIngress" or rtype == "AWS::EC2::SecurityGroup":
            blob = json.dumps(props)
            if "0.0.0.0/0" in blob:
                findings.append(
                    Finding(
                        "guardrail_patterns", "template.json",
                        f"public_internet_access_to_db: {logical_id} opens 0.0.0.0/0",
                    )
                )
    return findings


PATTERN_DETECTORS = {
    "hardcoded_credentials": _detect_hardcoded_credentials,
    "public_internet_access_to_db": _detect_public_db_access,
}


def _guardrail_findings(
    forbidden_patterns: list[str], artifacts: dict[str, str]
) -> tuple[list[Finding], list[str]]:
    """Returns (findings, unenforceable-pattern-names)."""
    findings: list[Finding] = []
    unenforceable: list[str] = []
    for name in forbidden_patterns:
        detector = PATTERN_DETECTORS.get(str(name).strip().lower())
        if detector is None:
            unenforceable.append(str(name))
            continue
        findings += detector(artifacts)
    return findings, unenforceable


def _contract_findings(doc: dict, content: str, artifacts: dict[str, str], path: str = "template.json") -> list[Finding]:
    findings = []
    outputs = doc.get("Outputs") or {}
    if "ApiUrl" not in outputs:
        findings.append(
            Finding(
                "deploy_contract",
                path,
                "Outputs.ApiUrl is required (the deployment console reads it)",
            )
        )
    # G25 (evaluate-tier proof, 4 Oct 2026): a model emitting the bare
    # intrinsic as the output body ({"ApiUrl": {"Fn::Sub": …}}) passed this
    # gate as "ready" and CloudFormation then refused CreateStack with
    # "Template format error: Invalid outputs property : [Fn::Sub]". Every
    # output must be an object carrying "Value".
    for name, body in outputs.items():
        if not isinstance(body, dict) or "Value" not in body:
            findings.append(
                Finding(
                    "deploy_contract",
                    path,
                    f"Outputs.{name} must be an object with a \"Value\" key "
                    "(CloudFormation rejects a bare Ref/Fn::Sub as the output body)",
                )
            )
    if len(content.encode()) > MAX_TEMPLATE_BYTES:
        findings.append(
            Finding(
                "deploy_contract",
                path,
                f"Template body is {len(content.encode())} bytes (CloudFormation limit ~51k)",
            )
        )
    if not any(p.endswith(".py") for p in artifacts):
        findings.append(
            Finding("deploy_contract", "src/", "No source files were generated")
        )
    if "README.md" not in artifacts:
        findings.append(Finding("deploy_contract", "README.md", "README.md is missing"))
    return findings


# --- B19 web test console (web-test-console spec R1/R3) -----------------------
# Documented opt-in phrases; narrow enough that descriptive prose ("the
# advisor UI calls this service") never trips it.
_WEB_CONSOLE_SIGNAL_RE = re.compile(
    r"\b(?:provide|include|offer)s?\s+a\s+web\s+(?:test\s+)?"
    r"(?:page|console|interface|ui|front[- ]?end)\b"
    r"|\bweb\s+test\s+(?:page|console)\b",
    re.IGNORECASE,
)

WEB_CONSOLE_BUCKET_ID = "WebConsoleBucket"
WEB_CONSOLE_FN_ID = "WebConsoleFunction"
WEB_CONSOLE_ARTIFACT = "web/index.html"


def requires_web_console(requirements_md: str) -> bool:
    """Deterministic console-signal detection over the requirements document."""
    return bool(_WEB_CONSOLE_SIGNAL_RE.search(requirements_md or ""))


# --- C1 connector-lite (external-import-connectors spec, owner: COPY custody)
# Documented opt-in phrase, the B13/B19 deterministic-signal family. The copy
# prefix is DISTINCT from the registry's marshal/connectors/ custody: in
# direct-provider mode the target account IS the control plane, and sharing
# the name would overwrite registry custody with the composite payload.
_CONNECTOR_SIGNAL_RE = re.compile(
    r"\bSHALL\s+use\s+connector\s+([A-Za-z0-9][A-Za-z0-9-]{1,47})\b",
    re.IGNORECASE,
)
AGENT_CONNECTOR_SECRET_PREFIX = "marshal/agent-connectors/"
_AGENT_CONNECTOR_REF_RE = re.compile(
    re.escape(AGENT_CONNECTOR_SECRET_PREFIX) + r"([A-Za-z0-9-]+)"
)
MAX_DECLARED_CONNECTORS = 5


def declared_connectors(requirements_md: str) -> list[str]:
    """Deterministic connector declarations from the frozen requirements.

    Deduped, lowercased, sorted, capped — every consumer (generation, gate,
    manifest, deployer) sees the identical list.
    """
    slugs = {
        m.group(1).lower() for m in _CONNECTOR_SIGNAL_RE.finditer(requirements_md or "")
    }
    return sorted(slugs)[:MAX_DECLARED_CONNECTORS]


def connector_env_name(slug: str) -> str:
    return f"CONNECTOR_{slug.upper().replace('-', '_')}_SECRET"


# --- Agent substance R2.1: conversation memory (agent-substance spec) --------
# Deterministic opt-in phrase; the generated agent gets a per-agent DynamoDB
# table (fixed logical id = the staging/teardown-style contract), TTL'd items,
# and verbatim remember/recall helpers. Inline-profile compatible.
_MEMORY_SIGNAL_RE = re.compile(
    r"\bSHALL\s+keep\s+conversation\s+memory\b", re.IGNORECASE
)
MEMORY_TABLE_ID = "AgentMemoryTable"


def requires_memory(requirements_md: str) -> bool:
    return bool(_MEMORY_SIGNAL_RE.search(requirements_md or ""))


def _memory_findings(
    doc: dict,
    artifacts: dict[str, str],
    declared: bool,
    path: str = "template.json",
) -> list[Finding]:
    """Memory contract: table present iff declared, TTL enabled, scoped IAM,
    helper presence — and symmetric refusal of undeclared memory artifacts."""
    findings: list[Finding] = []
    resources = (doc.get("Resources") or {}) if isinstance(doc, dict) else {}
    table = resources.get(MEMORY_TABLE_ID)
    env_present = any(
        "MEMORY_TABLE"
        in ((((r or {}).get("Properties") or {}).get("Environment") or {}).get("Variables") or {})
        for r in resources.values()
        if str((r or {}).get("Type")) == "AWS::Lambda::Function"
    )

    if not declared:
        if table is not None or env_present:
            findings.append(
                Finding(
                    "memory_contract", path,
                    "Memory artifacts are present but the spec does not declare "
                    "conversation memory (SHALL keep conversation memory is the "
                    "only opt-in)",
                )
            )
        return findings

    if table is None or str(table.get("Type")) != "AWS::DynamoDB::Table":
        findings.append(
            Finding(
                "memory_contract", path,
                f"{MEMORY_TABLE_ID} (AWS::DynamoDB::Table) is required — the "
                "fixed logical id is the contract",
            )
        )
    else:
        props = table.get("Properties") or {}
        ttl = props.get("TimeToLiveSpecification") or {}
        if not (ttl.get("Enabled") is True and ttl.get("AttributeName") == "expires_at"):
            findings.append(
                Finding(
                    "memory_contract", path,
                    f"{MEMORY_TABLE_ID}: TimeToLiveSpecification on 'expires_at' "
                    "must be enabled — memory expires, it does not accumulate",
                )
            )
        keys = {k.get("AttributeName") for k in (props.get("KeySchema") or [])}
        if keys != {"session_id", "sk"}:
            findings.append(
                Finding(
                    "memory_contract", path,
                    f"{MEMORY_TABLE_ID}: key schema must be session_id (HASH) + "
                    f"sk (RANGE), got {sorted(k for k in keys if k)}",
                )
            )
    if not env_present:
        findings.append(
            Finding(
                "memory_contract", path,
                "No Lambda carries the MEMORY_TABLE environment variable",
            )
        )
    joined = "\n".join(artifacts.values())
    if "def remember(" not in joined or "def recall(" not in joined:
        findings.append(
            Finding(
                "memory_contract", path,
                "Declared memory requires the remember/recall helpers in the "
                "handler code",
            )
        )
    return findings


# --- Agent substance R1: packaged dependency profile -------------------------
# Deterministic opt-in phrase; the ONLY selector of the packaged-cfn profile
# (never chosen silently — template defaults cannot pick it, R1.2).
_PACKAGED_SIGNAL_RE = re.compile(
    r"\bSHALL\s+use\s+packaged\s+dependencies\b", re.IGNORECASE
)
PACKAGE_BUCKET_PARAM = "PackageBucket"
PACKAGE_KEY_PARAM = "PackageKey"
REQUIREMENTS_ARTIFACT = "requirements.txt"


def requires_packaged(requirements_md: str) -> bool:
    return bool(_PACKAGED_SIGNAL_RE.search(requirements_md or ""))


def _packaged_packaging_findings(
    doc: dict, artifacts: dict[str, str], path: str = "template.json"
) -> list[Finding]:
    """packaged-cfn shape: business Lambdas reference the deployer-staged
    package via the two fixed parameters; fixed platform helpers (console,
    MCP scaffold) may stay inline under the 4KB ceiling; requirements.txt is
    exact-pinned and bounded (SBOM-lite discipline, S14-03)."""
    from app.services.codegen import contract as pkg_contract

    findings: list[Finding] = []
    params = (doc.get("Parameters") or {}) if isinstance(doc, dict) else {}
    for required in (PACKAGE_BUCKET_PARAM, PACKAGE_KEY_PARAM):
        if str((params.get(required) or {}).get("Type")) != "String":
            findings.append(
                Finding(
                    "packaged_dependencies", path,
                    f"Parameters.{required} (Type String) is required — the "
                    "deployer stages the package and binds it there",
                )
            )
    expected_code = {
        "S3Bucket": {"Ref": PACKAGE_BUCKET_PARAM},
        "S3Key": {"Ref": PACKAGE_KEY_PARAM},
    }
    for logical_id, resource in (doc.get("Resources") or {}).items():
        if str((resource or {}).get("Type")) != "AWS::Lambda::Function":
            continue
        props = (resource or {}).get("Properties") or {}
        code = props.get("Code") or {}
        zipfile = code.get("ZipFile")
        if isinstance(zipfile, str):
            if len(zipfile) > MAX_INLINE_CHARS:
                findings.append(
                    Finding(
                        "packaged_dependencies", path,
                        f"{logical_id}: inline helper code is {len(zipfile)} chars "
                        f"— fixed helpers must stay under {MAX_INLINE_CHARS}; "
                        "business code belongs in the package",
                    )
                )
            continue
        if code != expected_code:
            findings.append(
                Finding(
                    "packaged_dependencies", path,
                    f"{logical_id}: packaged Lambdas must reference the staged "
                    'package exactly as {"S3Bucket": {"Ref": "PackageBucket"}, '
                    '"S3Key": {"Ref": "PackageKey"}}',
                )
            )
            continue
        handler = str(props.get("Handler") or "")
        if not re.fullmatch(r"[A-Za-z0-9_]+\.handler", handler):
            findings.append(
                Finding(
                    "packaged_dependencies", path,
                    f"{logical_id}: Handler must be '<module>.handler' matching "
                    f"a packaged src/<module>.py (got '{handler}')",
                )
            )
        runtime = str(props.get("Runtime") or "")
        if runtime not in ALLOWED_RUNTIMES:
            findings.append(
                Finding(
                    "packaged_dependencies", path,
                    f"{logical_id}: runtime '{runtime}' is outside {ALLOWED_RUNTIMES}",
                )
            )
    manifest = artifacts.get(REQUIREMENTS_ARTIFACT)
    if manifest is None:
        findings.append(
            Finding(
                "packaged_dependencies", REQUIREMENTS_ARTIFACT,
                "requirements.txt was not generated — the packaged profile's "
                "dependency manifest is the deliverable",
            )
        )
    else:
        for error in pkg_contract.requirements_pin_errors(manifest):
            findings.append(
                Finding("packaged_dependencies", REQUIREMENTS_ARTIFACT, error)
            )
    return findings


def _packaged_symmetry_findings(
    profile: str, declared: bool, path: str = "template.json"
) -> list[Finding]:
    """Profile ↔ declaration symmetry: the signal is the only door in, and a
    declared spec must not silently build any other profile."""
    if declared and profile != "packaged-cfn":
        return [
            Finding(
                "packaged_profile", "requirements.md",
                "Spec declares packaged dependencies but the build ran the "
                f"'{profile}' profile — rebuild (the resolver honors the signal)",
            )
        ]
    if not declared and profile == "packaged-cfn":
        return [
            Finding(
                "packaged_profile", path,
                "packaged-cfn build without the spec declaration — 'SHALL use "
                "packaged dependencies' is the only opt-in",
            )
        ]
    return []


# --- Agent substance R2.2/R2.3: tools + planning rungs ------------------------
_TOOLS_SIGNAL_RE = re.compile(
    r"\bSHALL\s+use\s+tools\s*:\s*([A-Za-z0-9_ ,-]+)", re.IGNORECASE
)
_PLANNING_SIGNAL_RE = re.compile(
    r"\bSHALL\s+plan\s+multi-step\s+responses\b", re.IGNORECASE
)
MAX_DECLARED_TOOLS = 6


def declared_tools(requirements_md: str) -> list[str]:
    """Deterministic local-tool declarations: lowercased snake names, deduped,
    sorted, capped — every consumer sees the identical list."""
    names: set[str] = set()
    for match in _TOOLS_SIGNAL_RE.finditer(requirements_md or ""):
        for part in match.group(1).split(","):
            name = part.strip().lower().replace("-", "_").replace(" ", "_")
            if re.fullmatch(r"[a-z][a-z0-9_]{1,31}", name):
                names.add(name)
    return sorted(names)[:MAX_DECLARED_TOOLS]


def requires_planning(requirements_md: str) -> bool:
    return bool(_PLANNING_SIGNAL_RE.search(requirements_md or ""))


def _loop_env_findings(
    env: dict, var: str, cap: int, logical_id: str, path: str
) -> list[Finding]:
    raw = str(env.get(var, ""))
    if not raw.isdigit() or not 1 <= int(raw) <= cap:
        return [
            Finding(
                "loop_caps", path,
                f"{logical_id}: {var} must be an integer between 1 and {cap} "
                f"(the template rail), got '{raw}'",
            )
        ]
    return []


def _tools_findings(
    doc: dict,
    artifacts: dict[str, str],
    declared: list[str],
    cap: int,
    path: str = "template.json",
) -> list[Finding]:
    """Tools contract: the agent file's fixed byte-prefix (imports + exact
    TOOLS_JSON + verbatim driver), one tool_<name> per declared tool, wired
    Lambda (env cap within rails + bedrock:InvokeModel), symmetric refusal."""
    from app.services.codegen.scaffolds import (
        TOOLS_AGENT_FILENAME,
        TOOLS_DRIVER_CODE,
        tools_agent_prelude,
    )

    findings: list[Finding] = []
    resources = (doc.get("Resources") or {}) if isinstance(doc, dict) else {}
    agent_file = artifacts.get(TOOLS_AGENT_FILENAME)

    if not declared:
        if agent_file is not None or any(
            TOOLS_DRIVER_CODE in (text or "") for text in artifacts.values()
        ):
            findings.append(
                Finding(
                    "tools_contract", path,
                    "Tool-loop artifacts are present but the spec declares no "
                    "tools (SHALL use tools: <a>, <b> is the only opt-in)",
                )
            )
        return findings

    prelude = tools_agent_prelude(declared)
    if agent_file is None:
        findings.append(
            Finding(
                "tools_contract", TOOLS_AGENT_FILENAME,
                "Declared tools require the platform-assembled tool agent file",
            )
        )
        return findings
    if not agent_file.startswith(prelude):
        findings.append(
            Finding(
                "tools_contract", TOOLS_AGENT_FILENAME,
                "The tool agent's prelude (TOOLS_JSON + driver) must be "
                "BYTE-EQUAL to the platform scaffold for the declared tools",
            )
        )
    for name in declared:
        if f"\ndef tool_{name}(" not in agent_file:
            findings.append(
                Finding(
                    "tools_contract", TOOLS_AGENT_FILENAME,
                    f"Declared tool '{name}' has no tool_{name}() implementation",
                )
            )
    agent_fns = _rung_lambdas(resources, agent_file, "tools_agent")
    if not agent_fns:
        findings.append(
            Finding(
                "tools_contract", path,
                "No Lambda carries the tool agent (inline code or "
                "tools_agent.handler)",
            )
        )
    iam_json = json.dumps(
        [r for r in resources.values() if str((r or {}).get("Type", "")).startswith("AWS::IAM::")]
    )
    if agent_fns and "bedrock:InvokeModel" not in iam_json:
        findings.append(
            Finding(
                "tools_contract", path,
                "The tool agent's role needs bedrock:InvokeModel (the Converse loop)",
            )
        )
    for logical_id, props in agent_fns:
        env = (((props.get("Environment") or {}).get("Variables")) or {})
        findings += _loop_env_findings(env, "TOOLS_MAX_ITERATIONS", cap, logical_id, path)
        if not str(env.get("AGENT_SYSTEM_PROMPT") or "").strip():
            findings.append(
                Finding(
                    "tools_contract", path,
                    f"{logical_id}: AGENT_SYSTEM_PROMPT must carry the agent's behavior",
                )
            )
    return findings


def _planning_findings(
    doc: dict,
    artifacts: dict[str, str],
    declared: bool,
    cap: int,
    path: str = "template.json",
) -> list[Finding]:
    """Planning contract: fully verbatim scaffold (byte equality), wired
    Lambda with a rail-bounded step cap, symmetric refusal."""
    from app.services.codegen.scaffolds import (
        PLANNING_AGENT_FILENAME,
        PLANNING_AGENT_SCAFFOLD,
    )

    findings: list[Finding] = []
    resources = (doc.get("Resources") or {}) if isinstance(doc, dict) else {}
    agent_file = artifacts.get(PLANNING_AGENT_FILENAME)

    if not declared:
        if agent_file is not None or any(
            "MAX_PLAN_STEPS" in (text or "") for text in artifacts.values()
        ):
            findings.append(
                Finding(
                    "planning_contract", path,
                    "Planning-loop artifacts are present but the spec does not "
                    "declare planning (SHALL plan multi-step responses is the "
                    "only opt-in)",
                )
            )
        return findings

    if agent_file != PLANNING_AGENT_SCAFFOLD:
        findings.append(
            Finding(
                "planning_contract", PLANNING_AGENT_FILENAME,
                "The planning handler must be BYTE-EQUAL to the platform "
                "scaffold (zero-variance contract)"
                + (" — file missing" if agent_file is None else " — content differs"),
            )
        )
    agent_fns = _rung_lambdas(resources, agent_file or "", "planning_agent")
    if not agent_fns:
        findings.append(
            Finding(
                "planning_contract", path,
                "No Lambda carries the planning scaffold (inline code or "
                "planning_agent.handler)",
            )
        )
    iam_json = json.dumps(
        [r for r in resources.values() if str((r or {}).get("Type", "")).startswith("AWS::IAM::")]
    )
    if agent_fns and "bedrock:InvokeModel" not in iam_json:
        findings.append(
            Finding(
                "planning_contract", path,
                "The planning agent's role needs bedrock:InvokeModel",
            )
        )
    for logical_id, props in agent_fns:
        env = (((props.get("Environment") or {}).get("Variables")) or {})
        findings += _loop_env_findings(
            env, "PLANNING_MAX_ITERATIONS", cap, logical_id, path
        )
        if not str(env.get("AGENT_SYSTEM_PROMPT") or "").strip():
            findings.append(
                Finding(
                    "planning_contract", path,
                    f"{logical_id}: AGENT_SYSTEM_PROMPT must carry the agent's behavior",
                )
            )
    return findings


def _rung_lambdas(resources: dict, agent_file: str, module: str) -> list[tuple[str, dict]]:
    """Lambdas carrying a rung handler: inline profile → ZipFile equals the
    artifact; packaged profile → Handler '<module>.handler'."""
    matched: list[tuple[str, dict]] = []
    for logical_id, resource in resources.items():
        if str((resource or {}).get("Type")) != "AWS::Lambda::Function":
            continue
        props = (resource or {}).get("Properties") or {}
        code = (props.get("Code") or {}).get("ZipFile")
        if (agent_file and code == agent_file) or (
            str(props.get("Handler") or "") == f"{module}.handler"
        ):
            matched.append((logical_id, props))
    return matched


def _rung_combo_findings(
    mcp_declared: list[str], tools: list[str], planning: bool
) -> list[Finding]:
    """v1: one conversation loop per agent — MCP, tools and planning are
    mutually exclusive (memory composes with any of them)."""
    active = [
        name
        for name, on in (
            ("MCP tools", bool(mcp_declared)),
            ("local tools", bool(tools)),
            ("planning", planning),
        )
        if on
    ]
    if len(active) <= 1:
        return []
    return [
        Finding(
            "rung_combination", "requirements.md",
            f"v1 supports one conversation loop per agent — declared: "
            f"{' + '.join(active)}. Split the capabilities across composed "
            "agents (SHALL call agent <slug>) instead.",
        )
    ]


# --- Composable agents R2/R3 (composable-agents spec) ------------------------
AGENT_DEPENDENCY_SECRET_PREFIX = "marshal/agent-dependencies/"
_AGENT_DEP_REF_RE = re.compile(
    re.escape(AGENT_DEPENDENCY_SECRET_PREFIX) + r"([A-Za-z0-9-]+)"
)


def dependency_env_name(slug: str) -> str:
    return f"AGENT_DEP_{slug.upper().replace('-', '_')}_SECRET"


def _agent_dependency_findings(
    doc: dict,
    artifacts: dict[str, str],
    declared: list[str],
    orchestrated: bool,
    path: str = "template.json",
) -> list[Finding]:
    """Declared-only inter-agent reach (the connector_contract discipline):
    every dependency-secret reference maps to a declared slug, every declared
    slug is wired (env var + IAM read grant), and an orchestrated pipeline's
    code actually calls every declared agent."""
    findings: list[Finding] = []
    declared_set = set(declared)
    resources = (doc.get("Resources") or {}) if isinstance(doc, dict) else {}
    template_json = json.dumps(doc) if isinstance(doc, dict) else ""

    for where, text in {path: template_json, **artifacts}.items():
        for match in _AGENT_DEP_REF_RE.finditer(text or ""):
            slug = match.group(1).lower()
            if slug not in declared_set:
                findings.append(
                    Finding(
                        "agent_dependency",
                        where,
                        f"References dependency secret '{match.group(0)}' but the "
                        "spec does not declare that agent (SHALL call agent "
                        "<slug> / SHALL orchestrate agents <a>, <b> are the "
                        "only opt-ins)",
                    )
                )
    seen_env: dict[str, str] = {}
    for logical_id, resource in resources.items():
        if str((resource or {}).get("Type")) != "AWS::Lambda::Function":
            continue
        env = (
            ((resource.get("Properties") or {}).get("Environment") or {}).get("Variables")
        ) or {}
        for name, value in env.items():
            if not re.fullmatch(r"AGENT_DEP_[A-Z0-9_]+_SECRET", str(name)):
                continue
            slug = str(name)[len("AGENT_DEP_"):-len("_SECRET")].lower().replace("_", "-")
            if slug not in declared_set:
                findings.append(
                    Finding(
                        "agent_dependency", path,
                        f"{logical_id}: environment variable {name} does not map "
                        "to a declared agent dependency",
                    )
                )
                continue
            seen_env[slug] = str(value)
    iam_json = json.dumps(
        [
            r for r in resources.values()
            if str((r or {}).get("Type", "")).startswith("AWS::IAM::")
        ]
    )
    joined_code = "\n".join(artifacts.values())
    for slug in declared:
        expected = f"{AGENT_DEPENDENCY_SECRET_PREFIX}{slug}"
        if slug not in seen_env:
            findings.append(
                Finding(
                    "agent_dependency", path,
                    f"Declared agent '{slug}' is not wired: no Lambda carries the "
                    f"{dependency_env_name(slug)} environment variable",
                )
            )
        elif seen_env[slug] != expected:
            findings.append(
                Finding(
                    "agent_dependency", path,
                    f"{dependency_env_name(slug)} must be exactly '{expected}' "
                    f"(got '{seen_env[slug]}') — the deployer provisions that name",
                )
            )
        if expected not in iam_json:
            findings.append(
                Finding(
                    "agent_dependency", path,
                    f"Declared agent '{slug}' has no secretsmanager read grant "
                    "in the template IAM",
                )
            )
        if orchestrated and (
            f'call_agent("{slug}"' not in joined_code
            and f"call_agent('{slug}'" not in joined_code
        ):
            findings.append(
                Finding(
                    "agent_dependency", path,
                    f"Orchestrated pipeline never calls declared agent '{slug}' — "
                    "the coordinator must call every orchestrated agent",
                )
            )
    return findings


# --- C3 v1: MCP tool-loop agents (owner go 7 Sep 2026; inline scaffold) ------
# Deviation from the packaged-profile assumption recorded in FSD §13.5Q: MCP
# over streamable HTTP is plain JSON-RPC, so a stdlib mini-client + bounded
# Converse loop fit the inline profile as ONE platform-verbatim handler.
_MCP_TOOLS_SIGNAL_RE = re.compile(
    r"\bSHALL\s+use\s+MCP\s+tools\s+from\s+connector\s+([A-Za-z0-9][A-Za-z0-9-]{1,47})\b",
    re.IGNORECASE,
)


def mcp_tool_connectors(requirements_md: str) -> list[str]:
    """Deterministic MCP tool-loop declarations (deduped/lowered/sorted).
    v1 permits exactly ONE — the gate refuses more."""
    slugs = {
        m.group(1).lower() for m in _MCP_TOOLS_SIGNAL_RE.finditer(requirements_md or "")
    }
    return sorted(slugs)


def _mcp_tool_loop_findings(
    doc: dict,
    artifacts: dict[str, str],
    mcp_declared: list[str],
    path: str = "template.json",
) -> list[Finding]:
    """B23-strongest contract: the tool-loop handler is byte-equal to the
    platform scaffold, wired to exactly the declared MCP connector."""
    from app.services.codegen.scaffolds import MCP_AGENT_FILENAME, MCP_AGENT_SCAFFOLD

    findings: list[Finding] = []
    resources = (doc.get("Resources") or {}) if isinstance(doc, dict) else {}
    scaffold_file = artifacts.get(MCP_AGENT_FILENAME)

    if not mcp_declared:
        if scaffold_file is not None or any(
            "MCP_CONNECTOR_SLUG"
            in json.dumps(
                ((r or {}).get("Properties") or {}).get("Environment") or {}
            )
            for r in resources.values()
        ):
            findings.append(
                Finding(
                    "mcp_tool_loop", path,
                    "MCP tool-loop artifacts are present but the spec declares "
                    "no MCP connector (SHALL use MCP tools from connector "
                    "<slug> is the only opt-in)",
                )
            )
        return findings

    if len(mcp_declared) > 1:
        findings.append(
            Finding(
                "mcp_tool_loop", "requirements.md",
                f"v1 supports exactly ONE MCP tool connector per agent — "
                f"declared: {', '.join(mcp_declared)}",
            )
        )
        return findings
    slug = mcp_declared[0]

    if scaffold_file != MCP_AGENT_SCAFFOLD:
        findings.append(
            Finding(
                "mcp_tool_loop", MCP_AGENT_FILENAME,
                "The MCP tool-loop handler must be BYTE-EQUAL to the platform "
                "scaffold (zero-variance contract)"
                + (" — file missing" if scaffold_file is None else " — content differs"),
            )
        )

    agent_fns = []
    for logical_id, resource in resources.items():
        if str((resource or {}).get("Type")) != "AWS::Lambda::Function":
            continue
        props = (resource or {}).get("Properties") or {}
        code = (props.get("Code") or {}).get("ZipFile")
        if code == MCP_AGENT_SCAFFOLD:
            agent_fns.append((logical_id, props))
    if not agent_fns:
        findings.append(
            Finding(
                "mcp_tool_loop", path,
                "No Lambda carries the verbatim MCP tool-loop scaffold as its "
                "inline code",
            )
        )
    for logical_id, props in agent_fns:
        env = ((props.get("Environment") or {}).get("Variables")) or {}
        if env.get("MCP_CONNECTOR_SLUG") != slug:
            findings.append(
                Finding(
                    "mcp_tool_loop", path,
                    f"{logical_id}: MCP_CONNECTOR_SLUG must be exactly '{slug}' "
                    f"(got '{env.get('MCP_CONNECTOR_SLUG')}')",
                )
            )
        if connector_env_name(slug) not in env:
            findings.append(
                Finding(
                    "mcp_tool_loop", path,
                    f"{logical_id}: missing {connector_env_name(slug)} — the "
                    "scaffold reads the C1 connector copy through it",
                )
            )
        if not str(env.get("AGENT_SYSTEM_PROMPT") or "").strip():
            findings.append(
                Finding(
                    "mcp_tool_loop", path,
                    f"{logical_id}: AGENT_SYSTEM_PROMPT must carry the agent's "
                    "behavior (the scaffold is generic by design)",
                )
            )
    return findings


def _connector_findings(
    doc: dict,
    artifacts: dict[str, str],
    declared: list[str],
    path: str = "template.json",
) -> list[Finding]:
    """v1 egress control: generated artifacts may reference ONLY declared
    connector copies (undeclared reach fails the build), and every declared
    connector must be wired (env var + secret-read grant) so the deployed
    agent actually works."""
    findings: list[Finding] = []
    declared_set = set(declared)
    env_by_slug = {slug: connector_env_name(slug) for slug in declared}
    resources = (doc.get("Resources") or {}) if isinstance(doc, dict) else {}

    # Every marshal/agent-connectors/<slug> reference anywhere (template or
    # code) must map to a declared slug — zero-connector builds stay clean.
    template_json = json.dumps(doc) if isinstance(doc, dict) else ""
    corpora = {path: template_json, **artifacts}
    for where, text in corpora.items():
        for match in _AGENT_CONNECTOR_REF_RE.finditer(text or ""):
            slug = match.group(1).lower()
            if slug not in declared_set:
                findings.append(
                    Finding(
                        "connector_contract",
                        where,
                        f"References connector secret '{match.group(0)}' but the "
                        "spec does not declare that connector (SHALL use "
                        "connector <slug> is the only opt-in)",
                    )
                )

    # Undeclared CONNECTOR_*_SECRET env vars are the same violation by
    # another door; declared ones must be present on at least one function
    # with the exact copy name as value.
    seen_env: dict[str, str] = {}
    for logical_id, resource in resources.items():
        if str((resource or {}).get("Type")) != "AWS::Lambda::Function":
            continue
        env = (
            ((resource.get("Properties") or {}).get("Environment") or {}).get(
                "Variables"
            )
            or {}
        )
        for name, value in env.items():
            if not re.fullmatch(r"CONNECTOR_[A-Z0-9_]+_SECRET", str(name)):
                continue
            slug = str(name)[len("CONNECTOR_"):-len("_SECRET")].lower().replace("_", "-")
            if slug not in declared_set:
                findings.append(
                    Finding(
                        "connector_contract",
                        path,
                        f"{logical_id}: environment variable {name} does not map "
                        "to a declared connector",
                    )
                )
                continue
            seen_env[slug] = str(value)
    for slug in declared:
        expected = f"{AGENT_CONNECTOR_SECRET_PREFIX}{slug}"
        if slug not in seen_env:
            findings.append(
                Finding(
                    "connector_contract",
                    path,
                    f"Declared connector '{slug}' is not wired: no Lambda carries "
                    f"the {env_by_slug[slug]} environment variable",
                )
            )
        elif seen_env[slug] != expected:
            findings.append(
                Finding(
                    "connector_contract",
                    path,
                    f"{env_by_slug[slug]} must be exactly '{expected}' "
                    f"(got '{seen_env[slug]}') — the deployer provisions that name",
                )
            )
        # The read grant must exist in the template's IAM resources
        # specifically — an env var mentioning the name is not a grant.
        iam_json = json.dumps(
            [
                r
                for r in resources.values()
                if str((r or {}).get("Type", "")).startswith("AWS::IAM::")
            ]
        )
        if f"{AGENT_CONNECTOR_SECRET_PREFIX}{slug}" not in iam_json:
            findings.append(
                Finding(
                    "connector_contract",
                    path,
                    f"Declared connector '{slug}' has no secretsmanager read "
                    "grant in the template IAM",
                )
            )
    return findings


def _is_console_method(resource: dict) -> bool:
    """A method serving the console: its integration references the fixed
    serving-function logical id (the gate contract, R3.2)."""
    integration = ((resource or {}).get("Properties") or {}).get("Integration") or {}
    return WEB_CONSOLE_FN_ID in json.dumps(integration)


def _web_console_findings(
    doc: dict, artifacts: dict[str, str], *, require_api_key: bool,
    path: str = "template.json",
) -> list[Finding]:
    """R3.1: the spec demanded a test console — the template and artifacts
    must deliver the shape the deployer and card depend on."""
    findings: list[Finding] = []
    resources = doc.get("Resources") or {}

    bucket = resources.get(WEB_CONSOLE_BUCKET_ID)
    if bucket is None or str(bucket.get("Type")) != "AWS::S3::Bucket":
        findings.append(
            Finding(
                "web_console", path,
                f"{WEB_CONSOLE_BUCKET_ID} (AWS::S3::Bucket) is required — the "
                "fixed logical id is the staging/teardown contract",
            )
        )
    else:
        block = (bucket.get("Properties") or {}).get("PublicAccessBlockConfiguration") or {}
        flags = ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets")
        if not all(block.get(f) is True for f in flags):
            findings.append(
                Finding(
                    "web_console", path,
                    f"{WEB_CONSOLE_BUCKET_ID}: full PublicAccessBlockConfiguration is "
                    "required — the console is served THROUGH the API, never from a "
                    "public bucket",
                )
            )

    fn = resources.get(WEB_CONSOLE_FN_ID)
    if fn is None or str(fn.get("Type")) != "AWS::Lambda::Function":
        findings.append(
            Finding(
                "web_console", path,
                f"{WEB_CONSOLE_FN_ID} (AWS::Lambda::Function) is required to serve "
                "the console page",
            )
        )
    else:
        env = (((fn.get("Properties") or {}).get("Environment") or {}).get("Variables") or {})
        if "WEB_BUCKET" not in env:
            findings.append(
                Finding(
                    "web_console", path,
                    f"{WEB_CONSOLE_FN_ID}: WEB_BUCKET environment variable must "
                    "reference the console bucket",
                )
            )

    if not any(
        str((r or {}).get("Type")) == "AWS::ApiGateway::Method" and _is_console_method(r)
        for r in resources.values()
    ):
        findings.append(
            Finding(
                "web_console", path,
                f"No API Gateway method integrates with {WEB_CONSOLE_FN_ID} — the "
                "console must be reachable on the same RestApi (GET /app)",
            )
        )

    outputs = doc.get("Outputs") or {}
    for required in ("WebConsoleUrl", "WebBucketName"):
        if required not in outputs:
            findings.append(
                Finding(
                    "web_console", path,
                    f"Outputs.{required} is required (deployer staging + card link)",
                )
            )

    page = artifacts.get(WEB_CONSOLE_ARTIFACT)
    if page is None:
        findings.append(
            Finding(
                "web_console", WEB_CONSOLE_ARTIFACT,
                "web/index.html was not generated — the console page is the "
                "deliverable",
            )
        )
    else:
        if len(page) < 500 or "<html" not in page.lower() or "fetch(" not in page:
            findings.append(
                Finding(
                    "web_console", WEB_CONSOLE_ARTIFACT,
                    "Console page must be a real interactive document "
                    "(non-trivial, <html, calls fetch())",
                )
            )
        if re.search(r"fetch\(\s*['\"`]/", page):
            # Live drill finding (24 Aug 2026): root-relative paths resolve
            # WITHOUT the API Gateway stage prefix (/prod) and 403 — the page
            # must derive its base from location.pathname.
            findings.append(
                Finding(
                    "web_console", WEB_CONSOLE_ARTIFACT,
                    "Console page uses root-relative fetch('/...') — that drops "
                    "the API Gateway stage prefix and 403s; derive the base from "
                    "location.pathname instead",
                )
            )
        if require_api_key and "x-api-key" not in page:
            findings.append(
                Finding(
                    "web_console", WEB_CONSOLE_ARTIFACT,
                    "The spec requires an API key: the console must carry the "
                    "paste-key wiring (x-api-key header) — keys are entered at "
                    "runtime, never embedded",
                )
            )
    return findings


# --- B13/B21 deployed-app auth ---------------------------------------------
# B21 safe default: every new build is keyed unless the requirements carry
# one exact, line-anchored public opt-out. Casual prose never opens an API.
_API_KEY_SIGNAL_RE = re.compile(
    r"\brequire[sd]?\s+(?:an\s+)?api\s*key\b|\bapi\s*key\s+(?:is\s+)?required\b",
    re.IGNORECASE,
)
_NEGATED_API_KEY_RE = re.compile(
    r"\b(?:does?\s+not|shall\s+not|must\s+not)\s+require\s+(?:an\s+)?api\s*key\b",
    re.IGNORECASE,
)
_PUBLIC_ENDPOINT_SIGNAL_RE = re.compile(
    r"(?im)^\s*(?:[-*+]\s*)?(?:"
    r"endpoint\s+authentication\s*:\s*PUBLIC|"
    r"(?:all\s+endpoints|the\s+api)\s+(?:SHALL|MUST)\s+be\s+"
    r"publicly\s+accessible\s+without\s+authentication[.!]?"
    r")\s*$"
)


def endpoint_auth_decision(requirements_md: str) -> dict:
    """Normalized, JSON-safe B21 posture from the frozen requirements.

    Conflict keeps the safer keyed generation posture but is separately a
    blocking spec finding — contradictory intent must be resolved by author.
    """
    text = requirements_md or ""
    explicit_key = any(
        _API_KEY_SIGNAL_RE.search(line) and not _NEGATED_API_KEY_RE.search(line)
        for line in text.splitlines()
    )
    explicit_public = bool(_PUBLIC_ENDPOINT_SIGNAL_RE.search(text))
    if explicit_key and explicit_public:
        return {"mode": "key_required", "source": "conflict"}
    if explicit_public:
        return {"mode": "open", "source": "explicit_public"}
    if explicit_key:
        return {"mode": "key_required", "source": "explicit_key"}
    return {"mode": "key_required", "source": "default"}


def requires_api_key(requirements_md: str) -> bool:
    """Compatibility wrapper: no signal is keyed under B21."""
    return endpoint_auth_decision(requirements_md)["mode"] == "key_required"


def _auth_decision_findings(decision: dict, path: str) -> list[Finding]:
    if decision.get("source") != "conflict":
        return []
    return [
        Finding(
            "endpoint_auth_signal",
            "requirements.md",
            "Requirements explicitly demand BOTH API-key protection and public "
            "unauthenticated access — keep exactly one endpoint-authentication intent",
        )
    ]


def _auth_findings(
    doc: dict,
    path: str = "template.json",
    *,
    allow_console_exemption: bool = False,
) -> list[Finding]:
    """Keyed contract for either synthesized artifact profile."""
    findings: list[Finding] = []
    resources = doc.get("Resources") or {}
    key_ids = [
        lid for lid, r in resources.items()
        if str((r or {}).get("Type")) == "AWS::ApiGateway::ApiKey"
    ]
    if not key_ids:
        findings.append(
            Finding(
                "auth_contract", path,
                "Spec requires an API key but the template has no "
                "AWS::ApiGateway::ApiKey resource",
            )
        )
    plans = {
        lid: r
        for lid, r in resources.items()
        if str((r or {}).get("Type")) == "AWS::ApiGateway::UsagePlan"
    }
    if not plans:
        findings.append(
            Finding(
                "auth_contract", path,
                "AWS::ApiGateway::UsagePlan is required — a key without a bound "
                "plan cannot authorize requests",
            )
        )
    if not any(
        str((r or {}).get("Type")) == "AWS::ApiGateway::UsagePlanKey"
        for r in resources.values()
    ):
        findings.append(
            Finding(
                "auth_contract", path,
                "API key is not attached to a usage plan "
                "(AWS::ApiGateway::UsagePlanKey missing — the key would not be enforced)",
            )
        )
    # A usage plan without an ApiStages binding accepts the key for NO stage.
    for logical_id, resource in plans.items():
        stages = ((resource or {}).get("Properties") or {}).get("ApiStages")
        if not stages:
            findings.append(
                Finding(
                    "auth_contract", path,
                    f"{logical_id}: usage plan has no ApiStages binding — the "
                    "key would be valid for no stage and every request 403s",
                )
            )
    for logical_id, resource in resources.items():
        if str((resource or {}).get("Type")) != "AWS::ApiGateway::Method":
            continue
        props = (resource or {}).get("Properties") or {}
        if str(props.get("HttpMethod", "")).upper() == "OPTIONS":
            continue  # CORS preflight cannot carry keys
        if allow_console_exemption and _is_console_method(resource):
            continue  # B19 /app is public only when the console signal exists
        if props.get("ApiKeyRequired") is not True:
            findings.append(
                Finding(
                    "auth_contract", path,
                    f"{logical_id}: method is not key-protected "
                    "(ApiKeyRequired must be true on every non-OPTIONS method)",
                )
            )
    if "ApiKeyId" not in (doc.get("Outputs") or {}):
        findings.append(
            Finding(
                "auth_contract", path,
                "Outputs.ApiKeyId is required (the deployer fetches the key "
                "value through it for smoke and owner reveal)",
            )
        )
    return findings


def _public_auth_findings(doc: dict, path: str = "template.json") -> list[Finding]:
    """Explicit PUBLIC means the generated artifact must be genuinely open,
    not merely exempt from the keyed gate."""
    findings: list[Finding] = []
    resources = doc.get("Resources") or {}
    keyed_types = {
        "AWS::ApiGateway::ApiKey",
        "AWS::ApiGateway::UsagePlan",
        "AWS::ApiGateway::UsagePlanKey",
    }
    present = [
        logical_id
        for logical_id, resource in resources.items()
        if str((resource or {}).get("Type")) in keyed_types
    ]
    if present:
        findings.append(
            Finding(
                "public_auth_contract", path,
                "Requirements explicitly opt out of authentication, but API-key "
                f"infrastructure is present ({', '.join(present)})",
            )
        )
    for logical_id, resource in resources.items():
        if str((resource or {}).get("Type")) != "AWS::ApiGateway::Method":
            continue
        props = (resource or {}).get("Properties") or {}
        if str(props.get("HttpMethod", "")).upper() == "OPTIONS":
            continue
        if props.get("ApiKeyRequired") is True:
            findings.append(
                Finding(
                    "public_auth_contract", path,
                    f"{logical_id}: explicit PUBLIC intent forbids ApiKeyRequired=true",
                )
            )
    if "ApiKeyId" in (doc.get("Outputs") or {}):
        findings.append(
            Finding(
                "public_auth_contract", path,
                "Outputs.ApiKeyId contradicts the explicit PUBLIC endpoint intent",
            )
        )
    return findings


# Vendored dependency closure for cdk-app builds (cdk-artifacts spec, supply-
# chain stance): generated apps may import ONLY these. The runner image bakes
# exactly this set; anything else fails loudly here AND at offline npm ci.
CDK_DEPENDENCY_CLOSURE = {
    "aws-cdk-lib", "constructs", "typescript", "ts-node", "aws-cdk", "@types/node",
}


def _closure_findings(artifacts: dict[str, str]) -> list[Finding]:
    package_json = artifacts.get("package.json")
    if package_json is None:
        return [Finding("dependency_closure", "package.json", "package.json is missing")]
    try:
        doc = json.loads(package_json)
    except json.JSONDecodeError as exc:
        return [Finding("dependency_closure", "package.json", f"Not valid JSON: {exc}")]
    declared: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        declared |= set((doc.get(section) or {}).keys())
    outside = sorted(declared - CDK_DEPENDENCY_CLOSURE)
    if outside:
        return [
            Finding(
                "dependency_closure", "package.json",
                f"Depends outside the vendored closure: {', '.join(outside)} "
                "(dependency freedom is a GA question — spec scope stance)",
            )
        ]
    return []


def _cdk_packaging_findings(doc: dict) -> list[Finding]:
    """cdk-app profile: Lambda code may be inline (small) OR parameterized S3
    assets (the legacy-synthesizer output our deployer satisfies)."""
    findings = []
    for logical_id, resource in (doc.get("Resources") or {}).items():
        if str((resource or {}).get("Type")) != "AWS::Lambda::Function":
            continue
        props = (resource or {}).get("Properties") or {}
        code = props.get("Code") or {}
        zipfile = code.get("ZipFile")
        if isinstance(zipfile, str):
            if len(zipfile) > MAX_INLINE_CHARS:
                findings.append(
                    Finding(
                        "inline_packaging", "synth/template.json",
                        f"{logical_id}: inline code is {len(zipfile)} chars — over "
                        f"the {MAX_INLINE_CHARS}-char inline ceiling. Prefer "
                        "lambda.Code.fromAsset (assets have no ceiling); rebuild "
                        "if the handler was meant to be small.",
                    )
                )
        elif not (isinstance(code.get("S3Bucket"), dict) or isinstance(code.get("S3Bucket"), str)):
            findings.append(
                Finding(
                    "inline_packaging", "synth/template.json",
                    f"{logical_id}: Lambda code must be inline ZipFile or an S3 asset reference",
                )
            )
        runtime = str(props.get("Runtime", ""))
        if runtime and runtime not in ALLOWED_RUNTIMES:
            findings.append(
                Finding(
                    "inline_packaging", "synth/template.json",
                    f"{logical_id}: runtime '{runtime}' not allowed ({', '.join(ALLOWED_RUNTIMES)})",
                )
            )
    return findings


def validate_artifacts(
    artifacts: dict[str, str],
    forbidden_patterns: list[str],
    *,
    profile: str = "inline-cfn",
    endpoint_auth: dict | None = None,
    require_api_key: bool | None = None,  # compatibility for focused tests/callers
    require_web_console: bool = False,
    declared_connectors: list[str] | None = None,
    mcp_tool_connectors: list[str] | None = None,
    agent_dependencies: list[str] | None = None,
    orchestrated: bool = False,
    require_memory: bool = False,
    require_packaged: bool = False,
    declared_tools: list[str] | None = None,
    require_planning: bool = False,
    loop_iteration_cap: int = 5,
) -> tuple[list[Finding], list[str]]:
    """Run every deterministic check against what CloudFormation executes.

    `endpoint_auth` is the B21 normalized build contract. The legacy bool is
    accepted only for low-churn tests; production passes the full decision.
    """
    if endpoint_auth is None and require_api_key is not None:
        endpoint_auth = {
            "mode": "key_required" if require_api_key else "open",
            "source": "legacy_test",
        }
    template_path = "synth/template.json" if profile == "cdk-app" else "template.json"
    template = artifacts.get(template_path)
    if template is None:
        return (
            [Finding("template_json", template_path, f"{template_path} was not generated")],
            [],
        )
    doc, findings = _template_findings(template, template_path)
    if doc is not None and not findings:
        findings += _allowlist_findings(doc, template_path)
        if profile == "cdk-app":
            findings += _cdk_packaging_findings(doc)
            findings += _closure_findings(artifacts)
        elif profile == "packaged-cfn":
            findings += _packaged_packaging_findings(doc, artifacts, template_path)
        else:
            findings += _packaging_findings(doc)
        # Agent substance R1: profile ↔ declaration symmetry, both directions.
        findings += _packaged_symmetry_findings(profile, require_packaged, template_path)

        if endpoint_auth is not None:
            findings += _auth_decision_findings(endpoint_auth, template_path)
            if endpoint_auth.get("mode") == "key_required":
                findings += _auth_findings(
                    doc,
                    template_path,
                    allow_console_exemption=require_web_console,
                )
            else:
                findings += _public_auth_findings(doc, template_path)

        if require_web_console:
            if profile == "cdk-app":
                findings.append(
                    Finding(
                        "web_console", template_path,
                        "Web test console v0 is supported only by inline-cfn; "
                        "use inline-cfn or remove the console requirement",
                    )
                )
            else:
                findings += _web_console_findings(
                    doc,
                    artifacts,
                    require_api_key=(endpoint_auth or {}).get("mode") == "key_required",
                    path=template_path,
                )
        # C1: connector custody boundaries — runs for EVERY build (an empty
        # declaration list enforces zero-connector cleanliness).
        if declared_connectors and profile == "cdk-app":
            findings.append(
                Finding(
                    "connector_contract", template_path,
                    "Connector consumption v1 is supported only by inline-cfn; "
                    "use inline-cfn or remove the connector declaration",
                )
            )
        else:
            findings += _connector_findings(
                doc, artifacts, declared_connectors or [], template_path
            )
        # Agent substance: memory contract, always-on (symmetry both ways).
        if require_memory and profile == "cdk-app":
            findings.append(
                Finding(
                    "memory_contract", template_path,
                    "Conversation memory v1 is supported only by inline-cfn",
                )
            )
        else:
            findings += _memory_findings(doc, artifacts, require_memory, template_path)
        # Composable agents: declared-only inter-agent reach, always-on.
        if agent_dependencies and profile == "cdk-app":
            findings.append(
                Finding(
                    "agent_dependency", template_path,
                    "Agent composition v1 is supported only by inline-cfn",
                )
            )
        else:
            findings += _agent_dependency_findings(
                doc, artifacts, agent_dependencies or [], orchestrated, template_path
            )
        # C3 v1: MCP tool-loop contract — also always-on (undeclared scaffold
        # artifacts are refused the same way undeclared connector reach is).
        if mcp_tool_connectors and profile != "inline-cfn":
            findings.append(
                Finding(
                    "mcp_tool_loop", template_path,
                    "MCP tool-loop agents v1 are supported only by inline-cfn "
                    "(the byte-equality contract lives in the inline ZipFile)",
                )
            )
        else:
            findings += _mcp_tool_loop_findings(
                doc, artifacts, mcp_tool_connectors or [], template_path
            )
        # Agent substance R2.2/R2.3: one conversation loop per agent, then the
        # tools/planning contracts (always-on for the symmetric refusals).
        findings += _rung_combo_findings(
            mcp_tool_connectors or [], declared_tools or [], require_planning
        )
        if (declared_tools or require_planning) and profile == "cdk-app":
            findings.append(
                Finding(
                    "rung_combination", template_path,
                    "Tools/planning rungs v1 are supported only by "
                    "inline-cfn/packaged-cfn",
                )
            )
        else:
            findings += _tools_findings(
                doc, artifacts, declared_tools or [], loop_iteration_cap, template_path
            )
            findings += _planning_findings(
                doc, artifacts, require_planning, loop_iteration_cap, template_path
            )
        findings += _contract_findings(doc, template, artifacts, template_path)
    guardrail, unenforceable = _guardrail_findings(forbidden_patterns, artifacts)
    return findings + guardrail, unenforceable
