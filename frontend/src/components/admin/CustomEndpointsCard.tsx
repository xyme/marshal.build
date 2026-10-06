"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import { ModalShell } from "@/components/ui/primitives";

/** Admin → Model Controls → Custom endpoints (S17-02/03).
 *
 * OpenAI-compatible endpoints hosted outside Bedrock. The API key is
 * write-only: it goes to Secrets Manager and never comes back — the form
 * shows only `has_api_key`. Prices are REQUIRED [D15] so cost caps govern
 * external calls exactly like Bedrock ones.
 */

export interface CustomEndpoint {
  slug: string;
  id: string;
  label: string;
  base_url: string;
  model_name: string;
  tier: string;
  usd_per_1k_input: number;
  usd_per_1k_output: number;
  max_context_tokens: number;
  timeout_s: number;
  has_api_key: boolean;
  enabled: boolean;
}

interface ProbeResult {
  ok: boolean;
  rtt_ms?: number;
  model_echo?: string | null;
  streaming?: boolean;
  error_class?: string;
  error?: string;
}

const EMPTY_FORM = {
  slug: "",
  label: "",
  base_url: "",
  model_name: "",
  tier: "standard",
  usd_per_1k_input: "",
  usd_per_1k_output: "",
  max_context_tokens: "8192",
  timeout_s: "60",
  api_key: "",
};

