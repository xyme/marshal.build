"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { EmptyState } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { RISK_STYLE } from "@/lib/governance-ui";

interface ReviewItem {
  id: string;
  project_id: string;
  project_name: string | null;
  owner_email: string | null;
  score: number | null;
  level: string | null;
  factors: Record<string, { score: number; rationale: string }>;
  decision: string | null;
  assigned_group: string | null;
  escalated_to: string | null;
  waiting_hours: number | null;
  resubmission_of: string | null;
  owner_comment: string | null;
}

interface ReviewsResponse {
  groups: string[];
  is_reviewer: boolean;
  items: ReviewItem[];
}

const GROUP_LABELS: Record<string, string> = {
  managers: "Managers",
  governance_board: "AI Governance Board",
  admins: "Platform Admins",
};

const FACTOR_LABELS: Record<string, string> = {
  data_sensitivity: "Data sensitivity",
  model_capability: "Model capability",
  user_facing: "User-facing exposure",
  deployment_scope: "Deployment scope",
  request_volume: "Request volume",
};

/** Reviewer queue (risk-review-workflow spec R2.2) — reviewers need not be admins. */
export default function ReviewsPage() {
  const [data, setData] = useState<ReviewsResponse | null>(null);
  const [deciding, setDeciding] = useState<ReviewItem | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api<ReviewsResponse>("/v1/reviews"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (error) {
    return <p className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-red-300">{error}</p>;
  }
  if (!data) return <p className="text-slate-400">Loading…</p>;

  if (!data.is_reviewer) {
    return (
      <EmptyState
        icon="🛡️"
        title="You're not a reviewer"
        body="Risk reviews are handled by the Managers and AI Governance Board groups. An admin can add you under Admin → Risk → Reviewers."
      />
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Reviews</h1>
        <p className="mt-1 text-sm text-slate-400">
          Assessments waiting on {data.groups.length ? data.groups.map((g) => GROUP_LABELS[g] ?? g).join(", ") : "you (admin)"}.
          Approval unblocks deployment for the current spec content.
        </p>
      </div>

      {data.items.length === 0 ? (
        <EmptyState icon="✅" title="Queue clear" body="Nothing waiting on your review." />
      ) : (
        <div className="space-y-4">
          {data.items.map((item) => (
            <div key={item.id} className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
              <div className="flex flex-wrap items-center gap-3">
                <span className={`rounded px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${RISK_STYLE[item.level ?? "low"]}`}>
                  {item.level}
                </span>
                <h2 className="font-medium">{item.project_name}</h2>
                <span className="text-sm text-slate-400">
                  {item.owner_email} · {item.score}/100
                </span>
                <SlaChip item={item} />
                {item.resubmission_of && (
                  <span className="rounded bg-indigo-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300">
                    resubmission
                  </span>
                )}
                <span className="ml-auto flex gap-2">
                  <Link
                    href={`/projects/${item.project_id}?tab=spec`}
                    className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-slate-500"
                  >
                    View spec
                  </Link>
                  <button
                    onClick={() => setDeciding(item)}
                    className="rounded-lg bg-indigo-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-400"
                  >
                    Review…
                  </button>
                </span>
              </div>
              <div className="mt-4 space-y-1.5">
                {Object.entries(item.factors).map(([name, factor]) => (
                  <div key={name} className="flex items-center gap-3 text-sm">
                    <span className="w-44 shrink-0 text-slate-400">{FACTOR_LABELS[name] ?? name}</span>
                    <div className="h-2 w-36 overflow-hidden rounded bg-slate-800">
                      <div
                        className={`h-full ${factor.score >= 7 ? "bg-red-500" : factor.score >= 4 ? "bg-amber-500" : "bg-emerald-500"}`}
                        style={{ width: `${factor.score * 10}%` }}
                      />
                    </div>
                    <span className="w-10 font-mono text-xs text-slate-400">{factor.score}/10</span>
                    <span className="truncate text-xs text-slate-400" title={factor.rationale}>
                      {factor.rationale}
                    </span>
                  </div>
                ))}
              </div>
              {item.owner_comment && (
                <p className="mt-3 rounded-md bg-slate-950/60 px-3 py-2 text-sm text-slate-400">
                  Owner comment: {item.owner_comment}
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {deciding && (
        <DecideModal
          item={deciding}
          onClose={() => setDeciding(null)}
          onDone={(outcome) => {
            setDeciding(null);
            toast.success(
              outcome === "approve"
                ? "Approved — the owner can deploy."
                : outcome === "reject"
                  ? "Rejected with notes."
                  : "Changes requested — the owner has been notified."
            );
            load();
          }}
          onError={setError}
        />
      )}
    </div>
  );
}

function SlaChip({ item }: { item: ReviewItem }) {
  if (item.waiting_hours == null) return null;
  const hours = item.waiting_hours;
  const tone =
    hours > 48 ? "bg-red-500/15 text-red-400" : hours > 24 ? "bg-amber-500/15 text-amber-400" : "bg-slate-800 text-slate-400";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${tone}`}>
      waiting {hours < 1 ? "<1h" : `${Math.round(hours)}h`}
      {item.escalated_to && ` · escalated to ${GROUP_LABELS[item.escalated_to] ?? item.escalated_to}`}
    </span>
  );
}

function DecideModal({
  item,
  onClose,
  onDone,
  onError,
}: {
  item: ReviewItem;
  onClose: () => void;
  onDone: (outcome: string) => void;
  onError: (message: string) => void;
}) {
  const [outcome, setOutcome] = useState<"approve" | "request_changes" | "reject">("approve");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const notesRequired = outcome !== "approve";

  async function submit() {
    if (notesRequired && !notes.trim()) return;
    setBusy(true);
    try {
      await api(`/v1/risk-assessments/${item.id}/decide`, {
        method: "POST",
        body: JSON.stringify({ outcome, notes: notes.trim() || null }),
      });
      onDone(outcome);
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
        <h2 className="text-lg font-medium">Review &ldquo;{item.project_name}&rdquo;</h2>
        <p className="mt-1 text-sm text-slate-400">
          {item.score}/100 ({item.level}). Any spec edit re-scores and returns here.
        </p>
        <fieldset className="mt-4 space-y-2 text-sm">
          {(
            [
              ["approve", "✅ Approve", "Deployment unblocked for this content"],
              ["request_changes", "✏️ Request changes", "Owner revises; resubmission returns to the queue"],
              ["reject", "⛔ Reject", "Deployment blocked until content changes"],
            ] as const
          ).map(([value, label, hint]) => (
            <label
              key={value}
              className={`flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2 ${
                outcome === value ? "border-indigo-500 bg-indigo-500/10" : "border-slate-700"
              }`}
            >
              <input
                type="radio"
                name="outcome"
                className="mt-0.5"
                checked={outcome === value}
                onChange={() => setOutcome(value)}
              />
              <span>
                <span className="block">{label}</span>
                <span className="block text-xs text-slate-400">{hint}</span>
              </span>
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
          <button onClick={onClose} className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200">
            Cancel
          </button>
          <button
            onClick={submit}
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
