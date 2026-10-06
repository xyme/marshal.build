"""Verbatim platform scaffolds (C3 v1, FSD §13.5Q).

The MCP tool-loop handler is NOT model-generated: the planner reserves the
file, generate_file() short-circuits to this exact text, and the
`mcp_tool_loop` gate verifies byte-equality in the assembled template — the
B19 zero-variance discipline applied to a whole handler. Tools come from ONE
declared mcp_server connector; credentials/base-url arrive through the C1
copy custody (the same composite secret call_connector reads), so no new
custody surface exists.

Inline constraints honored: stdlib + boto3 only, single ZipFile, bounded
iterations, bounded payload sizes. Size is test-enforced under the 4000-char
CloudFormation gate.
"""

MCP_AGENT_FILENAME = "src/mcp_agent.py"
MCP_MAX_TOOL_ITERATIONS = 5

MCP_AGENT_SCAFFOLD = '''import boto3, json, os, urllib.request

_CONN = {}
MAX_TOOL_ITERATIONS = 5
J = {"Content-Type": "application/json"}

def _conn(slug):
    if slug not in _CONN:
        sid = os.environ["CONNECTOR_" + slug.upper().replace("-", "_") + "_SECRET"]
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=sid)["SecretString"]
        _CONN[slug] = json.loads(raw)
    return _CONN[slug]

def _rpc(slug, method, params, rpc_id):
    c = _conn(slug)
    h = dict(J, Accept="application/json, text/event-stream")
    if c.get("value"):
        h[c.get("header") or "Authorization"] = c["value"]
    if c.get("_sid"):
        h["mcp-session-id"] = c["_sid"]
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if rpc_id is not None:
        body["id"] = rpc_id
    req = urllib.request.Request(c["base_url"], data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.headers.get("mcp-session-id"):
            c["_sid"] = r.headers.get("mcp-session-id")
        t = r.read().decode()
    if "data:" in t and not t.lstrip().startswith("{"):
        t = [x[5:].strip() for x in t.splitlines() if x.startswith("data:")][0]
    return (json.loads(t).get("result") or {}) if t.strip() else {}

def _tools(slug):
    _rpc(slug, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "marshal-agent", "version": "1"}}, 1)
    _rpc(slug, "notifications/initialized", None, None)
    ts = (_rpc(slug, "tools/list", {}, 2).get("tools") or [])[:16]
    return [{"toolSpec": {"name": t["name"], "description": (t.get("description") or t["name"])[:400],
        "inputSchema": {"json": t.get("inputSchema") or {"type": "object"}}}} for t in ts]

def run_agent(text):
    slug = os.environ["MCP_CONNECTOR_SLUG"]
    br = boto3.client("bedrock-runtime")
    tools = _tools(slug)
    msgs = [{"role": "user", "content": [{"text": text[:8000]}]}]
    sys_p = [{"text": os.environ.get("AGENT_SYSTEM_PROMPT", "You are a helpful agent.")}]
    model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    for _ in range(MAX_TOOL_ITERATIONS):
        r = br.converse(modelId=model, messages=msgs, system=sys_p,
            toolConfig={"tools": tools}, inferenceConfig={"maxTokens": 1024})
        m = r["output"]["message"]
        msgs.append(m)
        if r.get("stopReason") != "tool_use":
            return "".join(b.get("text", "") for b in m.get("content", []))
        out = []
        for b in m.get("content", []):
            u = b.get("toolUse")
            if not u:
                continue
            try:
                res = _rpc(slug, "tools/call", {"name": u["name"], "arguments": u.get("input") or {}}, 3)
                out.append({"toolResult": {"toolUseId": u["toolUseId"],
                    "content": [{"text": json.dumps(res.get("content") or res)[:4000]}]}})
            except Exception as e:
                out.append({"toolResult": {"toolUseId": u["toolUseId"],
                    "content": [{"text": "tool error: " + str(e)[:200]}], "status": "error"}})
        msgs.append({"role": "user", "content": out})
    return "Stopped: tool-iteration limit reached."

def handler(event, context):
    try:
        q = str((json.loads(event.get("body") or "{}").get("input") or "")).strip()
        if not q:
            return {"statusCode": 422, "headers": J, "body": json.dumps({"error": "provide 'input' text"})}
        return {"statusCode": 200, "headers": J, "body": json.dumps({"output": run_agent(q)})}
    except Exception as e:
        return {"statusCode": 500, "headers": J, "body": json.dumps({"error": str(e)[:300]})}
'''


# --- Agent substance R2.2: structured tool use (tools rung) ------------------
# The tool-loop file is PLATFORM-ASSEMBLED: imports + the deterministic
# TOOLS_JSON line + the verbatim driver are fixed bytes (byte-prefix gate);
# the model writes ONLY the tool function bodies appended below them. v1
# tools take one {"input": string} argument — the model chooses WHICH tool
# and with WHAT input; local functions compute (external reach stays
# connector/MCP territory).

TOOLS_AGENT_FILENAME = "src/tools_agent.py"
TOOLS_MAX_ITERATIONS_CEILING = 8


def tools_json_for(declared: list[str]) -> str:
    """The exact TOOLS_JSON payload for a declared tool list — deterministic
    (sorted, minified) so the gate can demand byte equality."""
    import json as _json

    return _json.dumps(
        [
            {
                "name": name,
                "description": "Local tool: " + name.replace("_", " "),
                "inputSchema": {
                    "type": "object",
                    "properties": {"input": {"type": "string"}},
                    "required": ["input"],
                },
            }
            for name in declared
        ],
        separators=(",", ":"),
    )


