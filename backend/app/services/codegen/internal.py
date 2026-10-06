"""Internal codegen provider: Bedrock synthesis of a serverless artifact set
(codegen-handoff spec R1).

Output contract (enforced downstream by the validation gate, R3):
- `template.json` — CloudFormation JSON, §4.1.4-allowlisted resources only,
  Lambda code INLINE via ZipFile (the S1 sample-app packaging path — the
  engine deploys raw templates; no cdk/synth toolchain on the platform),
  `ApiUrl` output (the deploy console contract).
- source files under `src/` — the same code embedded in the template,
  browsable/downloadable as first-class artifacts.
- `README.md` — what was built + how to exercise it.

Generation is chunked per-file with token budgets (the S2 truncation lesson);
every invocation flows through the bedrock seam with purpose="codegen"
(NOT cap-exempt — R7).
"""

import json
import logging
import re
import sys

from app.services.bedrock import InvocationCtx, converse
from app.services.codegen.literals import literal_contract_block
from app.services.codegen.provider import BuildCtx, BuildPlan, PlannedFile
from app.services.codegen.scaffolds import (
    MCP_AGENT_FILENAME,
    MCP_AGENT_SCAFFOLD,
    PLANNING_AGENT_FILENAME,
    PLANNING_AGENT_SCAFFOLD,
    TOOLS_AGENT_FILENAME,
    assemble_tools_agent,
)
from app.services.codegen.validate import connector_env_name, dependency_env_name

logger = logging.getLogger("marshal.codegen")

PLAN_SYSTEM = """You are the code-generation planner for marshal. Given an agent's \
requirements, design and task documents, plan a SMALL deployable serverless AI agent \
implementing the core idea (an Alpha walking skeleton, not the full system).

Hard constraints:
- AWS services limited to: Lambda, API Gateway (REST), DynamoDB, S3, SQS, SNS, Step Functions, EventBridge, CloudWatch, IAM roles for wiring.
- Lambda functions: Python 3.12, stdlib + boto3 ONLY, each function body must stay under 3200 characters (it is embedded inline in CloudFormation ZipFile).
- Use at most 3 Lambda functions and 5 planned files total (including README). If requirements expose more than 3 distinct business routes, plan exactly 3 src/*.py handlers grouped by cohesive responsibility; for 2-3 routes use at most 2 handlers; otherwise prefer one. Each handler routes by HTTP method and path.
- The app must expose an HTTP API (API Gateway REST) as its entry point.

Reply with ONLY a JSON object (no fences, no prose):
{
  "app_name": "short-kebab-name",
  "architecture_notes": "2-4 sentences on what the demo does and how",
  "files": [
    {"path": "src/<name>.py", "brief": "what this handler does", "language": "python"},
    {"path": "README.md", "brief": "usage + what was built", "language": "markdown"}
  ]
}
Rules for files: every Lambda handler is one src/*.py file (handler function named `handler`); \
include README.md LAST; do NOT include template.json in the list (it is assembled separately)."""

# Decimal-safe JSON (hotfix 6 Oct 2026): boto3's DynamoDB resource/Table API
# returns numbers as decimal.Decimal; json.dumps raises on them and the
# deployed handler answers 500. Stated in every Python file-generation
# system prompt AND carried as a verbatim helper in the per-file prompt.
DECIMAL_JSON_RULE = (
    "DynamoDB's boto3 resource/Table API returns numbers as decimal.Decimal, which "
    "json.dumps cannot serialize: never json.dumps an item, a query result or "
    "anything built from one directly — build every HTTP response with the "
    "respond(status, body) helper supplied in the request "
    "(json.dumps(..., default=_json_default)) or convert Decimals first."
)

FILE_SYSTEM = f"""You are generating one file of a small serverless AWS demo app for marshal.
Constraints: Python 3.12, stdlib + boto3 only, no pip installs, handler function named `handler`, \
under 3200 characters total, no placeholders or TODOs — complete working code. \
Resource names arrive via environment variables (e.g. TABLE_NAME). \
{DECIMAL_JSON_RULE} \
Reply with ONLY the raw file content (no markdown fences, no commentary)."""

TEMPLATE_SYSTEM = """You are assembling the CloudFormation template for a small serverless demo app.
Produce a SINGLE CloudFormation JSON document (not YAML) and reply with ONLY that JSON — no fences, no prose.

Hard rules:
- Resource types limited to: AWS::Lambda::Function, AWS::Lambda::Permission, AWS::ApiGateway::* , AWS::DynamoDB::Table, AWS::S3::Bucket, AWS::SQS::Queue, AWS::SNS::Topic, AWS::StepFunctions::StateMachine, AWS::Events::Rule, AWS::Logs::LogGroup, AWS::IAM::Role, AWS::IAM::Policy.
- For each planned Lambda, set "Code": {"ZipFile": "<its exact __MARSHAL_SOURCE_N__ placeholder from the request>"}; never copy planned source text into the response because the platform injects it after parsing. Fixed platform helper Lambdas may use exact inline code supplied by the request. Set "Handler": "index.handler", "Runtime": "python3.12", "Timeout": 30.
- IAM roles: least privilege, scoped to the resources in this template; managed policy AWSLambdaBasicExecutionRole for logs.
- Wire environment variables for resource names (TABLE_NAME etc.) via Ref/GetAtt.
- API Gateway: RestApi + resources/methods (AWS_PROXY integrations) + Deployment + Stage "prod"; add AWS::Lambda::Permission for apigateway.amazonaws.com per function.
- Emit token-minimal MINIFIED JSON on one line. Omit optional Description, Metadata, Tags, and all nonfunctional prose. Use short logical IDs except exact IDs required by the specification, and do not duplicate functions, roles, policies, permissions, or API resources.
- Outputs MUST include "ApiUrl": {"Value": {"Fn::Sub": "https://${RestApi}.execute-api.${AWS::Region}.amazonaws.com/prod"}} (adjust logical id). Every output is an object with a "Value" key — never a bare Ref/Fn::Sub.
- No parameters requiring input; the template must deploy with no arguments."""


PLAN_SYSTEM_PACKAGED = """You are the code-generation planner for marshal. Given an agent's \
requirements, design and task documents, plan a SMALL deployable serverless AI agent \
implementing the core idea (an Alpha walking skeleton, not the full system) with a \
PACKAGED Lambda deployment (third-party Python dependencies allowed).

Hard constraints:
- AWS services limited to: Lambda, API Gateway (REST), DynamoDB, S3, SQS, SNS, Step Functions, EventBridge, CloudWatch, IAM roles for wiring.
- Lambda functions: Python 3.12, each function body under 11000 characters. Third-party libraries are allowed ONLY if listed in requirements.txt.
- Plan a `requirements.txt` file (language "text") listing the third-party dependencies as EXACT pins (name==version), at most 8, no ranges or URLs. Include ONLY libraries the handlers genuinely import.
- Use at most 3 Lambda functions and 6 planned files total (including requirements.txt and README). Each handler routes by HTTP method and path.
- The app must expose an HTTP API (API Gateway REST) as its entry point.

Reply with ONLY a JSON object (no fences, no prose):
{
  "app_name": "short-kebab-name",
  "architecture_notes": "2-4 sentences on what the demo does and how",
  "files": [
    {"path": "src/<name>.py", "brief": "what this handler does", "language": "python"},
    {"path": "requirements.txt", "brief": "exact-pinned dependencies", "language": "text"},
    {"path": "README.md", "brief": "usage + what was built", "language": "markdown"}
  ]
}
Rules for files: every Lambda handler is one src/*.py file (handler function named `handler`); \
include requirements.txt BEFORE README.md and README.md LAST; do NOT include template.json \
in the list (it is assembled separately)."""

