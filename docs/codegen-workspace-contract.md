# marshal Codegen Workspace Contract — v1

The wire format between the marshal platform and any code-generation engine
(external-codegen spec R1; FSD §8, OQ-1 "workspace sync"). The reference
implementation is `runner/main.py`; the platform side is
`backend/app/services/codegen/workspace_runner.py`. Schemas live in
`backend/app/services/codegen/contract.py` — the examples below are validated
against that module by `tests/test_external_codegen.py`, so this document
cannot silently drift.

Administrators select this engine as codegen provider `runner`; `kiro` is
accepted as a legacy alias for `runner`. The reference runner is one engine
behind this contract — any engine that reads the request prefix and writes
the response prefix as described below can replace it without a platform
change.

## Layout

```
s3://<workspace-bucket>/builds/<build_id>/          # attempt 1
s3://<workspace-bucket>/builds/<build_id>-r2/       # attempt 2 (single auto-retry)
  request/                          # written by the PLATFORM
    build-request.json
    .kiro/specs/<slug>/requirements.md
    .kiro/specs/<slug>/design.md
    .kiro/specs/<slug>/tasks.md     # optional
    cancelled.json                  # tombstone — appears if the build is cancelled
  response/                         # written by the ENGINE (only place it may write)
    started.json                    # optional early marker
    generated/<path>                # the artifact tree
    results.json                    # completion manifest — the LAST thing written
```

## build-request.json (platform → engine)

```json
{
  "contract_version": 1,
  "build_id": "9c1f6a2e-6f0e-4b7a-9a71-3d2f8c1b5e44",
  "project_slug": "invoice-assistant",
  "project_name": "Invoice Assistant",
  "artifact_profile": "inline-cfn",
  "model_id": "us.anthropic.claude-sonnet-5",
  "token_budget": 60000,
  "allowlist": [],
  "guardrail_rules": ["avoid: hardcoded_credentials"],
  "endpoint_auth": {"mode": "key_required", "source": "default"},
  "literal_contract": {
    "version": 1,
    "entries": [
      {
        "kind": "field_contract",
        "items": ["invoice_ref", "amount"],
        "criterion": "The request SHALL accept exactly {invoice_ref, amount}"
      }
    ]
  },
  "require_web_console": false,
  "response_prefix": "builds/9c1f6a2e-6f0e-4b7a-9a71-3d2f8c1b5e44/response/",
  "dispatched_at": "2026-07-26T12:00:00+00:00"
}
```

- `token_budget` is a HARD stop: engines must cease generation when cumulative
  (input + output) tokens reach it and write failed results with code
  `token_budget_exhausted`.
- `guardrail_rules` are advisory context for generation; enforcement happens
  platform-side at the validation gate regardless.
- `endpoint_auth` is platform-normalized from the frozen requirements. Engines
  MUST generate the declared keyed/open posture; missing fields default safely
  to `key_required` when read by the reference runner.
- `literal_contract` is the exact field/enum spelling contract extracted once
  by the platform. Engines MUST preserve every item verbatim; platform-side
  deterministic conformance still enforces it after ingest.
- `require_web_console` preserves the B19 artifact signal; console v0 remains
  inline-cfn-only and platform validation refuses unsupported CDK output.
- `artifact_profile` is `inline-cfn` or `cdk-app`.

## results.json (engine → platform)

```json
{
  "contract_version": 1,
  "status": "succeeded",
  "app_name": "invoice-assistant",
  "architecture_notes": "One Lambda behind API Gateway storing rows in DynamoDB.",
  "files": [
    {"path": "src/handler.py", "sha256": "<64-hex>", "bytes": 1420},
    {"path": "README.md", "sha256": "<64-hex>", "bytes": 512},
    {"path": "template.json", "sha256": "<64-hex>", "bytes": 6100}
  ],
  "usage": [
    {"model_id": "us.anthropic.claude-sonnet-5", "input_tokens": 4100, "output_tokens": 1900}
  ],
  "engine": {"name": "marshal-reference-runner", "version": "1.0"}
}
```

Failure shape: `"status": "failed"` + `"error": {"code": "...", "message": "..."}`;
partial `files` may still be listed (they aid diagnosis and are NOT deployed).

### v1.1 additions (S10, `cdk-app` profile — additive, same contract_version)

- `files[]` entries may carry `"binary": true` (asset zips): hash-verified but
  never parsed as text.
- Optional top-level `synth` block describes CDK assets and their CloudFormation
  parameter bindings (from the cloud assembly, legacy synthesizer):

```json
{
  "contract_version": 1,
  "status": "succeeded",
  "files": [
    {"path": "lib/app-stack.ts", "sha256": "<64-hex>", "bytes": 2100},
    {"path": "synth/template.json", "sha256": "<64-hex>", "bytes": 9000},
    {"path": "synth/assets/deadbeef.zip", "sha256": "<64-hex>", "bytes": 800, "binary": true}
  ],
  "usage": [{"model_id": "us.anthropic.claude-sonnet-5", "input_tokens": 5000, "output_tokens": 2500}],
  "engine": {"name": "marshal-reference-runner", "version": "1.0"},
  "synth": {
    "assets": [
      {
        "id": "deadbeef", "path": "synth/assets/deadbeef.zip", "source_hash": "deadbeef",
        "bucket_parameter": "AssetParametersdeadbeefS3Bucket…",
        "key_parameter": "AssetParametersdeadbeefS3VersionKey…",
        "hash_parameter": "AssetParametersdeadbeefArtifactHash…"
      }
    ]
  }
}
```

The platform stages each asset into an Enclave-local bucket at deploy time and
binds the three parameters per asset (`key` uses the legacy `key||` split
convention). Engines producing `cdk-app` builds MUST synthesize with the
legacy synthesizer so assets surface as parameters.

## Rules

1. Engines write ONLY under `response/`. Paths in `files[]` are relative to
   `response/generated/`, must not escape it (`..`, absolute, backslashes), and
   every listed file must exist with a matching sha256 — mismatches fail the
   build as `contract_violation`.
2. `results.json` is written LAST and exactly once per attempt; engines may
   retry uploads (last complete manifest wins).
3. If `request/cancelled.json` appears, stop; anything written afterwards is
   ignored.
4. Timeout: the platform allows 15 minutes from dispatch, with ONE automatic
   re-dispatch (attempt suffix `-r2`) on timeout or contract violation.
5. Usage honesty: `usage[]` is priced into platform cost accounting and charged
   against the requesting user's cap. Engines must report actual token usage.
6. Trust boundary: artifacts pass marshal's validation gate (service allowlist,
   packaging rules, guardrail detectors, deploy contract) — the contract grants
   no bypass.
7. Workspace objects expire after 30 days; the platform ingests artifacts into
   its own stores at completion. The workspace is a wire format, not storage.
