"""Reference runner tests (external-codegen spec R2): the runner module runs
against a dict-backed fake S3 with scripted model output — asserting contract
compliance, budget hard stop, and crash-traps-to-results."""

import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

from app.services.codegen import contract

pytestmark = pytest.mark.asyncio

RUNNER_PATH = Path(__file__).parent.parent.parent / "runner" / "main.py"


@pytest.fixture()
def runner_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location("runner_main", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["runner_main"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("runner_main", None)


class FakeS3:
    def __init__(self, store: dict[str, bytes]):
        self.store = store

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3 casing
        if Key not in self.store:
            raise KeyError(Key)
        import io

        return {"Body": io.BytesIO(self.store[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.store[Key] = Body

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.store:
            raise KeyError(Key)
        return {}


PLAN_JSON = json.dumps(
    {
        "app_name": "probe-app",
        "architecture_notes": "one lambda",
        "files": [
            {"path": "src/handler.py", "brief": "handler", "language": "python"},
            {"path": "README.md", "brief": "docs", "language": "markdown"},
        ],
    }
)
HANDLER = "def handler(event, context):\n    return {'statusCode': 200}\n"
TEMPLATE = json.dumps({"Resources": {"Fn": {"Type": "AWS::Lambda::Function"}}, "Outputs": {"ApiUrl": {"Value": "x"}}})


def _seed_request(store: dict[str, bytes], root: str = "builds/test-build") -> None:
    request = contract.build_request(
        build_id=str(uuid.uuid4()), project_slug="probe", project_name="Probe",
        artifact_profile="inline-cfn", model_id="us.anthropic.claude-sonnet-5",
        token_budget=60_000, allowlist=[], guardrail_rules=["avoid: hardcoded_credentials"],
        response_prefix=f"{root}/response/", dispatched_at="2026-07-26T00:00:00+00:00",
    )
    store[f"{root}/request/{contract.REQUEST_KEY}"] = json.dumps(request).encode()
    store[f"{root}/request/.kiro/specs/probe/requirements.md"] = b"# Requirements\nreal"
    store[f"{root}/request/.kiro/specs/probe/design.md"] = b"# Design\nreal"


def _scripted_converse(outputs: list[str], *, tokens_per_call: int = 100):
    calls = {"n": 0}

    async def converse(self, *, messages, system, model_id, max_tokens=None, **kwargs):
        if self.used_tokens >= self.token_budget:
            raise sys.modules["runner_main"].BudgetExhausted(
                f"token budget exhausted ({self.used_tokens}/{self.token_budget})"
            )
        text = outputs[min(calls["n"], len(outputs) - 1)]
        calls["n"] += 1
        self.used_tokens += tokens_per_call
        self.usage.append(
            {"model_id": model_id, "input_tokens": tokens_per_call // 2,
             "output_tokens": tokens_per_call // 2}
        )
        return text, {"inputTokens": tokens_per_call // 2, "outputTokens": tokens_per_call // 2}, "end_turn"

    return converse


async def test_runner_happy_path_is_contract_compliant(runner_mod, monkeypatch):
    store: dict[str, bytes] = {}
    _seed_request(store)
    monkeypatch.setattr(runner_mod, "s3", FakeS3(store))
    monkeypatch.setattr(
        runner_mod.HeadlessBedrock, "converse",
        _scripted_converse([PLAN_JSON, HANDLER, "# README\n", TEMPLATE]),
    )
    code = await runner_mod.run("bucket", "builds/test-build")
    assert code == 0

    raw = store["builds/test-build/response/results.json"]
    results = contract.parse_results(json.loads(raw))  # the platform-side parser accepts it
    assert results.status == "succeeded"
    assert results.app_name == "probe-app"
    paths = {f.path for f in results.files}
    assert paths == {"src/handler.py", "README.md", "template.json"}
    # hashes in the manifest match the written objects (ingest verification passes)
    for entry in results.files:
        body = store[f"builds/test-build/response/generated/{entry.path}"]
        assert hashlib.sha256(body).hexdigest() == entry.sha256
    assert sum(u.input_tokens + u.output_tokens for u in results.usage) == 400
    assert store.get("builds/test-build/response/started.json") is not None
    readme = store["builds/test-build/response/generated/README.md"].decode()
    assert "x-api-key" in readme and "Reveal key" in readme


async def test_runner_budget_hard_stop_writes_partials(runner_mod, monkeypatch):
    store: dict[str, bytes] = {}
    _seed_request(store)
    monkeypatch.setattr(runner_mod, "s3", FakeS3(store))
    # Budget of 150 tokens: plan (100) + first file (100) exhausts it mid-build
    request = json.loads(store["builds/test-build/request/build-request.json"])
    request["token_budget"] = 150
    store["builds/test-build/request/build-request.json"] = json.dumps(request).encode()
    monkeypatch.setattr(
        runner_mod.HeadlessBedrock, "converse",
        _scripted_converse([PLAN_JSON, HANDLER, "# README\n", TEMPLATE]),
    )
    code = await runner_mod.run("bucket", "builds/test-build")
    assert code == 0
    results = json.loads(store["builds/test-build/response/results.json"])
    assert results["status"] == "failed"
    assert results["error"]["code"] == "token_budget_exhausted"
    # partial artifacts flushed for diagnosis (R2.3)
    assert any(f["path"] == "src/handler.py" for f in results["files"])


async def test_runner_crash_traps_into_results(runner_mod, monkeypatch):
    store: dict[str, bytes] = {}
    _seed_request(store)
    monkeypatch.setattr(runner_mod, "s3", FakeS3(store))

    async def explode(self, **kwargs):
        raise RuntimeError("model melted")

    monkeypatch.setattr(runner_mod.HeadlessBedrock, "converse", explode)
    code = await runner_mod.run("bucket", "builds/test-build")
    assert code == 0  # the runner never exits nonzero after writing results
    results = json.loads(store["builds/test-build/response/results.json"])
    assert results["status"] == "failed"
    assert results["error"]["code"] == "engine_crash"
    assert "model melted" in results["error"]["message"]


async def test_runner_respects_cancel_tombstone(runner_mod, monkeypatch):
    store: dict[str, bytes] = {}
    _seed_request(store)
    store[f"builds/test-build/request/{contract.CANCELLED_KEY}"] = b"{}"
    monkeypatch.setattr(runner_mod, "s3", FakeS3(store))
    monkeypatch.setattr(
        runner_mod.HeadlessBedrock, "converse",
        _scripted_converse([PLAN_JSON, HANDLER, "# README\n", TEMPLATE]),
    )
    code = await runner_mod.run("bucket", "builds/test-build")
    assert code == 0
    # stopped without writing results (the platform's tombstone rules apply)
    assert "builds/test-build/response/results.json" not in store


def test_workspace_uri_parsing(runner_mod):
    assert runner_mod.parse_workspace("s3://bucket/builds/x") == ("bucket", "builds/x")
    with pytest.raises(ValueError):
        runner_mod.parse_workspace("http://bucket/builds/x")
    with pytest.raises(ValueError):
        runner_mod.parse_workspace("s3://bucket")


# --------------------------------------------- reasoning suppression (13.5Y)
# HeadlessBedrock mirrors app.services.bedrock: adaptive-thinking Claude
# models must have thinking pinned OFF (reasoning tokens bill against the
# HARD token budget while the text-join drops them), with rejection-tolerant
# fallback for families that refuse the field.


class _KwargCapturingClient:
    def __init__(self, script):
        self.calls: list[dict] = []
        self._script = list(script)

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return {
            "output": {"message": {"content": [{"text": item}]}},
            "usage": {"inputTokens": 5, "outputTokens": 5},
            "stopReason": "end_turn",
        }


async def test_headless_bedrock_pins_thinking_off_for_claude(runner_mod):
    hb = runner_mod.HeadlessBedrock(token_budget=1000)
    hb.client = _KwargCapturingClient(["ok"])
    text, _usage, stop = await hb.converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s", model_id="us.anthropic.claude-sonnet-5", max_tokens=100,
    )
    assert text == "ok" and stop == "end_turn"
    assert hb.client.calls[0]["additionalModelRequestFields"] == {
        "thinking": {"type": "disabled"}
    }


async def test_headless_bedrock_no_field_for_other_families(runner_mod):
    hb = runner_mod.HeadlessBedrock(token_budget=1000)
    hb.client = _KwargCapturingClient(["ok"])
    await hb.converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s", model_id="amazon.nova-pro-v1:0", max_tokens=100,
    )
    assert "additionalModelRequestFields" not in hb.client.calls[0]


async def test_headless_bedrock_thinking_rejection_falls_back(runner_mod):
    from botocore.exceptions import ClientError

    rejection = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "this model does not support thinking",
            }
        },
        "Converse",
    )
    hb = runner_mod.HeadlessBedrock(token_budget=1000)
    hb.client = _KwargCapturingClient([rejection, "fallback"])
    text, _usage, _stop = await hb.converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s", model_id="us.anthropic.claude-sonnet-5", max_tokens=100,
    )
    assert text == "fallback"
    assert "additionalModelRequestFields" in hb.client.calls[0]
    assert "additionalModelRequestFields" not in hb.client.calls[1]


async def test_headless_bedrock_retries_transient_invalid_model(runner_mod, monkeypatch):
    """Mirror of the seam's transient-invalid-model absorption (13.5Y)."""
    import threading

    from botocore.exceptions import ClientError

    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: None)
    flake = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "The provided model identifier is invalid.",
            }
        },
        "Converse",
    )
    hb = runner_mod.HeadlessBedrock(token_budget=1000)
    hb.client = _KwargCapturingClient([flake, "recovered"])
    text, _usage, _stop = await hb.converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s", model_id="us.anthropic.claude-sonnet-5", max_tokens=100,
    )
    assert text == "recovered"
    assert len(hb.client.calls) == 2


async def test_headless_bedrock_retries_internal_server_exception(runner_mod, monkeypatch):
    """Mirror of the seam's InternalServerException retry (13.5Z)."""
    import threading

    from botocore.exceptions import ClientError

    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: None)
    blip = ClientError(
        {"Error": {"Code": "InternalServerException", "Message": "unexpected error"}},
        "Converse",
    )
    hb = runner_mod.HeadlessBedrock(token_budget=1000)
    hb.client = _KwargCapturingClient([blip, "after-blip"])
    text, _usage, _stop = await hb.converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s", model_id="us.anthropic.claude-sonnet-5", max_tokens=100,
    )
    assert text == "after-blip"
    assert len(hb.client.calls) == 2