PLAN_SYSTEM_PACKAGED_STDLIB = """You are the code-generation planner for marshal. Given an agent's \
requirements, design and task documents, plan a SMALL deployable serverless AI agent \
implementing the core idea (an Alpha walking skeleton, not the full system) with a \
PACKAGED Lambda deployment (code ships as a zip asset, so handlers may be larger).

Hard constraints:
- AWS services limited to: Lambda, API Gateway (REST), DynamoDB, S3, SQS, SNS, Step Functions, EventBridge, CloudWatch, IAM roles for wiring.
- Lambda functions: Python 3.12, stdlib + boto3 ONLY (no third-party libraries — none are installed), each function body under 11000 characters.
- Plan a `requirements.txt` file (language "text") that will be EMPTY — it exists to record that nothing beyond the runtime is used.
- Use at most 3 Lambda functions and 6 planned files total (including requirements.txt and README). Each handler routes by HTTP method and path.
- The app must expose an HTTP API (API Gateway REST) as its entry point.

Reply with ONLY a JSON object (no fences, no prose):
{
  "app_name": "short-kebab-name",
  "architecture_notes": "2-4 sentences on what the demo does and how",
  "files": [
    {"path": "src/<name>.py", "brief": "what this handler does", "language": "python"},
    {"path": "requirements.txt", "brief": "empty — runtime only", "language": "text"},
    {"path": "README.md", "brief": "usage + what was built", "language": "markdown"}
  ]
}
Rules for files: every Lambda handler is one src/*.py file (handler function named `handler`); \
include requirements.txt BEFORE README.md and README.md LAST; do NOT include template.json \
in the list (it is assembled separately)."""

FILE_SYSTEM_PACKAGED_STDLIB = f"""You are generating one file of a small serverless AWS demo app for marshal.
Constraints: Python 3.12, stdlib + boto3 ONLY (no third-party libraries — none are installed), \
handler function named `handler`, under 11000 characters total, no placeholders or TODOs — \
complete working code. Resource names arrive via environment variables (e.g. TABLE_NAME). \
{DECIMAL_JSON_RULE} \
Reply with ONLY the raw file content (no markdown fences, no commentary)."""

FILE_SYSTEM_PACKAGED = f"""You are generating one file of a small serverless AWS demo app for marshal.
Constraints: Python 3.12, handler function named `handler`, under 11000 characters total, \
no placeholders or TODOs — complete working code. Third-party imports are allowed ONLY for \
libraries pinned in the planned requirements.txt; stdlib + boto3 need no pin. \
Resource names arrive via environment variables (e.g. TABLE_NAME). \
{DECIMAL_JSON_RULE} \
Reply with ONLY the raw file content (no markdown fences, no commentary)."""

TEMPLATE_SYSTEM_PACKAGED = """You are assembling the CloudFormation template for a small serverless demo app whose \
business Lambda code ships as a PACKAGED zip the deployer stages to S3.
Produce a SINGLE CloudFormation JSON document (not YAML) and reply with ONLY that JSON — no fences, no prose.

Hard rules:
- Resource types limited to: AWS::Lambda::Function, AWS::Lambda::Permission, AWS::ApiGateway::* , AWS::DynamoDB::Table, AWS::S3::Bucket, AWS::SQS::Queue, AWS::SNS::Topic, AWS::StepFunctions::StateMachine, AWS::Events::Rule, AWS::Logs::LogGroup, AWS::IAM::Role, AWS::IAM::Policy.
- Declare EXACTLY two input Parameters, both {"Type": "String"}: "PackageBucket" and "PackageKey". The deployer binds them; add no other parameters.
- For each planned Lambda listed in the request, set "Code": {"S3Bucket": {"Ref": "PackageBucket"}, "S3Key": {"Ref": "PackageKey"}} and "Handler": "<handler_module>.handler" using the module name from the request. Set "Runtime": "python3.12", "Timeout": 30.
- Fixed platform helper Lambdas (web console) may use exact inline ZipFile code supplied by the request, with "Handler": "index.handler".
- IAM roles: least privilege, scoped to the resources in this template; managed policy AWSLambdaBasicExecutionRole for logs.
- Wire environment variables for resource names (TABLE_NAME etc.) via Ref/GetAtt.
- API Gateway: RestApi + resources/methods (AWS_PROXY integrations) + Deployment + Stage "prod"; add AWS::Lambda::Permission for apigateway.amazonaws.com per function.
- Emit token-minimal MINIFIED JSON on one line. Use short logical IDs except exact IDs required by the specification.
- Outputs MUST include "ApiUrl": {"Value": {"Fn::Sub": "https://${RestApi}.execute-api.${AWS::Region}.amazonaws.com/prod"}} (adjust logical id). Every output is an object with a "Value" key — never a bare Ref/Fn::Sub."""


PLAN_SYSTEM_CDK = """You are the code-generation planner for marshal. Given an agent's \
requirements, design and task documents, plan a SMALL deployable serverless AI agent \
implementing the core idea as an AWS CDK (v2, TypeScript) application.

Hard constraints:
- AWS services limited to: Lambda, API Gateway (REST), DynamoDB, S3, SQS, SNS, Step Functions, EventBridge, CloudWatch.
- Lambda handlers: Python 3.12, stdlib + boto3 ONLY, each in its own directory src/<name>/index.py with a function named `handler`. At most 3 handlers.
- Exactly ONE stack file: lib/app-stack.ts. Do NOT plan bin/app.ts, package.json, cdk.json or tsconfig.json (the platform templates those).
- The app must expose an HTTP API (API Gateway REST).

Reply with ONLY a JSON object (no fences, no prose):
{
  "app_name": "short-kebab-name",
  "architecture_notes": "2-4 sentences on what the demo does and how",
  "files": [
    {"path": "lib/app-stack.ts", "brief": "the CDK stack wiring", "language": "typescript"},
    {"path": "src/<name>/index.py", "brief": "what this handler does", "language": "python"},
    {"path": "README.md", "brief": "usage + npm ci && npx cdk deploy instructions", "language": "markdown"}
  ]
}
Rules: lib/app-stack.ts FIRST, README.md LAST."""

TS_FILE_SYSTEM = """You are generating the CDK v2 TypeScript stack file for a small serverless AWS demo app.
Hard rules:
- Imports ONLY from "aws-cdk-lib", "constructs" (the vendored closure — nothing else resolves).
- Export exactly: `export class AppStack extends cdk.Stack` with the standard (scope, id, props?) constructor.
- Lambda functions: `new lambda.Function(...)` with `code: lambda.Code.fromAsset("src/<name>")`, `handler: "index.handler"`, `runtime: lambda.Runtime.PYTHON_3_12`, timeout 30s. NEVER use NodejsFunction or bundling options.
- API Gateway: `new apigateway.RestApi` (or LambdaRestApi) wiring the handlers.
- Wire resource names into handler environment variables (TABLE_NAME etc.).
- End with: `new cdk.CfnOutput(this, "ApiUrl", { value: api.url });` (the deploy console contract).
- Least-privilege grants (table.grantReadWriteData(fn) etc.). No custom IAM statements unless required.
Reply with ONLY the raw TypeScript file content (no markdown fences, no commentary)."""