TOOLS_DRIVER_CODE = '''MAX_TOOL_ITERATIONS = max(1, min(int(os.environ.get("TOOLS_MAX_ITERATIONS", "5")), 8))
J = {"Content-Type": "application/json"}

def _toolconfig():
    return {"tools": [{"toolSpec": {"name": t["name"], "description": t["description"],
        "inputSchema": {"json": t["inputSchema"]}}} for t in json.loads(TOOLS_JSON)]}

def run_agent(text):
    br = boto3.client("bedrock-runtime")
    msgs = [{"role": "user", "content": [{"text": text[:8000]}]}]
    sys_p = [{"text": os.environ.get("AGENT_SYSTEM_PROMPT", "You are a helpful agent.")}]
    model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    for _ in range(MAX_TOOL_ITERATIONS):
        r = br.converse(modelId=model, messages=msgs, system=sys_p,
            toolConfig=_toolconfig(), inferenceConfig={"maxTokens": 1024})
        m = r["output"]["message"]
        msgs.append(m)
        if r.get("stopReason") != "tool_use":
            return "".join(b.get("text", "") for b in m.get("content", []))
        out = []
        for b in m.get("content", []):
            u = b.get("toolUse")
            if not u:
                continue
            try:
                fn = globals().get("tool_" + u["name"])
                res = fn(u.get("input") or {}) if fn else "unknown tool: " + u["name"]
                out.append({"toolResult": {"toolUseId": u["toolUseId"],
                    "content": [{"text": json.dumps(res, default=str)[:4000]}]}})
            except Exception as e:
                out.append({"toolResult": {"toolUseId": u["toolUseId"],
                    "content": [{"text": "tool error: " + str(e)[:200]}], "status": "error"}})
        msgs.append({"role": "user", "content": out})
    return "Stopped: tool-iteration limit reached."

def handler(event, context):
    try:
        q = str((json.loads(event.get("body") or "{}").get("input") or "")).strip()
        if not q:
            return {"statusCode": 422, "headers": J, "body": json.dumps({"error": "provide 'input' text"})}
        return {"statusCode": 200, "headers": J, "body": json.dumps({"output": run_agent(q)})}
    except Exception as e:
        return {"statusCode": 500, "headers": J, "body": json.dumps({"error": str(e)[:300]})}
'''


def tools_agent_prelude(declared: list[str]) -> str:
    """The fixed byte-prefix of src/tools_agent.py: imports, the exact
    TOOLS_JSON line, the verbatim driver. Tool bodies follow it."""
    return (
        "import boto3, json, os\n\n"
        f"TOOLS_JSON = '{tools_json_for(declared)}'\n\n" + TOOLS_DRIVER_CODE
    )


def assemble_tools_agent(declared: list[str], bodies: str) -> str:
    """Platform-assembled tool agent: fixed prelude + model-written bodies."""
    return tools_agent_prelude(declared) + "\n\n" + bodies.strip() + "\n"


# --- Agent substance R2.3: bounded planning loop (planning rung) -------------
# Fully platform-verbatim (the MCP discipline): plan → bounded step execution
# → composition. Behavior arrives via AGENT_SYSTEM_PROMPT; the cap via
# PLANNING_MAX_ITERATIONS (template-rail-derived, runtime-clamped ≤ 8).

PLANNING_AGENT_FILENAME = "src/planning_agent.py"
PLANNING_MAX_ITERATIONS_CEILING = 8

PLANNING_AGENT_SCAFFOLD = '''import boto3, json, os

MAX_PLAN_STEPS = max(1, min(int(os.environ.get("PLANNING_MAX_ITERATIONS", "5")), 8))
J = {"Content-Type": "application/json"}

def _ask(br, model, sys_p, text, max_tokens=1024):
    r = br.converse(modelId=model, messages=[{"role": "user", "content": [{"text": text[:12000]}]}],
        system=sys_p, inferenceConfig={"maxTokens": max_tokens})
    return "".join(b.get("text", "") for b in r["output"]["message"]["content"])

def run_agent(text):
    br = boto3.client("bedrock-runtime")
    sys_p = [{"text": os.environ.get("AGENT_SYSTEM_PROMPT", "You are a helpful agent.")}]
    model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    raw = _ask(br, model, sys_p, "Break this request into at most " + str(MAX_PLAN_STEPS)
        + " short executable steps. Reply with ONLY a JSON array of step strings.\\n\\nREQUEST: " + text)
    try:
        steps = [str(s)[:400] for s in json.loads(raw[raw.find("["):raw.rfind("]") + 1])][:MAX_PLAN_STEPS]
    except Exception:
        steps = []
    if not steps:
        return _ask(br, model, sys_p, text)
    notes = []
    for i, step in enumerate(steps, 1):
        notes.append("Step " + str(i) + ": " + step + "\\n"
            + _ask(br, model, sys_p, "Original request: " + text[:4000] + "\\nPlan: "
            + json.dumps(steps) + "\\nCompleted step notes: " + "\\n".join(notes)[-6000:]
            + "\\nExecute ONLY step " + str(i) + " and reply with its result."))
    return _ask(br, model, sys_p, "Original request: " + text[:4000]
        + "\\nStep results:\\n" + "\\n\\n".join(notes)[-10000:]
        + "\\nCompose the final answer to the original request.")

def handler(event, context):
    try:
        q = str((json.loads(event.get("body") or "{}").get("input") or "")).strip()
        if not q:
            return {"statusCode": 422, "headers": J, "body": json.dumps({"error": "provide 'input' text"})}
        return {"statusCode": 200, "headers": J, "body": json.dumps({"output": run_agent(q)})}
    except Exception as e:
        return {"statusCode": 500, "headers": J, "body": json.dumps({"error": str(e)[:300]})}
'''
