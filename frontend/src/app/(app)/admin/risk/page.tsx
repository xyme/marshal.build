"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { RiskPolicyCard } from "@/components/admin/RiskPolicyCard";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import { RISK_STYLE } from "@/lib/governance-ui";
import type { AdminUser, AdminUserList, RiskAssessment, RiskList } from "@/lib/types";

const FACTOR_LABELS: Record<string, string> = {
  data_sensitivity: "Data sensitivity",
  model_capability: "Model capability",
  user_facing: "User-facing exposure",
  deployment_scope: "Deployment scope",
  request_volume: "Request volume",
};

/** Admin > Risk (FSD §4.6.4, S4-03 + S6 workflow): assessments + reviewer groups. */
export default function AdminRiskPage() {
  const [tab, setTab] = useState<"assessments" | "reviewers" | "policy">("assessments");
  const [status, setStatus] = useState("pending");
  const [data, setData] = useState<RiskList | null>(null);
  const [deciding, setDeciding] = useState<RiskAssessment | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api<RiskList>(`/v1/admin/risk-assessments?status=${status}&page_size=50`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [status]);

  useEffect(() => {
    load();
  }, [load]);

  function exportCsv() {
    window.open("/api/backend/v1/admin/risk-assessments/export", "_blank");
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Risk</h1>
          <p className="mt-1 text-sm text-slate-400">
            Automated scoring on every spec change; medium routes to Managers, high to the
            Governance Board (48h/72h escalation per FSD §4.6.7).
          </p>
        </div>
        <div className="flex items-center gap-2">
          {tab === "assessments" && (
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
            >
              <option value="pending">Pending</option>
              <option value="approved">Approved</option>
              <option value="rejected">Rejected</option>
              <option value="changes_requested">Changes requested</option>
              <option value="auto_approved">Auto-approved</option>
              <option value="all">All</option>
            </select>
          )}
          <button
            onClick={exportCsv}
            className="rounded-lg border border-slate-700 px-3 py-2 text-sm hover:border-slate-500"
          >
            Export CSV
          </button>
        </div>
      </div>

      <div className="flex gap-1 border-b border-slate-800 text-sm">
        {(
          [
            ["assessments", "Assessments"],
            ["reviewers", "Reviewers"],
            ["policy", "Policy"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`rounded-t-lg px-4 py-2 ${
              tab === key
                ? "border border-b-0 border-slate-800 bg-slate-900/40 text-white"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "reviewers" ? (
        <ReviewersTab />
      ) : tab === "policy" ? (
        <RiskPolicyCard />
      ) : (
        <>
      {data && (
        <p className="text-sm text-slate-400">
          Pending review: <strong className="text-white">{data.pending}</strong> · Approved (30d):{" "}
          <strong className="text-white">{data.approved_30d}</strong> · Rejected (30d):{" "}
          <strong className="text-white">{data.rejected_30d}</strong>
        </p>
      )}

      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {data === null ? (
        <p className="text-slate-400">Loading…</p>
      ) : data.items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 p-10 text-center text-slate-400">
          Nothing {status === "all" ? "assessed yet" : status.replace(/_/g, " ")}. Assessments
          appear automatically when specs are saved or generated.
        </div>
      ) : (
        <div className="space-y-4">
          {data.items.map((a) => (
            <AssessmentCard key={a.id} assessment={a} onDecide={() => setDeciding(a)} />
          ))}
        </div>
      )}

      {deciding && (
        <DecideModal
          assessment={deciding}
          onClose={() => setDeciding(null)}
          onDone={(approved) => {
            setDeciding(null);
            toast.success(approved ? "Approved — the owner can deploy now." : "Decision recorded.");
            load();
          }}
          onError={(msg) => setError(msg)}
        />
      )}
        </>
      )}
    </div>
  );
}

interface GroupMember {
  id: string;
  email: string;
  name: string | null;
  pending: number;
}

interface ReviewerGroups {
  groups: Record<string, GroupMember[]>;
  counters: {
    pending_by_group: Record<string, number>;
    median_decision_hours_30d: number | null;
    escalations_30d: number;
  };
}

const GROUP_TITLES: Record<string, string> = {
  managers: "Managers (medium risk)",
  governance_board: "AI Governance Board (high risk)",
};

function ReviewersTab() {
  const readonly = useAdminReadonly();
  const [data, setData] = useState<ReviewerGroups | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api<ReviewerGroups>("/v1/admin/reviewer-groups"));
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
    api<AdminUserList>("/v1/admin/users?page_size=200")
      .then((l) => setUsers(l.items))
      .catch(() => {});
  }, [load]);

  async function setMembers(group: string, ids: string[]) {
    try {
      await api(`/v1/admin/reviewer-groups/${group}`, {
        method: "PUT",
        body: JSON.stringify({ user_ids: ids }),
      });
      toast.success("Reviewer group updated.");
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (error) {
    return <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</p>;
  }
  if (!data) return <p className="text-slate-400">Loading…</p>;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-3 gap-3">
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
          <div className="text-xl font-semibold">
            {Object.values(data.counters.pending_by_group).reduce((a, b) => a + b, 0)}
          </div>
          <div className="mt-0.5 text-xs uppercase tracking-wide text-slate-400">Pending total</div>
        </div>
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
          <div className="text-xl font-semibold">
            {data.counters.median_decision_hours_30d != null
              ? `${data.counters.median_decision_hours_30d}h`
              : "—"}
          </div>
          <div className="mt-0.5 text-xs uppercase tracking-wide text-slate-400">
            Median decision time (30d)
          </div>
        </div>
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
          <div className="text-xl font-semibold">{data.counters.escalations_30d}</div>
          <div className="mt-0.5 text-xs uppercase tracking-wide text-slate-400">Escalations (30d)</div>
        </div>
      </div>

      {Object.entries(GROUP_TITLES).map(([group, title]) => {
        const members = data.groups[group] ?? [];
        const memberIds = new Set(members.map((m) => m.id));
        return (
          <section key={group} className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
            <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">{title}</h2>
            {members.length === 0 && (
              <p className="mt-2 text-sm text-amber-300">
                No members — reviews route past this group (a configuration alert fires).
              </p>
            )}
            <ul className="mt-3 space-y-2">
              {members.map((m) => (
                <li key={m.id} className="flex items-center justify-between text-sm">
                  <span>
                    {m.name ?? m.email}
                    <span className="ml-2 text-xs text-slate-400">{m.email}</span>
                    {m.pending > 0 && (
                      <span className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-400">
                        {m.pending} pending
                      </span>
                    )}
                  </span>
                  <button
                    onClick={() =>
                      setMembers(group, members.filter((x) => x.id !== m.id).map((x) => x.id))
                    }
                    disabled={readonly}
                    className="text-xs text-red-400 hover:text-red-300 disabled:opacity-40"
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            <select
              value=""
              onChange={(e) => {
                if (e.target.value) setMembers(group, [...memberIds, e.target.value] as string[]);
              }}
              disabled={readonly}
              className="mt-3 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm disabled:opacity-50"
              aria-label={`Add member to ${title}`}
            >
              <option value="">+ Add member…</option>
              {users
                .filter((u) => !memberIds.has(u.id) && u.status === "active")
                .map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.email} ({u.persona ?? u.role})
                  </option>
                ))}
            </select>
          </section>
        );
      })}
    </div>
  );
}

function AssessmentCard({
  assessment,
  onDecide,
}: {
  assessment: RiskAssessment;
  onDecide: () => void;
}) {
  const readonly = useAdminReadonly();
  const level = assessment.level ?? "low";
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
      <div className="flex flex-wrap items-center gap-3">
        <span className={`rounded px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${RISK_STYLE[level] ?? "bg-slate-700/40 text-slate-400"}`}>
          {assessment.status === "error" ? "error" : level}
        </span>
        <h2 className="font-medium">
          {assessment.project_name ?? "(project)"}
        </h2>
        <span className="text-sm text-slate-400">
          {assessment.owner_email} · {assessment.score != null ? `${assessment.score}/100` : "—"} ·{" "}
          {new Date(assessment.created_at).toLocaleString()}
        </span>
        <span className="ml-auto flex items-center gap-2">
          <Link
            href={`/projects/${assessment.project_id}?tab=spec`}
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-slate-500"
          >
            View spec
          </Link>
          {assessment.decision === "pending" && !readonly && (
            <button
              onClick={onDecide}
              className="rounded-lg bg-indigo-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-400"
            >
              Review…
            </button>
          )}
          {assessment.decision && assessment.decision !== "pending" && (
            <span className="rounded bg-slate-800 px-2 py-0.5 text-xs uppercase tracking-wide text-slate-400">
              {assessment.decision.replace(/_/g, " ")}
            </span>
          )}
        </span>
      </div>

      {assessment.status === "error" ? (
        <p className="mt-3 text-sm text-red-300">
          Scoring failed: {assessment.error ?? "unknown error"} — re-triggers on the next spec save.
        </p>
      ) : (
        <div className="mt-4 space-y-2">
          {Object.entries(assessment.factors).map(([name, factor]) => (
            <div key={name} className="flex items-center gap-3 text-sm">
              <span className="w-44 shrink-0 text-slate-400">{FACTOR_LABELS[name] ?? name}</span>
              <div className="h-2 w-40 overflow-hidden rounded bg-slate-800">
                <div
                  className={`h-full ${factor.score >= 7 ? "bg-red-500" : factor.score >= 4 ? "bg-amber-500" : "bg-emerald-500"}`}
                  style={{ width: `${factor.score * 10}%` }}
                />
              </div>
              <span className="w-10 shrink-0 font-mono text-xs text-slate-400">
                {factor.score}/10
              </span>
              <span className="truncate text-xs text-slate-400" title={factor.rationale}>
                {factor.rationale}
              </span>
            </div>
          ))}
        </div>
      )}

      {assessment.notes && (
        <p className="mt-3 rounded-md bg-slate-950/60 px-3 py-2 text-sm text-slate-400">
          Reviewer notes: {assessment.notes}
        </p>
      )}
    </div>
  );
}

function DecideModal({
  assessment,
  onClose,
  onDone,
  onError,
}: {
  assessment: RiskAssessment;
  onClose: () => void;
  onDone: (approved: boolean) => void;
  onError: (msg: string) => void;
}) {
  const [outcome, setOutcome] = useState<"approve" | "request_changes" | "reject">("approve");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const notesRequired = outcome !== "approve";

  async function decide() {
    if (notesRequired && !notes.trim()) {
      onError("This outcome requires notes explaining the decision.");
      return;
    }
    setBusy(true);
    try {
      await api(`/v1/admin/risk-assessments/${assessment.id}/decide`, {
        method: "POST",
        body: JSON.stringify({ outcome, notes: notes.trim() || null }),
      });
      onDone(outcome === "approve");
    } catch (e) {
      onError((e as Error).message);
      onClose();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-xl border border-slate-700 bg-slate-900 p-6">
        <h2 className="text-lg font-medium">
          Review &ldquo;{assessment.project_name}&rdquo;
        </h2>
        <p className="mt-1 text-sm text-slate-400">
          Score {assessment.score}/100 ({assessment.level}). Decisions apply to the CURRENT spec
          content; any edit re-scores.
        </p>
        <fieldset className="mt-4 space-y-2 text-sm">
          {(
            [
              ["approve", "✅ Approve"],
              ["request_changes", "✏️ Request changes"],
              ["reject", "⛔ Reject"],
            ] as const
          ).map(([value, label]) => (
            <label
              key={value}
              className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 ${
                outcome === value ? "border-indigo-500 bg-indigo-500/10" : "border-slate-700"
              }`}
            >
              <input
                type="radio"
                name="admin-outcome"
                checked={outcome === value}
                onChange={() => setOutcome(value)}
              />
              {label}
            </label>
          ))}
        </fieldset>
        <label className="mt-4 block text-sm text-slate-400">
          Notes {notesRequired ? "(required)" : "(optional)"}
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={3}
            maxLength={2000}
            className="mt-1 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
          />
        </label>
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
          >
            Cancel
          </button>
          <button
            onClick={decide}
            disabled={busy || (notesRequired && !notes.trim())}
            className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            {busy ? "Submitting…" : "Submit decision"}
          </button>
        </div>
      </div>
    </div>
  );
}