def _replace_logical_id_refs(value, old: str, new: str) -> None:
    """Update CloudFormation references after normalizing one logical ID."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "Ref" and child == old:
                value[key] = new
            elif (
                key == "Fn::GetAtt"
                and isinstance(child, list)
                and child
                and child[0] == old
            ):
                child[0] = new
            elif key == "Fn::Sub" and isinstance(child, str):
                value[key] = child.replace(f"${{{old}}}", f"${{{new}}}")
            else:
                _replace_logical_id_refs(child, old, new)
    elif isinstance(value, list):
        for child in value:
            _replace_logical_id_refs(child, old, new)
    elif isinstance(value, str):
        # Strings are immutable; Fn::Sub replacements are handled by their
        # containing mapping below when that mapping is visited.
        return


class SizeEscalationDependencyError(ValueError):
    """A size-escalated (platform-chosen packaged) build regenerated handlers
    that import third-party modules the spec never declared (13.5AA)."""

    def __init__(self, modules: list[str]):
        self.modules = list(modules)
        super().__init__(
            "size escalation regenerated handlers with third-party imports "
            f"({', '.join(self.modules)}) — escalation lifts the inline SIZE "
            "limit only; declare 'SHALL use packaged dependencies' in the "
            "requirements document to allow libraries beyond stdlib + boto3"
        )


# Modules the Lambda Python runtime provides without a pin (13.5Z).
_RUNTIME_PROVIDED_MODULES = frozenset(
    set(sys.stdlib_module_names) | {"boto3", "botocore", "s3transfer", "urllib3"}
)
_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w]*)|import\s+([A-Za-z_][\w]*))", re.M)


def third_party_imports(sources: dict[str, str]) -> list[str]:
    """Top-level modules imported by the generated Python sources that the
    Lambda runtime does NOT provide — the only thing requirements.txt may
    legitimately contain. Deterministic: derived from code, not asked of a
    model. Relative imports (`from .x`) don't match the pattern and are
    correctly ignored (they're sibling modules in the zip)."""
    found: set[str] = set()
    for path, content in sources.items():
        if not path.endswith(".py"):
            continue
        for match in _IMPORT_RE.finditer(content):
            module = (match.group(1) or match.group(2) or "").split(".")[0]
            if module and module not in _RUNTIME_PROVIDED_MODULES:
                found.add(module)
    return sorted(found)


def pin_lines_only(text: str) -> str:
    """Keep only lines that ARE exact pins (contract.PIN_RE). Everything else —
    prose, bullets, fences, comments — is dropped, so a narrating model can
    never smuggle non-manifest text into the deliverable."""
    from app.services.codegen.contract import PIN_RE

    kept = [
        line.strip() for line in text.splitlines()
        if line.strip() and PIN_RE.match(line.strip())
    ]
    return ("\n".join(kept) + "\n") if kept else ""


def _enforce_preview_only_topic(ctx: BuildCtx, document: dict) -> None:
    """Honor the explicit non-deployed Approval SNS preview marker exactly.

    The candidate is intentionally never deployed. Model generation supplies
    the rest of the candidate; this deterministic normalization prevents the
    exact preview topic's logical-ID drift or omission from weakening evidence.
    """
    design = str(ctx.spec_docs.get("design", ""))
    marker = "## Rehearsal-only SNS candidate (DO NOT DEPLOY)"
    logical_id = "ApprovalDecisionTopic"
    resource_type = "AWS::SNS::Topic"
    if marker not in design or logical_id not in design or resource_type not in design:
        return
    resources = document.get("Resources")
    if not isinstance(resources, dict):
        raise ValueError("Preview candidate template has no Resources object")
    topics = [
        name
        for name, resource in resources.items()
        if isinstance(resource, dict) and resource.get("Type") == resource_type
    ]
    if logical_id in topics:
        if len(topics) != 1:
            raise ValueError("Preview candidate must contain exactly one SNS topic")
        return
    if len(topics) > 1:
        raise ValueError("Preview candidate generated multiple SNS topics")
    if topics:
        old = topics[0]
        resources[logical_id] = resources.pop(old)
        _replace_logical_id_refs(document, old, logical_id)
    else:
        resources[logical_id] = {"Type": resource_type}


def _clip(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[:budget] + "\n\n[...clipped for planning...]"


# B19 (web-test-console R2.1): the console page generator. Self-contained is
# a hard rule — no CDN scripts, no external fonts; the page must work inside
# the Enclave with nothing but the API it fronts.
def _web_console_system(require_api_key: bool, require_memory: bool = False) -> str:
    memory_block = (
        "\n- The agent keeps conversation memory: generate ONE session id per "
        "page load (crypto.randomUUID(), plain JS variable) and include it as "
        '"session_id" in every request body, so replies can reference earlier '
        "turns."
        if require_memory
        else ""
    )
    key_block = (
        "\n- The API requires an API key: render an 'API key' password input at the top; "
        "keep its value ONLY in a JS variable (never localStorage, never hardcoded) and "
        "send it as the `x-api-key` header on every request. Show a hint that the owner "
        "reveals the key on the deployment card."
        if require_api_key
        else ""
    )
    return (
        "You are generating web/index.html: a SINGLE self-contained interactive test "
        "console for a small serverless API. It is served at <stage>/app on the SAME "
        "API Gateway as the API, and API Gateway URLs carry the stage in the path — "
        "so NEVER use root-relative paths (fetch('/notes') drops the stage and 403s). "
        "Define EXACTLY this base once and use it for every call:\n"
        "const BASE = location.pathname.replace(/\\/app\\/?$/, '');\n"
        "then fetch(BASE + '/notes') etc.\n"
        "Hard rules:\n"
        "- One file: inline <style> and <script> only. NO external resources, CDNs, "
        "fonts, or frameworks. Vanilla JS.\n"
        "- Dark, clean layout. A form per API operation (inputs derived from the "
        "request fields), a Send button, and a response pane showing status + "
        "pretty-printed JSON.\n"
        "- NEVER embed credentials, tokens or keys of any kind."
        f"{key_block}{memory_block}\n"
        "- Under 18000 characters. Reply with ONLY the raw HTML (no fences)."
    )


def _auth_generation_block(ctx: BuildCtx, *, cdk: bool = False) -> str:
    auth = ctx.endpoint_auth or {"mode": "key_required", "source": "default"}
    if auth.get("source") == "conflict":
        return (
            "\nENDPOINT AUTH CONFLICT: requirements demand both key protection and "
            "public access. Keep the safer keyed shape; the platform gate will "
            "fail until the author resolves the contradiction.\n"
        )
    if auth.get("mode") == "open":
        return (
            "\nENDPOINT AUTHENTICATION: explicitly PUBLIC. Do not create an API key, "
            "usage plan, UsagePlanKey or ApiKeyId output. Every business method "
            "must have API-key protection disabled.\n"
        )
    if cdk:
        return (
            "\nENDPOINT AUTHENTICATION: API KEY REQUIRED (platform default unless "
            "the spec explicitly opts out). In lib/app-stack.ts use RestApi and "
            "explicit methods; create api.addApiKey, api.addUsagePlan with "
            "apiStages [{api, stage: api.deploymentStage}], then addApiKey on the "
            "plan. Every business addMethod call MUST set apiKeyRequired: true; "
            "OPTIONS stays false. End with CfnOutput ApiKeyId = apiKey.keyId.\n"
        )
    return (
        "\nENDPOINT AUTHENTICATION: API KEY REQUIRED (platform default unless the "
        "spec explicitly opts out). The template must create ApiKey + UsagePlan "
        "bound to prod + UsagePlanKey; every non-OPTIONS business method has "
        "ApiKeyRequired true; output ApiKeyId.\n"
    )


# C1 connector-lite (external-import-connectors spec): the connector-call
# helper is spelled VERBATIM — secret shape, env naming, header injection and
# timeout are the fiddly parts, so they carry zero generation variance (the
# B19 console-handler discipline). The deployer provisions the secrets at
# marshal/agent-connectors/<slug> before stack create; the composite payload
# is {"base_url","header","value"} with value null when no credential exists.
CONNECTOR_HELPER_CODE = '''_CONN = {}
def call_connector(slug, path, payload=None, method=None):
    if slug not in _CONN:
        sid = os.environ["CONNECTOR_" + slug.upper().replace("-", "_") + "_SECRET"]
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=sid)["SecretString"]
        _CONN[slug] = json.loads(raw)
    c = _CONN[slug]
    url = c["base_url"].rstrip("/") + "/" + str(path).lstrip("/")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if c.get("value"):
        req.add_header(c.get("header") or "Authorization", c["value"])
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}'''


# Agent substance R2.1: memory helpers spelled VERBATIM (TTL, caps and item
# shape carry zero generation variance). Table + IAM wiring is template-side.
MEMORY_HELPER_CODE = '''def remember(session_id, role, text):
    boto3.client("dynamodb").put_item(TableName=os.environ["MEMORY_TABLE"], Item={
        "session_id": {"S": str(session_id)[:128]}, "sk": {"S": str(int(time.time() * 1000))},
        "role": {"S": str(role)[:16]}, "text": {"S": str(text)[:4000]},
        "expires_at": {"N": str(int(time.time()) + 30 * 86400)}})

def recall(session_id, limit=20):
    r = boto3.client("dynamodb").query(TableName=os.environ["MEMORY_TABLE"],
        KeyConditionExpression="session_id = :s",
        ExpressionAttributeValues={":s": {"S": str(session_id)[:128]}},
        ScanIndexForward=False, Limit=limit)
    return [(i["role"]["S"], i["text"]["S"]) for i in reversed(r.get("Items", []))]'''


def _memory_generation_block(ctx: BuildCtx) -> str:
    if not ctx.require_memory:
        return ""
    return (
        "\nCONVERSATION MEMORY (the spec demands it):\n"
        "Handlers accept a client-supplied `session_id` field. Include EXACTLY "
        "these helpers (verbatim; imports os, time, boto3 present) and use them "
        "— recall(session_id) before building the model/business context, "
        "remember(session_id, role, text) after producing a reply:\n"
        f"{MEMORY_HELPER_CODE}\n"
        "Requests without a session_id run memoryless (never fail on it).\n"
    )


# Decimal-safe responses, spelled VERBATIM (the same zero-variance discipline
# as the connector/memory helpers). Integral Decimals become int, others
# float; every other unknown type still raises, so nothing is hidden.
JSON_RESPONSE_HELPER_CODE = '''def _json_default(o):
    if isinstance(o, decimal.Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"{type(o).__name__} is not JSON serializable")

def respond(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default)}'''


def _json_response_block() -> str:
    return (
        "\nJSON RESPONSES (every Python handler): DynamoDB's boto3 resource/Table "
        "API returns numbers as decimal.Decimal and json.dumps raises TypeError on "
        "them (the deployed handler then answers HTTP 500). Include EXACTLY this "
        "helper (verbatim; imports json and decimal present) and build EVERY HTTP "
        "response through respond(status, body) — never json.dumps an item, a "
        "query result or anything derived from one without default=_json_default:\n"
        f"{JSON_RESPONSE_HELPER_CODE}\n"
    )


# Composable agents R2.1: the inter-agent call helper is spelled VERBATIM —
# secret shape, env naming, attribution header and timeout carry zero
# generation variance. The deployer provisions marshal/agent-dependencies/
# <slug> with {"base_url","header","value"} before stack create.
AGENT_CALL_HELPER_CODE = '''_DEPS = {}
def call_agent(slug, path="", payload=None):
    if slug not in _DEPS:
        sid = os.environ["AGENT_DEP_" + slug.upper().replace("-", "_") + "_SECRET"]
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=sid)["SecretString"]
        _DEPS[slug] = json.loads(raw)
    d = _DEPS[slug]
    url = d["base_url"].rstrip("/") + ("/" + str(path).lstrip("/") if path else "")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-marshal-caller", os.environ.get("MARSHAL_AGENT_ID", "unknown"))
    if d.get("value"):
        req.add_header(d.get("header") or "x-api-key", d["value"])
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}'''


def _dependency_generation_block(ctx: BuildCtx) -> str:
    if not ctx.agent_dependencies:
        return ""
    slugs = ", ".join(ctx.agent_dependencies)
    pipeline = (
        "\nThis agent ORCHESTRATES the listed agents as a pipeline: the "
        "coordinator handler calls each one in declared order via "
        "call_agent(slug, path, payload), passing each result into the next, "
        "with a typed error surface when a step fails (never silent).\n"
        if ctx.orchestrated
        else ""
    )
    return (
        f"\nAGENT DEPENDENCIES (deployed marshal agents this agent calls): {slugs}.\n"
        "Where a handler calls one, include EXACTLY this helper (verbatim, with "
        "imports os, json, boto3, urllib.request present):\n"
        f"{AGENT_CALL_HELPER_CODE}\n"
        "Dependency endpoints and keys arrive through platform-provisioned "
        "secrets — never hardcode URLs or keys. A failed dependency call must "
        "surface as a typed error in this agent's response, never a silent "
        f"fallback.\n{pipeline}"
    )


def _mcp_plan_block(ctx: BuildCtx) -> str:
    if not ctx.mcp_tool_connectors:
        return ""
    return (
        f"\nThis agent uses MCP tools from connector "
        f"'{ctx.mcp_tool_connectors[0]}'. The platform supplies "
        f"{MCP_AGENT_FILENAME} as the PRIMARY API handler (a Converse tool "
        "loop — do NOT plan its content or a competing handler for the main "
        "route). Plan at most one additional trivial handler only if the "
        "spec demands separate auxiliary routes.\n"
    )


def _tools_plan_block(ctx: BuildCtx) -> str:
    if not ctx.declared_tools:
        return ""
    return (
        f"\nThis agent uses declared local tools: {', '.join(ctx.declared_tools)}. "
        f"The platform supplies {TOOLS_AGENT_FILENAME} as the PRIMARY API "
        "handler (a Converse tool loop over locally implemented tool "
        "functions — do NOT plan a competing handler for the main route). "
        "Plan at most one additional trivial handler only if the spec "
        "demands separate auxiliary routes.\n"
    )


def _planning_plan_block(ctx: BuildCtx) -> str:
    if not ctx.require_planning:
        return ""
    return (
        f"\nThis agent plans multi-step responses. The platform supplies "
        f"{PLANNING_AGENT_FILENAME} as the PRIMARY API handler (a bounded "
        "plan-then-execute Converse loop — do NOT plan its content or a "
        "competing handler for the main route). Plan at most one additional "
        "trivial handler only if the spec demands separate auxiliary routes.\n"
    )


def _connector_generation_block(ctx: BuildCtx) -> str:
    if not ctx.declared_connectors:
        return ""
    slugs = ", ".join(ctx.declared_connectors)
    return (
        f"\nCONNECTORS (registered external systems this agent calls): {slugs}.\n"
        "Where a handler needs one, include EXACTLY this helper (verbatim, with "
        "imports os, json, boto3, urllib.request present) and call it as "
        "call_connector('<slug>', '<path>', payload):\n"
        f"{CONNECTOR_HELPER_CODE}\n"
        "Never hardcode connector URLs or credentials; the platform provisions "
        "the secret and the environment variable at deploy time.\n"
    )


def _readme_auth_section(ctx: BuildCtx) -> str:
    if (ctx.endpoint_auth or {}).get("mode") == "open":
        return (
            "\n\n## Endpoint authentication\n\n"
            "This API is deliberately public without authentication because the "
            "requirements explicitly opted out. Treat every route as internet-accessible.\n"
        )
    console = (
        " The `/app` test page loads without a key so you can paste it at runtime; "
        "the page keeps the value in memory only."
        if ctx.require_web_console
        else ""
    )
    return (
        "\n\n## Endpoint authentication\n\n"
        "All business routes require an API key. The deployment owner uses "
        "**Reveal key** on the deployment card, then sends it as `x-api-key`:\n\n"
        "```bash\ncurl -H 'x-api-key: <REVEALED_API_KEY>' '<API_URL>/<route>'\n```\n"
        "Never embed the key in source or artifacts." + console + "\n"
    )


class InternalProvider:
    name = "internal"
    mode = "inprocess"

    def _ctx(self, ctx: BuildCtx) -> InvocationCtx:
        return InvocationCtx(
            purpose="codegen",
            user_id=ctx.user_id,
            project_id=ctx.project_id,
            generation_id=ctx.build_id,  # generic job-attribution column
        )

    async def plan(self, ctx: BuildCtx, *, profile: str = "inline-cfn") -> BuildPlan:
        rules = (
            "\nTemplate architecture rules that MUST hold:\n- " + "\n- ".join(ctx.guardrail_rules)
            if ctx.guardrail_rules
            else ""
        )
        # B19: the console page is planned as an artifact; the SERVING
        # infrastructure is template-only (fixed code at assembly).
        console = (
            '\nThe spec requires a web test page: include {"path": "web/index.html", '
            '"brief": "self-contained interactive test console for the API", '
            '"language": "html"} in files (before README.md). Do NOT plan a '
            "handler for serving it — the platform wires that.\n"
            if ctx.require_web_console
            else ""
        )
        prompt = (
            f"Agent: {ctx.project_name}\n\n"
            f"REQUIREMENTS:\n{_clip(ctx.spec_docs.get('requirements', ''), 16000)}\n\n"
            f"DESIGN:\n{_clip(ctx.spec_docs.get('design', ''), 16000)}\n\n"
            f"TASKS:\n{_clip(ctx.spec_docs.get('tasks', ''), 8000)}\n"
            f"{rules}{console}{_auth_generation_block(ctx, cdk=profile == 'cdk-app')}"
            f"{_connector_generation_block(ctx)}{_dependency_generation_block(ctx)}"
            f"{_mcp_plan_block(ctx)}{_tools_plan_block(ctx)}{_planning_plan_block(ctx)}"
            f"{_memory_generation_block(ctx)}"
            f"{literal_contract_block(ctx.literal_contract)}\n\nPlan the demo now."
        )
        if profile == "cdk-app":
            plan_system = PLAN_SYSTEM_CDK
        elif profile == "packaged-cfn" and ctx.size_escalated:
            plan_system = PLAN_SYSTEM_PACKAGED_STDLIB
        elif profile == "packaged-cfn":
            plan_system = PLAN_SYSTEM_PACKAGED
        else:
            plan_system = PLAN_SYSTEM
        text, _usage, stop = await converse(
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=plan_system,
            model_id=ctx.model_id,
            max_tokens=2000,
            ctx=self._ctx(ctx),
        )
        if stop == "max_tokens":
            raise ValueError("Plan generation hit the token cap — retry the build")
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`\n")
            raw = raw[raw.find("{"):]
        try:
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Planner returned unparseable JSON: {exc}") from exc
        files = [
            PlannedFile(
                path=str(f.get("path", "")).strip(),
                brief=str(f.get("brief", ""))[:500],
                language=str(f.get("language", "python")),
            )
            for f in data.get("files", [])
            if str(f.get("path", "")).strip()
        ][:6]
        if ctx.mcp_tool_connectors and not any(
            f.path == MCP_AGENT_FILENAME for f in files
        ):
            # C3 v1: the tool-loop handler is ALWAYS present and ALWAYS
            # platform-supplied — deterministic append, never model-optional.
            files.insert(
                0,
                PlannedFile(
                    path=MCP_AGENT_FILENAME,
                    brief="MCP tool-loop agent handler (platform verbatim scaffold)",
                    language="python",
                ),
            )
            files = files[:6]
        # Agent substance R2.2/R2.3: the rung handlers are equally
        # deterministic — reserved files, never model-optional.
        if ctx.declared_tools and not any(f.path == TOOLS_AGENT_FILENAME for f in files):
            files.insert(
                0,
                PlannedFile(
                    path=TOOLS_AGENT_FILENAME,
                    brief="tool-loop agent handler (platform prelude + tool bodies)",
                    language="python",
                ),
            )
            files = files[:7]  # never truncate README off the tail
        if ctx.require_planning and not any(
            f.path == PLANNING_AGENT_FILENAME for f in files
        ):
            files.insert(
                0,
                PlannedFile(
                    path=PLANNING_AGENT_FILENAME,
                    brief="planning-loop agent handler (platform verbatim scaffold)",
                    language="python",
                ),
            )
            files = files[:7]
        if profile == "packaged-cfn" and not any(
            f.path == "requirements.txt" for f in files
        ):
            # Agent substance R1.3: the dependency manifest is the deliverable.
            files.insert(
                max(len(files) - 1, 0),
                PlannedFile(
                    path="requirements.txt",
                    brief="exact-pinned third-party dependencies",
                    language="text",
                ),
            )
            files = files[:7]
        if not any(f.path.endswith(".py") for f in files):
            raise ValueError("Planner produced no source files")
        return BuildPlan(
            app_name=str(data.get("app_name", ctx.project_name))[:60],
            architecture_notes=str(data.get("architecture_notes", ""))[:2000],
            files=files,
        )

    async def generate_file(
        self, ctx: BuildCtx, plan: BuildPlan, file: PlannedFile, generated: dict[str, str]
    ) -> str:
        # C3 v1: the MCP tool-loop handler is verbatim platform code — no
        # model call, zero variance, byte-equality gate-enforced downstream.
        if file.path == MCP_AGENT_FILENAME and ctx.mcp_tool_connectors:
            return MCP_AGENT_SCAFFOLD
        # Agent substance R2.3: the planning handler is equally verbatim.
        if file.path == PLANNING_AGENT_FILENAME and ctx.require_planning:
            return PLANNING_AGENT_SCAFFOLD
        # Agent substance R2.2: the tools handler is PLATFORM-ASSEMBLED — the
        # model writes ONLY the tool function bodies; prelude bytes are fixed.
        if file.path == TOOLS_AGENT_FILENAME and ctx.declared_tools:
            return await self._generate_tools_agent(ctx, plan)
        # 13.5Z: requirements.txt is DERIVED, not authored. The handlers are
        # already generated (the manifest is planned after them); their
        # imports decide the content. Stdlib + boto3 apps — the common case
        # when packaged is chosen for SIZE — get an empty manifest with zero
        # model calls. Only genuine third-party imports need version pins,
        # and the model's answer is filtered to pin-lines before it can
        # become a deliverable (a narrating model produced prose here live).
        if file.path == "requirements.txt" and ctx.require_packaged:
            return await self._generate_requirements(ctx, generated)
        siblings = "\n".join(
            f"- {path} ({len(content)} chars)" for path, content in generated.items()
        )
        rules = (
            "\nArchitecture rules that MUST hold:\n- " + "\n- ".join(ctx.guardrail_rules)
            if ctx.guardrail_rules
            else ""
        )
        implementation_context = (
            "\nCANONICAL SPECIFICATION (implementation source of truth):\n"
            f"REQUIREMENTS:\n{_clip(ctx.spec_docs.get('requirements', ''), 16000)}\n\n"
            f"DESIGN:\n{_clip(ctx.spec_docs.get('design', ''), 16000)}\n\n"
            f"TASKS:\n{_clip(ctx.spec_docs.get('tasks', ''), 8000)}\n"
            if file.language in {"python", "typescript", "html"}
            else ""
        )
        prompt = (
            f"App: {plan.app_name}\nArchitecture: {plan.architecture_notes}\n"
            f"All planned files:\n"
            + "\n".join(f"- {f.path}: {f.brief}" for f in plan.files)
            + (f"\nAlready generated:\n{siblings}" if siblings else "")
            + implementation_context
            + f"{rules}{_auth_generation_block(ctx, cdk=file.language == 'typescript')}"
            + (_connector_generation_block(ctx) if file.language == "python" else "")
            + (_dependency_generation_block(ctx) if file.language == "python" else "")
            + (_memory_generation_block(ctx) if file.language == "python" else "")
            + (_json_response_block() if file.language == "python" else "")
            + literal_contract_block(ctx.literal_contract)
            + f"\nGenerate the complete content of `{file.path}` ({file.brief}) now."
        )
        if file.language == "python" and ctx.require_packaged and ctx.size_escalated:
            system = FILE_SYSTEM_PACKAGED_STDLIB
        elif file.language == "python":
            system = FILE_SYSTEM_PACKAGED if ctx.require_packaged else FILE_SYSTEM
        elif file.language == "typescript":
            system = TS_FILE_SYSTEM
        elif file.language == "html":
            system = _web_console_system(ctx.require_api_key, ctx.require_memory)
        elif file.language == "text":
            system = (
                "You are writing a plain manifest file for a small serverless AWS "
                "demo app. For requirements.txt: one exact pin per line "
                "(name==version), at most 8, ONLY libraries the planned handlers "
                "import beyond stdlib+boto3. Reply with ONLY the raw file content."
            )
        else:
            system = (
                "You are writing documentation for a small serverless AWS demo app. "
                "Reply with ONLY the raw markdown content."
            )
        text, _usage, stop = await converse(
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=system,
            model_id=ctx.model_id,
            max_tokens=min(ctx.max_tokens, 8192),  # per-file budget floor (R1.2)
            ctx=self._ctx(ctx),
        )
        if stop == "max_tokens":
            raise ValueError(f"{file.path} generation hit the token cap")
        content = text.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        if file.path.lower().endswith("readme.md"):
            content = content.rstrip() + _readme_auth_section(ctx)
        return content.rstrip() + "\n"

    async def _generate_requirements(
        self, ctx: BuildCtx, generated: dict[str, str]
    ) -> str:
        modules = third_party_imports(generated)
        if not modules:
            return ""  # valid: nothing beyond the runtime (contract 13.5Z)
        if ctx.size_escalated:
            # 13.5AA: the platform escalated for SIZE; the spec never opted into
            # third-party code. Refuse by name rather than pin what nobody
            # declared — the runner turns this into a validation finding.
            raise SizeEscalationDependencyError(modules)
        text, _usage, stop = await converse(
            messages=[{
                "role": "user",
                "content": [{
                    "text": (
                        "The generated Lambda handlers import these third-party "
                        f"top-level modules: {', '.join(modules)}.\n"
                        "Write requirements.txt for them: one exact pin per line, "
                        "`distribution==version` (the PyPI distribution name, which "
                        "may differ from the import name — e.g. import yaml → "
                        "PyYAML), a current stable version, nothing else — no "
                        "comments, no prose, no fences."
                    )
                }],
            }],
            system=(
                "You emit a Python requirements.txt manifest and NOTHING else: "
                "only lines matching name==version."
            ),
            model_id=ctx.model_id,
            max_tokens=300,
            ctx=self._ctx(ctx),
        )
        if stop == "max_tokens":
            raise ValueError("requirements.txt generation hit the token cap")
        manifest = pin_lines_only(text)
        if not manifest:
            raise ValueError(
                "requirements.txt generation produced no exact pins for "
                f"third-party imports {modules} — rebuild"
            )
        return manifest

    async def _generate_tools_agent(self, ctx: BuildCtx, plan: BuildPlan) -> str:
        """Tool bodies from the model, structure from the platform (R2.2).

        The prelude (imports + exact TOOLS_JSON + verbatim driver) is fixed
        bytes the gate byte-prefix-checks; variance is confined to the leaf
        tool functions, which are LOCAL computation only."""
        names = ", ".join(ctx.declared_tools)
        prompt = (
            f"App: {plan.app_name}\nArchitecture: {plan.architecture_notes}\n\n"
            "CANONICAL SPECIFICATION:\n"
            f"REQUIREMENTS:\n{_clip(ctx.spec_docs.get('requirements', ''), 16000)}\n\n"
            f"DESIGN:\n{_clip(ctx.spec_docs.get('design', ''), 12000)}\n\n"
            f"Write ONLY the Python tool functions for: {names}.\n"
            f"One function per tool, named exactly tool_<name>(args), where args "
            'is a dict {"input": str}. Each returns a JSON-serializable result. '
            "LOCAL computation only — stdlib"
            + (" + the pinned requirements.txt libraries" if ctx.require_packaged else " + boto3")
            + ", no network calls, no placeholders, complete working code. "
            "Do NOT write imports, a handler, or any loop driver — the platform "
            "supplies them. Reply with ONLY the raw function definitions."
        )
        text, _usage, stop = await converse(
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=(
                "You are generating Python tool functions for a Converse tool "
                "loop. Reply with ONLY raw Python function definitions, no "
                "fences, no commentary."
            ),
            model_id=ctx.model_id,
            max_tokens=min(ctx.max_tokens, 4096),
            ctx=self._ctx(ctx),
        )
        if stop == "max_tokens":
            raise ValueError("tool function generation hit the token cap")
        bodies = text.strip()
        if bodies.startswith("```"):
            lines = bodies.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            bodies = "\n".join(lines).strip()
        return assemble_tools_agent(ctx.declared_tools, bodies)

    async def assemble_template(
        self, ctx: BuildCtx, plan: BuildPlan, generated: dict[str, str]
    ) -> str:
        """Assemble structure with source placeholders, then inject exact code.

        Bedrock gets requirements plus deterministic handler signals to wire routes,
        roles, and environment variables. Handler bodies never enter the assembly
        prompt or response; this trusted process performs exact, deterministic source
        embedding after JSON parsing.
        """
        briefs = {item.path: item.brief for item in plan.files}
        source_entries = []
        for index, (path, content) in enumerate(
            item for item in generated.items() if item[0].endswith(".py")
        ):
            env_names = sorted(
                {
                    *re.findall(
                        r'''os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']''',
                        content,
                    ),
                    *re.findall(
                        r'''os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']''',
                        content,
                    ),
                    *re.findall(
                        r'''os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']''',
                        content,
                    ),
                }
            )
            aws_services = sorted(
                set(
                    re.findall(
                        r'''boto3\.(?:client|resource)\(\s*["']([^"']+)["']''',
                        content,
                    )
                )
            )
            source_entries.append(
                (
                    path,
                    f"__MARSHAL_SOURCE_{index}__",
                    content,
                    briefs.get(path, "planned Lambda handler"),
                    env_names,
                    aws_services,
                )
            )
        packaged = ctx.require_packaged
        replacements = {
            placeholder: content
            for _path, placeholder, content, _brief, _env, _services in source_entries
        }
        if packaged:
            # Agent substance R1: business code ships in the staged package —
            # the template references it by parameter, never inline. The
            # sources listing carries handler modules instead of placeholders.
            sources = "\n".join(
                f"- path={path}; handler_module={path.rsplit('/', 1)[-1][:-3]}; "
                f"purpose={brief}; "
                f"required_env={','.join(env_names) or 'none'}; "
                f"aws_sdks={','.join(aws_services) or 'none'}"
                for path, _placeholder, _content, brief, env_names, aws_services in source_entries
            )
        else:
            sources = "\n".join(
                f"- path={path}; ZipFile={placeholder}; purpose={brief}; "
                f"required_env={','.join(env_names) or 'none'}; "
                f"aws_sdks={','.join(aws_services) or 'none'}"
                for path, placeholder, _content, brief, env_names, aws_services in source_entries
            )
        # B19 (web-test-console R2.2): the serving code is spelled VERBATIM so
        # the fiddly part has zero generation variance; only wiring remains.
        console_block = (
            "\nWEB CONSOLE (the spec demands a web test page):\n"
            '- Add an S3 bucket with logical id "WebConsoleBucket": Properties MUST include '
            '"PublicAccessBlockConfiguration": {"BlockPublicAcls": true, "BlockPublicPolicy": true, '
            '"IgnorePublicAcls": true, "RestrictPublicBuckets": true}. No bucket policy.\n'
            '- Add a Lambda with logical id "WebConsoleFunction" (python3.12, Handler index.handler, '
            'Timeout 10, Environment variable WEB_BUCKET = {"Ref": "WebConsoleBucket"}) whose inline '
            "ZipFile code is EXACTLY:\n"
            "import boto3, os\n"
            "def handler(event, context):\n"
            '    o = boto3.client("s3").get_object(Bucket=os.environ["WEB_BUCKET"], Key="index.html")\n'
            '    return {"statusCode": 200, "headers": {"Content-Type": "text/html"}, '
            '"body": o["Body"].read().decode()}\n'
            "- Its execution role gets s3:GetObject on the bucket's objects (least privilege).\n"
            '- Add API resource path "app" with a GET method: AWS_PROXY integration to '
            'WebConsoleFunction, "ApiKeyRequired": false (the console page is public even when '
            "other endpoints require a key), plus the lambda invoke Permission.\n"
            '- Outputs MUST also include "WebConsoleUrl": {"Value": <the ApiUrl value + "/app">} and '
            '"WebBucketName": {"Value": {"Ref": "WebConsoleBucket"}}.\n'
            "- Do NOT embed web/index.html anywhere in the template — the platform stages it "
            "into the bucket at deploy time.\n"
            if ctx.require_web_console
            else ""
        )
        # B13/B21: normalized posture adds either the keyed contract or an
        # explicit-public prohibition. No-signal builds are keyed by default.
        if (ctx.endpoint_auth or {}).get("mode") == "open":
            auth_block = (
                "\nAUTH REQUIREMENT (explicit PUBLIC opt-out):\n"
                "- Do NOT add ApiKey, UsagePlan, UsagePlanKey or ApiKeyId output.\n"
                "- Set ApiKeyRequired false (or omit it) on every API method.\n"
            )
        else:
            auth_block = (
                "\nAUTH REQUIREMENT (API key on every business endpoint):\n"
                '- Add an AWS::ApiGateway::ApiKey (Properties: {"Enabled": true}, DependsOn the Deployment/Stage).\n'
                '- Add an AWS::ApiGateway::UsagePlan whose Properties REQUIRE "ApiStages": [{"ApiId": {"Ref": "<RestApi>"}, "Stage": "prod"}] (DependsOn the Stage), and an AWS::ApiGateway::UsagePlanKey linking the key to the plan.\n'
                '- Set "ApiKeyRequired": true on EVERY AWS::ApiGateway::Method except OPTIONS and the B19 GET /app console loader.\n'
                '- Outputs MUST also include "ApiKeyId": {"Value": {"Ref": "<the ApiKey logical id>"}}.\n'
            )
        # C1: env-var + least-privilege read grant per DECLARED connector only.
        # The template never creates secrets (allowlist forbids it) — the
        # platform provisions marshal/agent-connectors/<slug> at deploy time.
        connector_block = (
            (
                "\nCONNECTORS (this agent calls registered external systems):\n"
                + "".join(
                    f'- Every Lambda whose source calls call_connector("{slug}", ...) MUST have '
                    f'environment variable {connector_env_name(slug)} = "marshal/agent-connectors/{slug}" '
                    "and its execution role MUST allow secretsmanager:GetSecretValue on "
                    '{"Fn::Sub": "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:'
                    f'${{AWS::AccountId}}:secret:marshal/agent-connectors/{slug}*"}}.\n'
                    for slug in ctx.declared_connectors
                )
                + "- Do NOT create any AWS::SecretsManager resources and do NOT grant "
                "access to any other secret.\n"
            )
            if ctx.declared_connectors
            else ""
        )
        # C3 v1: wiring instructions for the platform-verbatim tool-loop
        # handler. Its CODE never enters the prompt (placeholder protocol);
        # only env/IAM/routing wiring is the model's job.
        mcp_block = (
            (
                f"\nMCP TOOL-LOOP AGENT (the handler {MCP_AGENT_FILENAME} is "
                "platform-supplied — wire it, never modify it):\n"
                f'- Its Lambda MUST have env vars: MCP_CONNECTOR_SLUG = "{ctx.mcp_tool_connectors[0]}", '
                f"{connector_env_name(ctx.mcp_tool_connectors[0])} = "
                f'"marshal/agent-connectors/{ctx.mcp_tool_connectors[0]}", and '
                'AGENT_SYSTEM_PROMPT = a 1-3 sentence literal system prompt you '
                "write from the REQUIREMENTS (the agent's behavior contract).\n"
                "- Its execution role MUST allow bedrock:InvokeModel (resource *) "
                "plus the connector secret read from the CONNECTORS section.\n"
                "- Route the PRIMARY business POST endpoint to this Lambda "
                "(AWS_PROXY). Do not generate another handler for the same route.\n"
            )
            if ctx.mcp_tool_connectors
            else ""
        )
        # Composable agents: env + least-privilege read grant per DECLARED
        # dependency; MARSHAL_AGENT_ID literal for the attribution header.
        dependency_block = (
            (
                "\nAGENT DEPENDENCIES (deployed agents this agent calls):\n"
                + "".join(
                    f'- Every Lambda whose source calls call_agent("{slug}", ...) MUST have '
                    f'environment variable {dependency_env_name(slug)} = "marshal/agent-dependencies/{slug}" '
                    "and its execution role MUST allow secretsmanager:GetSecretValue on "
                    '{"Fn::Sub": "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:'
                    f'${{AWS::AccountId}}:secret:marshal/agent-dependencies/{slug}*"}}.\n'
                    for slug in ctx.agent_dependencies
                )
                + f'- Those Lambdas also get MARSHAL_AGENT_ID = "{ctx.project_id}" '
                "(caller attribution).\n"
                "- Do NOT create AWS::SecretsManager resources; the platform "
                "provisions the dependency secrets at deploy time.\n"
            )
            if ctx.agent_dependencies
            else ""
        )
        # Agent substance R2.1: fixed-id memory table + TTL + scoped IAM.
        memory_block = (
            (
                "\nCONVERSATION MEMORY (the spec demands it):\n"
                '- Add an AWS::DynamoDB::Table with logical id "AgentMemoryTable": '
                'BillingMode PAY_PER_REQUEST, KeySchema session_id (HASH, S) + sk '
                '(RANGE, S), "TimeToLiveSpecification": {"AttributeName": '
                '"expires_at", "Enabled": true}.\n'
                '- Every Lambda whose source uses remember/recall gets env var '
                'MEMORY_TABLE = {"Ref": "AgentMemoryTable"} and its execution '
                "role allows dynamodb:PutItem and dynamodb:Query on that table's "
                "Arn ONLY (Fn::GetAtt).\n"
            )
            if ctx.require_memory
            else ""
        )
        # Agent substance R2.2/R2.3: loop-rung wiring — env caps come from the
        # template rail (ctx.loop_iteration_cap); behavior via system prompt.
        tools_block = (
            (
                f"\nTOOL-LOOP AGENT (the handler {TOOLS_AGENT_FILENAME} is "
                "platform-assembled — wire it, never modify it):\n"
                f'- Its Lambda MUST have env vars: TOOLS_MAX_ITERATIONS = "{ctx.loop_iteration_cap}", and '
                "AGENT_SYSTEM_PROMPT = a 1-3 sentence literal system prompt you "
                "write from the REQUIREMENTS (the agent's behavior contract).\n"
                "- Its execution role MUST allow bedrock:InvokeModel (resource *).\n"
                "- Route the PRIMARY business POST endpoint to this Lambda "
                "(AWS_PROXY). Do not generate another handler for the same route.\n"
            )
            if ctx.declared_tools
            else ""
        )
        planning_block = (
            (
                f"\nPLANNING AGENT (the handler {PLANNING_AGENT_FILENAME} is "
                "platform-supplied — wire it, never modify it):\n"
                f'- Its Lambda MUST have env vars: PLANNING_MAX_ITERATIONS = "{ctx.loop_iteration_cap}", and '
                "AGENT_SYSTEM_PROMPT = a 1-3 sentence literal system prompt you "
                "write from the REQUIREMENTS (the agent's behavior contract).\n"
                "- Its execution role MUST allow bedrock:InvokeModel (resource *).\n"
                "- Route the PRIMARY business POST endpoint to this Lambda "
                "(AWS_PROXY). Do not generate another handler for the same route.\n"
            )
            if ctx.require_planning
            else ""
        )
        prompt = (
            f"App: {plan.app_name}\nArchitecture: {plan.architecture_notes}\n\n"
            f"REQUIREMENTS (resource and route wiring context):\n"
            f"{_clip(ctx.spec_docs.get('requirements', ''), 16000)}\n\n"
            f"DESIGN (resource wiring context):\n"
            f"{_clip(ctx.spec_docs.get('design', ''), 12000)}\n\n"
            + (
                "Planned Lambda metadata is listed below. For each handler, set "
                'Code to the staged-package reference {"S3Bucket": {"Ref": '
                '"PackageBucket"}, "S3Key": {"Ref": "PackageKey"}} and Handler to '
                "'<handler_module>.handler' from its metadata. Never inline "
                "planned handler code.\n\n"
                if packaged
                else
                "Planned Lambda metadata is listed below. For each handler, use its "
                "named placeholder EXACTLY ONCE as the complete Code.ZipFile string. "
                "The backend injects the source after parsing; never invent or inline "
                "handler code.\n\n"
            )
            + f"{sources}\n"
            f"{auth_block}{console_block}{connector_block}{dependency_block}{mcp_block}"
            f"{tools_block}{planning_block}{memory_block}"
            f"{literal_contract_block(ctx.literal_contract)}\n"
            "Assemble the CloudFormation JSON template now."
        )
        text, _usage, stop = await converse(
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=TEMPLATE_SYSTEM_PACKAGED if packaged else TEMPLATE_SYSTEM,
            model_id=ctx.model_id,
            max_tokens=min(ctx.max_tokens, 8192),
            ctx=self._ctx(ctx),
        )
        if stop == "max_tokens":
            raise ValueError("template.json assembly hit the token cap")
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw[raw.find("{") :]
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Template assembly returned no JSON object")
        try:
            document = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Template assembly returned invalid JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise ValueError("Template assembly JSON root is not an object")

        resources = document.get("Resources")
        lambda_resources = [
            (logical_id, resource)
            for logical_id, resource in (
                resources.items() if isinstance(resources, dict) else []
            )
            if isinstance(resource, dict)
            and resource.get("Type") == "AWS::Lambda::Function"
        ]
        # Structural/legacy responses with no inline Code continue to the
        # normal validator. Any real inline template must use the placeholder
        # protocol, and every generated source must occur exactly once.
        uses_inline_code = False
        for _logical_id, resource in lambda_resources:
            properties = resource.get("Properties")
            code = properties.get("Code") if isinstance(properties, dict) else None
            zip_file = code.get("ZipFile") if isinstance(code, dict) else None
            if isinstance(zip_file, str):
                uses_inline_code = True
                break
        # Packaged profile: planned sources ride the staged package, never the
        # placeholder protocol — only fixed helpers (console) may be inline,
        # with exact code, so placeholder enforcement does not apply.
        if uses_inline_code and not packaged:
            seen = dict.fromkeys(replacements, 0)
            unknown: list[str] = []
            for logical_id, resource in lambda_resources:
                properties = resource.get("Properties")
                code = properties.get("Code") if isinstance(properties, dict) else None
                zip_file = code.get("ZipFile") if isinstance(code, dict) else None
                if isinstance(zip_file, str) and zip_file in replacements:
                    seen[zip_file] += 1
                    code["ZipFile"] = replacements[zip_file]
                elif isinstance(zip_file, str) and zip_file.startswith("__MARSHAL_SOURCE_"):
                    unknown.append(f"{logical_id}:{zip_file}")
            mismatched = [
                f"{placeholder}:{count}"
                for placeholder, count in seen.items()
                if count != 1
            ]
            if unknown or mismatched:
                detail = ", ".join([*unknown, *mismatched])
                raise ValueError(f"Template assembly source placeholder mismatch: {detail}")

        _enforce_preview_only_topic(ctx, document)
        return json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
