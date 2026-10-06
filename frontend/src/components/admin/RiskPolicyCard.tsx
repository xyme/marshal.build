"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import type { RiskPolicy } from "@/lib/types";

const FACTOR_LABELS: Record<string, string> = {
  data_sensitivity: "Data sensitivity",
  model_capability: "Model capability",
  user_facing: "User-facing exposure",
  deployment_scope: "Deployment scope",
  request_volume: "Request volume",
};
const ANCHOR_FACTORS = ["data_sensitivity", "user_facing", "request_volume"] as const;

/** B17: admin-tuned risk policy. Every save that changes the effective
 *  policy bumps the version — assessments under older versions stop opening
 *  the gate and re-score at the next deploy. The card says so before saving. */
export function RiskPolicyCard() {
  const readonly = useAdminReadonly();
  const [policy, setPolicy] = useState<RiskPolicy | null>(null);
  const [autoApprove, setAutoApprove] = useState(true);
  const [lowMax, setLowMax] = useState(30);
  const [mediumMax, setMediumMax] = useState(60);
  const [weights, setWeights] = useState<Record<string, number>>({});
  const [anchors, setAnchors] = useState<Record<string, string>>({});
  const [showAnchors, setShowAnchors] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const p = await api<RiskPolicy>("/v1/admin/governance/risk-policy");
      setPolicy(p);
      setAutoApprove(p.auto_approve_low);
      setLowMax(p.band_low_max);
      setMediumMax(p.band_medium_max);
      setWeights({ ...p.weights });
      setAnchors({ ...p.anchors });
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function save() {
    if (
      !window.confirm(
        `Save risk policy?\n\nIf the effective policy changes this bumps it to v${
          (policy?.policy_version ?? 1) + 1
        }. Assessments scored under the old policy stop opening the deploy gate — affected projects re-score automatically at their next deploy (undecided review items included).`
      )
    ) {
      return;
    }
    setSaving(true);
    try {
      const cleanedAnchors: Record<string, string> = {};
      for (const [k, v] of Object.entries(anchors)) {
        if (v.trim()) cleanedAnchors[k] = v.trim();
      }
      const p = await api<RiskPolicy>("/v1/admin/governance/risk-policy", {
        method: "PUT",
        body: JSON.stringify({
          auto_approve_low: autoApprove,
          band_low_max: lowMax,
          band_medium_max: mediumMax,
          weights,
          anchors: cleanedAnchors,
        }),
      });
      setPolicy(p);
      setAnchors({ ...p.anchors });
      toast.success(
        p.policy_version === policy?.policy_version
          ? "No effective change — policy version unchanged"
          : `Risk policy saved — now v${p.policy_version}`
      );
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  if (!policy) {
    return <p className="text-slate-400">{error ?? "Loading policy…"}</p>;
  }

  const weightTotal = Object.values(weights).reduce((a, b) => a + (b || 0), 0);

  return (
    <div className="max-w-2xl space-y-6">
      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
            Scoring policy
          </h2>
          <span
            className="rounded bg-slate-800 px-2 py-0.5 text-xs text-slate-300"
            title={
              policy.is_default
                ? "Platform defaults — never changed"
                : "Assessments display the version they were scored under"
            }
          >
            policy v{policy.policy_version}
            {policy.is_default ? " (defaults)" : ""}
          </span>
        </div>

        <label className="flex items-center gap-3 text-sm">
          <input
            type="checkbox"
            checked={autoApprove}
            onChange={(e) => setAutoApprove(e.target.checked)}
          />
          <span>
            Auto-approve <strong>low</strong>-band assessments
            <span className="block text-xs text-slate-400">
              Off means every assessment queues for review, including low.
            </span>
          </span>
        </label>

        <div className="mt-4 grid grid-cols-2 gap-4 text-sm">
          <label className="space-y-1">
            <span className="text-slate-400">Low band up to (score ≤)</span>
            <input
              type="number"
              min={1}
              max={99}
              value={lowMax}
              onChange={(e) => setLowMax(Number(e.target.value))}
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
            />
          </label>
          <label className="space-y-1">
            <span className="text-slate-400">Medium band up to (score ≤)</span>
            <input
              type="number"
              min={2}
              max={99}
              value={mediumMax}
              onChange={(e) => setMediumMax(Number(e.target.value))}
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
            />
          </label>
        </div>
        <p className="mt-1 text-xs text-slate-500">
          Above the medium bound is <strong>high</strong>. Bounds are on the 0–100 weighted
          score.
        </p>
      </div>

      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="mb-1 text-sm font-medium uppercase tracking-wide text-slate-400">
          Factor weights
        </h2>
        <p className="mb-4 text-xs text-slate-500">
          Enter proportions — they normalize automatically. Effective share shown beside each.
        </p>
        <div className="space-y-2">
          {Object.keys(FACTOR_LABELS).map((factor) => (
            <label key={factor} className="flex items-center justify-between gap-3 text-sm">
              <span className="w-44 text-slate-300">{FACTOR_LABELS[factor]}</span>
              <input
                type="number"
                min={0.01}
                step={0.05}
                value={weights[factor] ?? 0}
                onChange={(e) =>
                  setWeights((w) => ({ ...w, [factor]: Number(e.target.value) }))
                }
                className="w-24 rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-right"
              />
              <span className="w-14 text-right text-xs text-slate-400">
                {weightTotal > 0
                  ? `${(((weights[factor] ?? 0) / weightTotal) * 100).toFixed(0)}%`
                  : "—"}
              </span>
            </label>
          ))}
        </div>
      </div>

      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <button
          onClick={() => setShowAnchors((s) => !s)}
          className="flex w-full items-center justify-between text-sm font-medium uppercase tracking-wide text-slate-400"
        >
          <span>Rubric anchors (highest friction)</span>
          <span>{showAnchors ? "▾" : "▸"}</span>
        </button>
        {showAnchors && (
          <div className="mt-4 space-y-4">
            <p className="text-xs text-amber-300/90">
              Anchor text changes how the model scores — the strongest knob here. Leave a
              field empty to keep the platform anchor.
            </p>
            {ANCHOR_FACTORS.map((factor) => (
              <label key={factor} className="block space-y-1 text-sm">
                <span className="text-slate-300">{FACTOR_LABELS[factor]}</span>
                <textarea
                  value={anchors[factor] ?? ""}
                  onChange={(e) =>
                    setAnchors((a) => ({ ...a, [factor]: e.target.value }))
                  }
                  placeholder={policy.default_anchors[factor]}
                  maxLength={600}
                  rows={2}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs"
                />
              </label>
            ))}
          </div>
        )}
      </div>

      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      <div className="flex items-center gap-3">
        <button
          onClick={save}
          disabled={saving || readonly}
          className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium hover:bg-indigo-500 disabled:opacity-40"
        >
          {saving ? "Saving…" : "Save policy"}
        </button>
        <button
          onClick={load}
          disabled={saving}
          className="rounded-lg border border-slate-700 px-4 py-2 text-sm hover:border-slate-500 disabled:opacity-40"
        >
          Reset
        </button>
      </div>
    </div>
  );
}