export default function CustomEndpointsCard({ onChanged }: { onChanged?: () => void }) {
  const readonly = useAdminReadonly();
  const [endpoints, setEndpoints] = useState<CustomEndpoint[] | null>(null);
  const [editing, setEditing] = useState<CustomEndpoint | "new" | null>(null);
  const [probes, setProbes] = useState<Record<string, ProbeResult | "running">>({});
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setEndpoints(await api<CustomEndpoint[]>("/v1/admin/model-endpoints"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function toggleEnabled(endpoint: CustomEndpoint) {
    try {
      await api(`/v1/admin/model-endpoints/${endpoint.slug}`, {
        method: "PUT",
        body: JSON.stringify({ enabled: !endpoint.enabled }),
      });
      toast.success(
        endpoint.enabled
          ? `"${endpoint.label}" disabled — it leaves every allowlist within 60s.`
          : `"${endpoint.label}" enabled.`
      );
      await load();
      onChanged?.();
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  async function probe(slug: string) {
    setProbes((p) => ({ ...p, [slug]: "running" }));
    try {
      const result = await api<ProbeResult>(`/v1/admin/model-endpoints/${slug}/probe`, {
        method: "POST",
      });
      setProbes((p) => ({ ...p, [slug]: result }));
    } catch (e) {
      setProbes((p) => ({
        ...p,
        [slug]: { ok: false, error_class: "RequestFailed", error: (e as Error).message },
      }));
    }
  }

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
            Custom endpoints
          </h2>
          <p className="mt-1 text-xs text-slate-400">
            OpenAI-compatible models hosted outside Bedrock (HTTPS only). Prompts sent to
            these endpoints <strong className="text-slate-300">leave AWS</strong>; calls are
            badged, priced from the rates below, and audited.
          </p>
        </div>
        <button
          onClick={() => setEditing("new")}
          disabled={readonly}
          className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-indigo-400 disabled:opacity-40"
        >
          + Add endpoint
        </button>
      </div>

      {error && (
        <p className="mt-3 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {endpoints === null ? (
        <p className="mt-3 text-sm text-slate-400">Loading…</p>
      ) : endpoints.length === 0 ? (
        <p className="mt-3 rounded-lg border border-dashed border-slate-800 p-4 text-center text-sm text-slate-400">
          No custom endpoints. Bedrock models remain the platform defaults.
        </p>
      ) : (
        <ul className="mt-3 space-y-3">
          {endpoints.map((endpoint) => {
            const probeState = probes[endpoint.slug];
            return (
              <li
                key={endpoint.slug}
                className="rounded-lg border border-slate-800 bg-slate-950/50 p-4"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{endpoint.label}</span>
                  <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-sky-300">
                    External
                  </span>
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-400">
                    {endpoint.tier}
                  </span>
                  {!endpoint.enabled && (
                    <span className="rounded bg-slate-700/60 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-300">
                      Disabled
                    </span>
                  )}
                  <code className="ml-auto rounded bg-slate-950 px-1.5 py-0.5 text-[11px] text-slate-400">
                    {endpoint.id}
                  </code>
                </div>
                <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs text-slate-400 sm:grid-cols-2">
                  <div className="truncate">
                    <dt className="inline text-slate-400">URL: </dt>
                    <dd className="inline">{endpoint.base_url}</dd>
                  </div>
                  <div>
                    <dt className="inline text-slate-400">Model: </dt>
                    <dd className="inline">{endpoint.model_name}</dd>
                  </div>
                  <div>
                    <dt className="inline text-slate-400">Price /1k: </dt>
                    <dd className="inline">
                      ${endpoint.usd_per_1k_input} in · ${endpoint.usd_per_1k_output} out
                    </dd>
                  </div>
                  <div>
                    <dt className="inline text-slate-400">Key: </dt>
                    <dd className="inline">{endpoint.has_api_key ? "stored (masked)" : "none"}</dd>
                  </div>
                </dl>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => probe(endpoint.slug)}
                    disabled={probeState === "running" || readonly}
                    className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs hover:border-indigo-400 disabled:opacity-40"
                  >
                    {probeState === "running" ? "Probing…" : "▶ Test connection"}
                  </button>
                  <button
                    onClick={() => setEditing(endpoint)}
                    disabled={readonly}
                    className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs hover:border-slate-500 disabled:opacity-40"
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => toggleEnabled(endpoint)}
                    disabled={readonly}
                    className={`rounded-lg border px-3 py-1.5 text-xs disabled:opacity-40 ${
                      endpoint.enabled
                        ? "border-amber-500/50 text-amber-300 hover:bg-amber-500/10"
                        : "border-emerald-500/50 text-emerald-300 hover:bg-emerald-500/10"
                    }`}
                  >
                    {endpoint.enabled ? "Disable" : "Enable"}
                  </button>
                  {probeState && probeState !== "running" && (
                    <span
                      className={`text-xs ${probeState.ok ? "text-emerald-400" : "text-red-300"}`}
                    >
                      {probeState.ok
                        ? `OK · ${probeState.rtt_ms}ms · model=${probeState.model_echo ?? "?"} · streaming ${probeState.streaming ? "yes" : "no"}`
                        : `${probeState.error_class ?? "Failed"}: ${probeState.error ?? "no detail"}`}
                    </span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {editing && (
        <EndpointForm
          endpoint={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await load();
            onChanged?.();
          }}
        />
      )}
    </section>
  );
}

function EndpointForm({
  endpoint,
  onClose,
  onSaved,
}: {
  endpoint: CustomEndpoint | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState(() =>
    endpoint
      ? {
          slug: endpoint.slug,
          label: endpoint.label,
          base_url: endpoint.base_url,
          model_name: endpoint.model_name,
          tier: endpoint.tier,
          usd_per_1k_input: String(endpoint.usd_per_1k_input),
          usd_per_1k_output: String(endpoint.usd_per_1k_output),
          max_context_tokens: String(endpoint.max_context_tokens),
          timeout_s: String(endpoint.timeout_s),
          api_key: "",
        }
      : EMPTY_FORM
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const creating = endpoint === null;

  function set(key: keyof typeof EMPTY_FORM, value: string) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function save() {
    setBusy(true);
    setError(null);
    const body: Record<string, unknown> = {
      label: form.label.trim(),
      base_url: form.base_url.trim(),
      model_name: form.model_name.trim(),
      tier: form.tier,
      usd_per_1k_input: Number(form.usd_per_1k_input),
      usd_per_1k_output: Number(form.usd_per_1k_output),
      max_context_tokens: Number(form.max_context_tokens),
      timeout_s: Number(form.timeout_s),
    };
    if (form.api_key.trim()) body.api_key = form.api_key.trim();
    try {
      if (creating) {
        await api("/v1/admin/model-endpoints", {
          method: "POST",
          body: JSON.stringify({ ...body, slug: form.slug.trim() }),
        });
        toast.success("Endpoint added — allowlist it below to make it selectable.");
      } else {
        await api(`/v1/admin/model-endpoints/${endpoint.slug}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
        toast.success("Endpoint updated.");
      }
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <ModalShell label={creating ? "Add custom endpoint" : `Edit ${endpoint?.label}`} onClose={onClose}>
      <h2 className="text-lg font-medium">
        {creating ? "Add custom endpoint" : `Edit "${endpoint?.label}"`}
      </h2>
      <p className="mt-1 text-xs text-slate-400">
        Must speak the OpenAI-compatible <code>/chat/completions</code> contract over HTTPS.
      </p>
      {error && (
        <p className="mt-3 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <Text label="Slug (permanent — becomes ext/<slug>)" value={form.slug} disabled={!creating} onChange={(v) => set("slug", v)} placeholder="onprem-llama70b" />
        <Text label="Display label" value={form.label} onChange={(v) => set("label", v)} placeholder="ACME Llama 3 70B" />
        <Text label="Base URL (…/v1)" value={form.base_url} onChange={(v) => set("base_url", v)} placeholder="https://models.example.com/v1" wide />
        <Text label="Model name (API payload)" value={form.model_name} onChange={(v) => set("model_name", v)} placeholder="llama-3-70b-instruct" />
        <label className="block text-xs text-slate-400">
          Tier
          <select
            value={form.tier}
            onChange={(e) => set("tier", e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
          >
            <option value="standard">standard</option>
            <option value="fast">fast</option>
            <option value="advanced">advanced</option>
          </select>
        </label>
        <Text label="USD / 1k input tokens (required)" value={form.usd_per_1k_input} onChange={(v) => set("usd_per_1k_input", v)} placeholder="0.0009" />
        <Text label="USD / 1k output tokens (required)" value={form.usd_per_1k_output} onChange={(v) => set("usd_per_1k_output", v)} placeholder="0.0011" />
        <Text label="Max context tokens" value={form.max_context_tokens} onChange={(v) => set("max_context_tokens", v)} />
        <Text label="Timeout (seconds)" value={form.timeout_s} onChange={(v) => set("timeout_s", v)} />
        <label className="block text-xs text-slate-400 sm:col-span-2">
          API key {endpoint?.has_api_key && "(stored — leave blank to keep)"}
          <input
            type="password"
            autoComplete="off"
            value={form.api_key}
            onChange={(e) => set("api_key", e.target.value)}
            placeholder={endpoint?.has_api_key ? "••••••••" : "optional"}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
          />
          <span className="mt-1 block text-[11px] text-slate-400">
            Write-only: stored in Secrets Manager, never displayed again.
          </span>
        </label>
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200">
          Cancel
        </button>
        <button
          onClick={save}
          disabled={busy}
          className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
        >
          {busy ? "Saving…" : creating ? "Add endpoint" : "Save changes"}
        </button>
      </div>
    </ModalShell>
  );
}

function Text({
  label,
  value,
  onChange,
  placeholder,
  disabled,
  wide,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
  wide?: boolean;
}) {
  return (
    <label className={`block text-xs text-slate-400 ${wide ? "sm:col-span-2" : ""}`}>
      {label}
      <input
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 disabled:opacity-50"
      />
    </label>
  );
}
