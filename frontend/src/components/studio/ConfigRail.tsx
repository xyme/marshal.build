"use client";

import { useState } from "react";
import { modelLabel } from "@/lib/marketplace-ui";
import type { EffectiveParamsResp } from "@/lib/types";

/** Studio config rail (S18-03/R3): model picker + param controls showing
 * requested vs EFFECTIVE values from the backend's own clamp path — never a
 * client-side recalculation. Changes persist via the session PATCH (audited).
 */
export default function ConfigRail({
  eff,
  busy,
  onChange,
}: {
  eff: EffectiveParamsResp | null;
  busy: boolean;
  onChange: (fields: { model_id?: string; temperature?: number; max_tokens?: number }) => void;
}) {
  const [tempDraft, setTempDraft] = useState<number | null>(null);
  const [tokensDraft, setTokensDraft] = useState<number | null>(null);

  if (!eff) {
    return (
      <aside aria-label="Session configuration" className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
        <p className="text-sm text-slate-400">Loading configuration…</p>
      </aside>
    );
  }

  const requestedTemp = eff.requested.temperature;
  const shownTemp = tempDraft ?? requestedTemp ?? eff.effective.temperature ?? 0.7;
  const shownTokens = tokensDraft ?? eff.requested.max_tokens ?? eff.effective.max_tokens;

  return (
    <aside
      aria-label="Session configuration"
      className="flex flex-col gap-4 rounded-xl border border-slate-800 bg-slate-900/40 p-4"
    >
      <h2 className="text-xs font-medium uppercase tracking-wide text-slate-400">
        Model & parameters
      </h2>

      {/* Model picker — registry-backed domain from the resolution endpoint */}
      <label className="block text-xs text-slate-400">
        Model
        <select
          value={eff.effective.model_id}
          disabled={busy}
          onChange={(e) => onChange({ model_id: e.target.value })}
          className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-100 disabled:opacity-50"
        >
          {eff.allowed_models.map((id) => (
            <option key={id} value={id}>
              {modelLabel(id)}
              {id.startsWith("ext/") ? " (external)" : ""}
            </option>
          ))}
        </select>
        {eff.effective.model_id.startsWith("ext/") && (
          <span
            className="mt-1 inline-block rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-sky-300"
            title="Custom endpoint hosted outside Bedrock — prompts leave AWS"
          >
            External
          </span>
        )}
        {eff.clamped_by.model_id && (
          <span className="mt-1 block text-[11px] text-amber-300">
            ⚠ requested {eff.requested.model_id ? modelLabel(eff.requested.model_id) : "model"} is
            not allowed by {eff.clamped_by.model_id} — using{" "}
            {modelLabel(eff.effective.model_id)}
          </span>
        )}
      </label>

      {/* Temperature — native range input (R5.2: keyboard-operable) */}
      <label className="block text-xs text-slate-400">
        <span className="flex items-center justify-between">
          Temperature
          <span className="tabular-nums text-slate-300">{shownTemp.toFixed(2)}</span>
        </span>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={shownTemp}
          disabled={busy}
          onChange={(e) => setTempDraft(Number(e.target.value))}
          onMouseUp={() => tempDraft !== null && (onChange({ temperature: tempDraft }), setTempDraft(null))}
          onTouchEnd={() => tempDraft !== null && (onChange({ temperature: tempDraft }), setTempDraft(null))}
          onKeyUp={(e) => {
            if (tempDraft !== null && (e.key.startsWith("Arrow") || e.key === "Home" || e.key === "End")) {
              onChange({ temperature: tempDraft });
              setTempDraft(null);
            }
          }}
          className="mt-1 w-full accent-indigo-500"
          aria-describedby="temp-clamp-note"
        />
        <span id="temp-clamp-note" className="block min-h-4 text-[11px]">
          {eff.clamped_by.temperature ? (
            <span className="text-amber-300">
              ⚠ {requestedTemp} → clamped to {eff.effective.temperature} by{" "}
              {eff.clamped_by.temperature}
            </span>
          ) : requestedTemp === null ? (
            <span className="text-slate-400">platform default (unset)</span>
          ) : (
            <span className="text-slate-400">applied as requested</span>
          )}
        </span>
      </label>

      {/* Max tokens */}
      <label className="block text-xs text-slate-400">
        <span className="flex items-center justify-between">
          Max tokens
          <span className="tabular-nums text-slate-300">{shownTokens}</span>
        </span>
        <input
          type="number"
          min={256}
          max={65536}
          step={256}
          value={shownTokens}
          disabled={busy}
          onChange={(e) => setTokensDraft(Number(e.target.value))}
          onBlur={() => tokensDraft !== null && (onChange({ max_tokens: tokensDraft }), setTokensDraft(null))}
          className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-100 disabled:opacity-50"
          aria-describedby="tokens-clamp-note"
        />
        <span id="tokens-clamp-note" className="block min-h-4 text-[11px]">
          {eff.clamped_by.max_tokens ? (
            <span className="text-amber-300">
              ⚠ {eff.requested.max_tokens} → clamped to {eff.effective.max_tokens} by{" "}
              {eff.clamped_by.max_tokens}
            </span>
          ) : (
            <span className="text-slate-400">ceiling {eff.effective.max_tokens}</span>
          )}
        </span>
      </label>

      {/* Guardrail chips: mandatory vs configurable, text not color (R5.2) */}
      <div>
        <h3 className="text-xs font-medium uppercase tracking-wide text-slate-400">Guardrails</h3>
        <ul className="mt-2 flex flex-wrap gap-1.5">
          {eff.template_name && (
            <Chip mandatory label={`Template: ${eff.template_name}`} />
          )}
          <Chip mandatory label="Platform bounds" />
          <Chip mandatory={false} label="Temperature" />
          <Chip mandatory={false} label="Max tokens" />
          <Chip mandatory={false} label={`Models (${eff.allowed_models.length})`} />
        </ul>
        <p className="mt-2 text-[11px] text-slate-400">
          Mandatory rails come from your admin and template — the studio shows them,
          it cannot loosen them.
        </p>
      </div>
    </aside>
  );
}

function Chip({ label, mandatory }: { label: string; mandatory: boolean }) {
  return (
    <li
      className={`rounded-full border px-2 py-0.5 text-[10px] ${
        mandatory
          ? "border-slate-600 bg-slate-800 text-slate-300"
          : "border-indigo-500/40 bg-indigo-500/10 text-indigo-300"
      }`}
    >
      {mandatory ? "🔒 " : ""}
      {label}
      <span className="sr-only">{mandatory ? " (mandatory)" : " (configurable)"}</span>
    </li>
  );
}
