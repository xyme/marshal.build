"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { RISK_STYLE } from "@/lib/governance-ui";
import type { ProjectRisk, RiskFactor } from "@/lib/types";

export function RiskBadge({ level, score }: { level: string | null; score?: number | null }) {
  if (!level) {
    return <span className="text-xs text-slate-400" title="Not scored yet">risk: —</span>;
  }
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${RISK_STYLE[level] ?? "bg-slate-700/40 text-slate-400"}`}
      title={score != null ? `Risk score ${score}/100` : undefined}
    >
      risk: {level}
    </span>
  );
}

const FACTOR_LABELS: Record<string, string> = {
  data_sensitivity: "Data sensitivity",
  model_capability: "Model capability",
  user_facing: "User-facing exposure",
  deployment_scope: "Deployment scope",
  request_volume: "Request volume",
};

interface TimelineEntry {
  id: string;
  score: number | null;
  level: string | null;
  decision: string | null;
  notes: string | null;
  resubmission_of: string | null;
  owner_comment: string | null;
  created_at: string;
  decided_at: string | null;
}

/** Owner-visible assessment detail + decision timeline (S4 R3.5, S6 R5.1/R3.4). */
export function AssessmentDrawer({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  const [risk, setRisk] = useState<(ProjectRisk & { id?: string; timeline?: TimelineEntry[] }) | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () =>
    api<ProjectRisk & { id?: string; timeline?: TimelineEntry[] }>(`/v1/projects/${projectId}/risk`)
      .then(setRisk)
      .catch(() => {});

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  if (!risk?.assessed || risk.status === "error") return null;

  const canComment =
    risk.decision === "changes_requested" && !(risk as { owner_comment?: string }).owner_comment;

  async function sendComment() {
    if (!comment.trim() || !risk?.id) return;
    setBusy(true);
    try {
      await api(`/v1/risk-assessments/${risk.id}/comment`, {
        method: "POST",
        body: JSON.stringify({ body: comment.trim() }),
      });
      setComment("");
      load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between px-5 py-3 text-sm"
      >
        <span className="flex items-center gap-3">
          <span className="font-medium">Risk assessment</span>
          <RiskBadge level={risk.level ?? null} score={risk.score} />
          {risk.policy_version != null && (
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] ${
                risk.current_policy_version != null &&
                risk.policy_version !== risk.current_policy_version
                  ? "bg-amber-500/15 text-amber-300"
                  : "bg-slate-800 text-slate-400"
              }`}
              title={
                risk.current_policy_version != null &&
                risk.policy_version !== risk.current_policy_version
                  ? `Scored under policy v${risk.policy_version}; current is v${risk.current_policy_version} — re-scores at the next deploy`
                  : "Risk policy version this assessment was scored under"
              }
            >
              policy v{risk.policy_version}
              {risk.current_policy_version != null &&
              risk.policy_version !== risk.current_policy_version
                ? " (superseded)"
                : ""}
            </span>
          )}
          <span className="text-slate-400">
            {risk.score}/100 · {risk.decision?.replace(/_/g, " ")}
            {risk.current === false && " · from an earlier version"}
          </span>
        </span>
        <span className="text-slate-400">{open ? "▾" : "▸"}</span>
      </button>
      {open && risk.factors && (
        <div className="space-y-2 border-t border-slate-800 px-5 py-4">
          {Object.entries(risk.factors).map(([name, factor]) => (
            <FactorRow key={name} name={FACTOR_LABELS[name] ?? name} factor={factor} />
          ))}
          {risk.notes && (
            <p className="mt-2 rounded-md bg-slate-950/60 px-3 py-2 text-sm text-slate-400">
              Reviewer notes: {risk.notes}
            </p>
          )}
          {canComment && (
            <div className="mt-2 flex gap-2">
              <input
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                placeholder="One reply to the reviewer (optional)…"
                maxLength={2000}
                className="flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
              />
              <button
                onClick={sendComment}
                disabled={busy || !comment.trim()}
                className="rounded-lg border border-slate-700 px-3 py-2 text-sm hover:border-slate-500 disabled:opacity-40"
              >
                Send
              </button>
            </div>
          )}
          {risk.timeline && risk.timeline.length > 1 && (
            <div className="mt-3 border-t border-slate-800 pt-3">
              <h4 className="text-xs font-medium uppercase tracking-wide text-slate-400">
                Assessment history
              </h4>
              <ul className="mt-2 space-y-1.5 text-xs text-slate-400">
                {risk.timeline.map((entry) => (
                  <li key={entry.id} className="flex items-baseline gap-2">
                    <span className="shrink-0 font-mono text-slate-400">
                      {new Date(entry.created_at).toLocaleDateString()}
                    </span>
                    <span>
                      {entry.score}/100 ({entry.level}) — {entry.decision?.replace(/_/g, " ")}
                      {entry.resubmission_of && " · resubmission"}
                      {entry.notes && ` · "${entry.notes.slice(0, 80)}"`}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function FactorRow({ name, factor }: { name: string; factor: RiskFactor }) {
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="w-44 shrink-0 text-slate-400">{name}</span>
      <div className="h-2 w-36 overflow-hidden rounded bg-slate-800">
        <div
          className={`h-full ${factor.score >= 7 ? "bg-red-500" : factor.score >= 4 ? "bg-amber-500" : "bg-emerald-500"}`}
          style={{ width: `${factor.score * 10}%` }}
        />
      </div>
      <span className="w-10 shrink-0 font-mono text-xs text-slate-400">{factor.score}/10</span>
      <span className="truncate text-xs text-slate-400" title={factor.rationale}>
        {factor.rationale}
      </span>
    </div>
  );
}
