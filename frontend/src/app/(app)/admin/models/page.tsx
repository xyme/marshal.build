"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import CustomEndpointsCard from "@/components/admin/CustomEndpointsCard";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import { modelLabel } from "@/lib/marketplace-ui";
import type { ModelControls, ModelRegistryEntry } from "@/lib/types";

interface CodegenHealth {
  active_provider: string;
  runner: { reachable: boolean; detail: string | null; project?: string };
}

/** Admin > Model Controls (FSD §4.6.6, S4-04; S9 codegen provider). */
export default function AdminModelsPage() {
  const readonly = useAdminReadonly();
  const [registry, setRegistry] = useState<ModelRegistryEntry[]>([]);
  const [controls, setControls] = useState<ModelControls | null>(null);
  const [codegenHealth, setCodegenHealth] = useState<CodegenHealth | null>(null);
  const [catalogQuery, setCatalogQuery] = useState("");
  const [showUnavailable, setShowUnavailable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const loadRegistry = useCallback(
    () =>
      api<ModelRegistryEntry[]>("/v1/admin/templates/model-registry")
        .then(setRegistry)
        .catch(() => {}),
    []
  );

  useEffect(() => {
    loadRegistry();
    api<ModelControls>("/v1/admin/model-controls")
      .then(setControls)
      .catch((e) => setError(e.message));
    api<CodegenHealth>("/v1/admin/codegen-health").then(setCodegenHealth).catch(() => {});
  }, [loadRegistry]);

  function toggleModel(id: string) {
    if (!controls) return;
    const entry = registry.find((model) => model.id === id);
    const isAlreadyAllowed = controls.model_allowlist.includes(id);
    if (entry?.selectable === false && !isAlreadyAllowed) return;
    const list = controls.model_allowlist.includes(id)
      ? controls.model_allowlist.filter((m) => m !== id)
      : [...controls.model_allowlist, id];
    setControls({ ...controls, model_allowlist: list });
  }

  function setBound(path: "temperature" | "top_p", key: "min" | "max", value: number) {
    if (!controls) return;
    setControls({
      ...controls,
      param_bounds: {
        ...controls.param_bounds,
        [path]: { ...(controls.param_bounds[path] ?? { min: 0, max: 1 }), [key]: value },
      },
    });
  }

  async function save() {
    if (!controls) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await api<ModelControls>("/v1/admin/model-controls", {
        method: "PUT",
        body: JSON.stringify({
          model_allowlist: controls.model_allowlist,
          param_bounds: controls.param_bounds,
          rate_limits: controls.rate_limits,
          cost: controls.cost,
          codegen: controls.codegen ?? {},
          deployment_policies: controls.deployment_policies ?? {},
          security: controls.security ?? {},
        }),
      });
      setControls(saved);
      setConfirming(false);
      toast.success("Model controls saved — takes effect within 60 seconds.");
    } catch (e) {
      setError((e as Error).message);
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  if (error && !controls) {
    return <p className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-red-300">{error}</p>;
  }
  if (!controls) return <p className="text-slate-400">Loading…</p>;

  const temp = controls.param_bounds.temperature ?? { min: 0, max: 1 };
  const topP = controls.param_bounds.top_p ?? { min: 0, max: 1 };
  const normalizedQuery = catalogQuery.trim().toLowerCase();
  const selectableCount = registry.filter((model) => model.selectable !== false).length;
  const unavailableCount = registry.length - selectableCount;
  const visibleRegistry = registry.filter((model) => {
    if (!showUnavailable && model.selectable === false) return false;
    if (!normalizedQuery) return true;
    return [model.label, model.id, model.base_model_id, model.provider_name]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(normalizedQuery));
  });
  const catalogIsStale = registry.some((model) => model.catalog_stale);

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Model Controls</h1>
        <p className="mt-1 text-sm text-slate-400">
          Global bounds for every session — templates can only narrow further, never widen.
        </p>
      </div>

      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Model allowlist
        </h2>
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          <span className="rounded bg-emerald-500/15 px-2 py-1 text-emerald-300">
            {selectableCount} selectable
          </span>
          <span className="rounded bg-slate-800 px-2 py-1 text-slate-300">
            {unavailableCount} visible but unavailable
          </span>
          <span className="rounded bg-indigo-500/15 px-2 py-1 text-indigo-300">
            {controls.model_allowlist.length} allowed
          </span>
        </div>
        {catalogIsStale && (
          <p className="mt-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
            Live Bedrock discovery is unavailable. This is the last-known-good catalog; access was not broadened.
          </p>
        )}
        <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
          <input
            type="search"
            value={catalogQuery}
            onChange={(event) => setCatalogQuery(event.target.value)}
            placeholder="Search model, provider, or ID"
            className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          />
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              checked={showUnavailable}
              onChange={(event) => setShowUnavailable(event.target.checked)}
            />
            Show unavailable
          </label>
        </div>
        <div className="mt-3 max-h-[36rem] space-y-2 overflow-y-auto pr-1">
          {visibleRegistry.map((model) => {
            const checked = controls.model_allowlist.includes(model.id);
            const disabled = model.selectable === false;
            return (
              <label
                key={`${model.id}:${model.base_model_id ?? ""}`}
                className={`block rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm ${
                  disabled ? "text-slate-500" : "text-slate-100"
                }`}
                title={model.disabled_reason ?? undefined}
              >
                <span className="flex flex-wrap items-center gap-2">
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={disabled && !checked}
                    onChange={() => toggleModel(model.id)}
                  />
                  <span className="font-medium">{model.label ?? modelLabel(model.id)}</span>
                  <span className="text-xs text-slate-500">{model.provider_name}</span>
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-400">
                    {model.tier}
                  </span>
                  {model.inference_type === "inference_profile" && (
                    <span className="rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-violet-300">
                      {model.routing_scope} profile
                    </span>
                  )}
                  {model.pricing?.status === "estimated_ceiling" && (
                    <span
                      className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-300"
                      title="Budget accounting uses a deliberately high internal ceiling, not a provider quote"
                    >
                      Conservative estimate
                    </span>
                  )}
                  {model.source === "external" && (
                    <span
                      className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-sky-300"
                      title="Hosted outside Bedrock — prompts sent here leave AWS"
                    >
                      External
                    </span>
                  )}
                </span>
                <span className="mt-1 block break-all pl-6 text-[11px] text-slate-500">
                  {model.id}
                </span>
                {model.disabled_reason && (
                  <span className="mt-1 block pl-6 text-xs text-amber-300/80">
                    {model.disabled_reason}
                  </span>
                )}
              </label>
            );
          })}
          {visibleRegistry.length === 0 && (
            <p className="py-4 text-center text-sm text-slate-500">No models match this filter.</p>
          )}
        </div>
        {controls.model_allowlist.length === 0 && (
          <p className="mt-2 text-xs text-red-400">At least one model must stay allowed.</p>
        )}
        <p className="mt-2 text-xs text-slate-400">
          Discovery never enables a model automatically. Governed purposes (risk scoring and code generation)
          remain pinned to Bedrock, and unpriced models use a deliberately conservative budget ceiling.
        </p>
      </section>

      <CustomEndpointsCard onChanged={loadRegistry} />

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Parameter bounds
        </h2>
        <div className="mt-3 grid grid-cols-2 gap-4 text-sm">
          <BoundPair label="Temperature" value={temp} onChange={(k, v) => setBound("temperature", k, v)} />
          <BoundPair label="Top-p" value={topP} onChange={(k, v) => setBound("top_p", k, v)} />
          <label className="block text-slate-400">
            Max tokens
            <input
              type="number" min={256} max={200000}
              value={controls.param_bounds.max_tokens ?? 8192}
              onChange={(e) =>
                setControls({
                  ...controls,
                  param_bounds: { ...controls.param_bounds, max_tokens: Number(e.target.value) },
                })
              }
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
            />
          </label>
          <label className="block text-slate-400">
            Max context (tokens)
            <input
              type="number" min={1000}
              value={controls.param_bounds.max_context ?? 200000}
              onChange={(e) =>
                setControls({
                  ...controls,
                  param_bounds: { ...controls.param_bounds, max_context: Number(e.target.value) },
                })
              }
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
            />
          </label>
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Rate limits <span className="normal-case text-slate-400">— enforced at the model seam (queue ≤30s, then 429)</span>
        </h2>
        <div className="mt-3 grid grid-cols-3 gap-4 text-sm">
          {(
            [
              ["per_user_rpm", "Per user (req/min)"],
              ["per_project_rpm", "Per project (req/min)"],
              ["platform_rpm", "Platform (req/min)"],
            ] as const
          ).map(([key, label]) => (
            <label key={key} className="block text-slate-400">
              {label}
              <input
                type="number" min={1}
                value={controls.rate_limits[key] ?? ""}
                onChange={(e) =>
                  setControls({
                    ...controls,
                    rate_limits: { ...controls.rate_limits, [key]: Number(e.target.value) },
                  })
                }
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
              />
            </label>
          ))}
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Cost <span className="normal-case text-slate-400">— caps enforced at the model seam; thresholds alert at 50/75/90/100%</span>
        </h2>
        <fieldset className="mt-3 text-sm text-slate-400">
          <legend>At cap</legend>
          <div className="mt-1 flex gap-4">
            {(
              [["alert", "Alert only (calls proceed)"], ["block", "Block (429 until next month or cap raise)"]] as const
            ).map(([value, label]) => (
              <label key={value} className="flex items-center gap-1.5">
                <input
                  type="radio"
                  name="at_cap"
                  checked={(controls.cost.at_cap ?? "alert") === value}
                  onChange={() =>
                    setControls({ ...controls, cost: { ...controls.cost, at_cap: value } })
                  }
                />
                {label}
              </label>
            ))}
          </div>
        </fieldset>
        <div className="mt-3 grid grid-cols-3 gap-4 text-sm">
          <label className="block text-slate-400">
            Platform budget ($/month)
            <input
              type="number" min={0} placeholder="not set"
              value={controls.cost.platform_budget_usd ?? ""}
              onChange={(e) =>
                setControls({
                  ...controls,
                  cost: {
                    ...controls.cost,
                    platform_budget_usd: e.target.value === "" ? null : Number(e.target.value),
                  },
                })
              }
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
            />
          </label>
          <label className="block text-slate-400">
            Default user cap ($)
            <input
              type="number" min={0}
              value={controls.cost.default_user_cap_usd ?? 500}
              onChange={(e) =>
                setControls({
                  ...controls,
                  cost: { ...controls.cost, default_user_cap_usd: Number(e.target.value) },
                })
              }
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
            />
          </label>
          <label className="block text-slate-400">
            Default project cap ($)
            <input
              type="number" min={0}
              value={controls.cost.default_project_cap_usd ?? 1000}
              onChange={(e) =>
                setControls({
                  ...controls,
                  cost: { ...controls.cost, default_project_cap_usd: Number(e.target.value) },
                })
              }
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
            />
          </label>
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Code generation{" "}
          <span className="normal-case text-slate-400">
            — which engine turns specs into applications (S9, OQ-1)
          </span>
        </h2>
        <fieldset className="mt-3 text-sm text-slate-400">
          <legend className="sr-only">Codegen provider</legend>
          <div className="flex flex-col gap-2">
            <label className="flex items-start gap-2">
              <input
                type="radio"
                name="codegen_provider"
                checked={(controls.codegen?.provider ?? "internal") === "internal"}
                onChange={() =>
                  setControls({ ...controls, codegen: { ...controls.codegen, provider: "internal" } })
                }
              />
              <span>
                <span className="text-slate-200">Internal (in-process)</span> — generation runs
                inside the platform; dies with restarts.
              </span>
            </label>
            <label className="flex items-start gap-2">
              <input
                type="radio"
                name="codegen_provider"
                checked={
                  // "kiro" is the legacy identifier older settings rows may still return
                  controls.codegen?.provider === "runner" || controls.codegen?.provider === "kiro"
                }
                onChange={() =>
                  setControls({ ...controls, codegen: { ...controls.codegen, provider: "runner" } })
                }
              />
              <span>
                <span className="text-slate-200">Workspace runner (CodeBuild)</span> — builds
                execute out-of-process via the S3 workspace contract; they survive platform
                restarts. The runner image is a reference engine behind the published S3
                workspace contract — any engine that speaks the contract can replace it.{" "}
                <a
                  href="https://github.com/xyme/marshal.build/blob/main/docs/codegen-workspace-contract.md"
                  target="_blank"
                  rel="noreferrer"
                  className="text-indigo-400 hover:underline"
                >
                  Contract ↗
                </a>
              </span>
            </label>
          </div>
        </fieldset>
        {codegenHealth && (
          <p className="mt-3 text-xs">
            <span
              className={`mr-1.5 inline-block h-2 w-2 rounded-full ${
                codegenHealth.runner.reachable ? "bg-emerald-400" : "bg-red-400"
              }`}
            />
            <span className="text-slate-400">
              Runner {codegenHealth.runner.reachable ? "reachable" : "unreachable"}
              {codegenHealth.runner.project && ` (${codegenHealth.runner.project})`}
              {codegenHealth.runner.detail && ` — ${codegenHealth.runner.detail}`}
              {" · "}active for new builds: {codegenHealth.active_provider}
            </span>
          </p>
        )}
        <p className="mt-2 text-xs text-slate-400">
          Switching affects NEW builds only. Governance is identical either way: same validation
          gate, same caps (dispatch pre-flight + post-ingest reconciliation), same audit trail.
        </p>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Security{" "}
          <span className="normal-case text-slate-400">
            — platform-wide authentication and data controls (S14)
          </span>
        </h2>
        <div className="mt-3 space-y-3 text-sm">
          <label className="flex items-start gap-2.5 text-slate-400">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={Boolean(controls.security?.admin_mfa_required)}
              onChange={(e) =>
                setControls({
                  ...controls,
                  security: { ...controls.security, admin_mfa_required: e.target.checked },
                })
              }
            />
            <span>
              <span className="text-slate-200">Require two-factor for administrators</span>
              {" — "}admin endpoints refuse sessions without a second factor. Enrol every
              admin (Profile → Two-factor authentication) BEFORE turning this on, or they
              lose admin access until they do.
            </span>
          </label>
          <label className="flex items-start gap-2.5 text-slate-400">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={Boolean(controls.security?.pii_redaction)}
              onChange={(e) =>
                setControls({
                  ...controls,
                  security: { ...controls.security, pii_redaction: e.target.checked },
                })
              }
            />
            <span>
              <span className="text-slate-200">Mask sensitive data in model calls</span>
              {" — "}applies a Bedrock guardrail that anonymises card numbers, bank
              details, government IDs and credentials in prompts and completions. Names,
              emails and addresses are deliberately NOT masked: they appear legitimately
              in specifications.
            </span>
          </label>
          {Boolean(controls.security?.pii_redaction) && (
            <fieldset className="ml-6 space-y-1.5 text-slate-400">
              <legend className="text-xs uppercase tracking-wide text-slate-400">
                Masking scope
              </legend>
              {(
                [
                  ["non_interactive", "Everything except live chat", "spec generation, risk scoring, titling — masking is invisible there; chat keeps its measured speed (recommended)"],
                  ["all", "All model calls", "includes chat — adds roughly a second before the first token appears (+41% p95, measured)"],
                ] as const
              ).map(([value, label, hint]) => (
                <label key={value} className="flex items-start gap-2.5">
                  <input
                    type="radio"
                    name="pii-scope"
                    className="mt-0.5"
                    checked={(controls.security?.pii_redaction_scope ?? "all") === value}
                    onChange={() =>
                      setControls({
                        ...controls,
                        security: { ...controls.security, pii_redaction_scope: value },
                      })
                    }
                  />
                  <span>
                    <span className="text-slate-200">{label}</span>
                    {" — "}{hint}
                  </span>
                </label>
              ))}
            </fieldset>
          )}
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Deployment policies{" "}
          <span className="normal-case text-slate-400">
            — Enclave bounds enforced at deploy pre-flight (S12)
          </span>
        </h2>
        <label className="mt-3 flex items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
          <input
            type="checkbox"
            checked={Boolean(controls.deployment_policies?.admissions_paused)}
            onChange={(event) =>
              setControls({
                ...controls,
                deployment_policies: {
                  ...controls.deployment_policies,
                  admissions_paused: event.target.checked,
                },
              })
            }
            className="mt-0.5"
          />
          <span>
            <span className="font-medium text-amber-200">Pause deployment admissions</span>
            <span className="mt-0.5 block text-xs text-slate-400">
              Blocks creates and updates before risk scoring or account leasing. Cloud also
              forces this posture when its persisted policy snapshot cannot be trusted.
            </span>
          </span>
        </label>
        <div className="mt-3 grid grid-cols-3 gap-4 text-sm">
          {(
            [
              ["max_concurrent_per_user", "Concurrent per user", 3, 1],
              ["max_concurrent_platform", "Concurrent platform-wide", 10, 1],
              ["provider_capacity_buffer", "Available account buffer", 1, 0],
              ["per_deployment_budget_usd", "Budget per deployment ($)", 200, 1],
              ["default_ttl_hours", "Default TTL (hours)", 72, 1],
              ["max_ttl_hours", "Max TTL (hours)", 168, 1],
            ] as const
          ).map(([key, label, fallback, minimum]) => (
            <label key={key} className="block text-slate-400">
              {label}
              <input
                type="number"
                min={minimum}
                value={controls.deployment_policies?.[key] ?? fallback}
                onChange={(e) =>
                  setControls({
                    ...controls,
                    deployment_policies: {
                      ...controls.deployment_policies,
                      [key]: Number(e.target.value),
                    },
                  })
                }
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
              />
            </label>
          ))}
        </div>
        <label className="mt-4 block max-w-xl text-sm text-slate-400">
          Endpoint authentication policy
          <select
            value={controls.deployment_policies?.require_endpoint_auth ?? "default"}
            onChange={(e) =>
              setControls({
                ...controls,
                deployment_policies: {
                  ...controls.deployment_policies,
                  require_endpoint_auth: e.target.value as "default" | "always",
                },
              })
            }
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-slate-200"
          >
            <option value="default">
              Authenticated by default — explicit PUBLIC opt-out allowed
            </option>
            <option value="always">
              Always authenticated — refuse explicit public builds
            </option>
          </select>
          <span className="mt-1 block text-xs text-slate-400">
            Applies at deployment pre-flight before risk scoring or account leasing.
            Legacy no-signal builds must be rebuilt under “Always.” Running deployments
            are untouched.
          </span>
        </label>
        <p className="mt-3 text-xs text-slate-400">
          Applies to new deployments only — running Enclaves keep the policy they launched
          with. In-place updates never count against concurrency. Deploys stay pinned to
          us-east-1 at Beta; the region allowlist unlocks with multi-region Enclave pools.
        </p>
      </section>

      <div className="flex items-center justify-end gap-3">
        {confirming ? (
          <>
            <span className="text-sm text-amber-300">
              This affects every user immediately. Save?
            </span>
            <button
              onClick={() => setConfirming(false)}
              className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
            >
              Cancel
            </button>
            <button
              onClick={save}
              disabled={busy}
              className="rounded-lg bg-amber-600 px-4 py-2 text-sm font-medium text-white hover:bg-amber-500 disabled:opacity-40"
            >
              {busy ? "Saving…" : "Confirm save"}
            </button>
          </>
        ) : (
          <button
            onClick={() => setConfirming(true)}
            disabled={readonly || controls.model_allowlist.length === 0}
            className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            Save changes
          </button>
        )}
      </div>
    </div>
  );
}

function BoundPair({
  label,
  value,
  onChange,
}: {
  label: string;
  value: { min: number; max: number };
  onChange: (key: "min" | "max", value: number) => void;
}) {
  return (
    <div className="text-slate-400">
      {label}
      <div className="mt-1 flex items-center gap-2">
        <input
          type="number" step={0.05} min={0} max={1}
          value={value.min}
          aria-label={`${label} minimum`}
          onChange={(e) => onChange("min", Number(e.target.value))}
          className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
        />
        <span className="text-slate-400">to</span>
        <input
          type="number" step={0.05} min={0} max={1}
          value={value.max}
          aria-label={`${label} maximum`}
          onChange={(e) => onChange("max", Number(e.target.value))}
          className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
        />
      </div>
    </div>
  );
}
