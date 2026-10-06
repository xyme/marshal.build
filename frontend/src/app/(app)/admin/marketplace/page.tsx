"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import { CATEGORY_ART } from "@/lib/marketplace-ui";
import type { SampleCard, SampleList, Submission } from "@/lib/types";

const STATUS_STYLES: Record<string, string> = {
  draft: "bg-slate-700/40 text-slate-300",
  published: "bg-emerald-500/15 text-emerald-400",
  archived: "bg-slate-800/80 text-slate-400",
  submitted: "bg-amber-500/15 text-amber-400",
  rejected: "bg-red-500/15 text-red-300",
  withdrawn: "bg-slate-800/80 text-slate-400",
};

/** Admin > Marketplace: curation list + submission review queue (S3-04, S7-08). */
function AdminMarketplaceInner() {
  const readonly = useAdminReadonly();
  const searchParams = useSearchParams();
  const [tab, setTab] = useState<"samples" | "submissions">(
    searchParams.get("tab") === "submissions" ? "submissions" : "samples"
  );
  const [status, setStatus] = useState("");
  const [data, setData] = useState<SampleList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingCount, setPendingCount] = useState(0);

  useEffect(() => {
    const params = new URLSearchParams({ page_size: "100" });
    if (status) params.set("status", status);
    api<SampleList>(`/v1/admin/marketplace/samples?${params}`)
      .then(setData)
      .catch((e) => setError(e.message));
  }, [status]);

  useEffect(() => {
    api<Submission[]>("/v1/admin/marketplace/submissions")
      .then((rows) => setPendingCount(rows.length))
      .catch(() => {});
  }, [tab]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Marketplace</h1>
          <p className="mt-1 text-sm text-slate-400">
            Curate the sample catalog and review community submissions.
          </p>
        </div>
        {tab === "samples" && (
          <div className="flex items-center gap-3">
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
            >
              <option value="">All statuses</option>
              <option value="draft">Draft</option>
              <option value="published">Published</option>
              <option value="archived">Archived</option>
              <option value="submitted">Submitted</option>
              <option value="rejected">Rejected</option>
            </select>
            {!readonly && (
              <Link
                href="/admin/marketplace/new"
                className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400"
              >
                + New Sample
              </Link>
            )}
          </div>
        )}
      </div>

      <div className="flex gap-1 border-b border-slate-800">
        <button
          onClick={() => setTab("samples")}
          className={`rounded-t-lg px-4 py-2 text-sm ${
            tab === "samples"
              ? "border border-b-0 border-slate-800 bg-slate-900/40 text-white"
              : "text-slate-400 hover:text-slate-200"
          }`}
        >
          Samples
        </button>
        <button
          onClick={() => setTab("submissions")}
          className={`rounded-t-lg px-4 py-2 text-sm ${
            tab === "submissions"
              ? "border border-b-0 border-slate-800 bg-slate-900/40 text-white"
              : "text-slate-400 hover:text-slate-200"
          }`}
        >
          Submissions
          {pendingCount > 0 && (
            <span className="ml-1.5 rounded-full bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-amber-300">
              {pendingCount}
            </span>
          )}
        </button>
      </div>

      {tab === "submissions" ? (
        <SubmissionsTab onDecided={() => setPendingCount((n) => Math.max(0, n - 1))} />
      ) : (
        <>
      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {data === null ? (
        <p className="text-slate-400">Loading…</p>
      ) : data.items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 p-10 text-center text-slate-400">
          No samples{status ? ` with status "${status}"` : ""}. Create one, or run the seed script.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/80 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-4 py-3">Sample</th>
                <th className="px-4 py-3">Category</th>
                <th className="px-4 py-3">Complexity</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Forks</th>
                <th className="px-4 py-3">Views</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {data.items.map((s: SampleCard) => (
                <tr key={s.id} className="bg-slate-900/40 hover:bg-slate-900/70">
                  <td className="px-4 py-3">
                    <Link
                      href={`/admin/marketplace/${s.id}`}
                      className="font-medium text-indigo-300 hover:text-indigo-200"
                    >
                      <span className="mr-2">{CATEGORY_ART[s.category] ?? "🧩"}</span>
                      {s.title}
                    </Link>
                  </td>
                  <td className="px-4 py-3 capitalize text-slate-400">
                    {s.category.replace(/_/g, " ")}
                  </td>
                  <td className="px-4 py-3 capitalize text-slate-400">{s.complexity}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                        STATUS_STYLES[s.status ?? ""] ?? "bg-slate-700/40 text-slate-400"
                      }`}
                    >
                      {s.status ?? "…"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-400">{s.fork_count}</td>
                  <td className="px-4 py-3 text-slate-400">{s.view_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
        </>
      )}
    </div>
  );
}

function relWait(iso: string | null): string {
  if (!iso) return "—";
  const hours = (Date.now() - new Date(iso).getTime()) / 3_600_000;
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m`;
  return hours < 48 ? `${Math.round(hours)}h` : `${Math.round(hours / 24)}d`;
}

function SubmissionsTab({ onDecided }: { onDecided: () => void }) {
  const readonly = useAdminReadonly();
  const [view, setView] = useState<"submitted" | "all">("submitted");
  const [items, setItems] = useState<Submission[] | null>(null);
  const [rejecting, setRejecting] = useState<Submission | null>(null);
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<Submission[]>(`/v1/admin/marketplace/submissions?status=${view}`)
      .then(setItems)
      .catch(() => setItems([]));
  }, [view]);

  useEffect(() => {
    load();
  }, [load]);

  async function approve(s: Submission) {
    setBusy(true);
    try {
      await api(`/v1/admin/marketplace/submissions/${s.id}/approve`, { method: "POST" });
      toast.success("Approved — it's now a draft in the curation list.");
      onDecided();
      load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    if (!rejecting) return;
    setBusy(true);
    try {
      await api(`/v1/admin/marketplace/submissions/${rejecting.id}/reject`, {
        method: "POST",
        body: JSON.stringify({ feedback: feedback.trim() }),
      });
      toast.success("Rejected — the author has been notified with your feedback.");
      setRejecting(null);
      setFeedback("");
      onDecided();
      load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-slate-400">
          Power users submit projects here (US-018). Approve into curation, or reject with
          feedback — the author can revise and resubmit.
        </p>
        <select
          value={view}
          onChange={(e) => setView(e.target.value as "submitted" | "all")}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm"
        >
          <option value="submitted">Pending</option>
          <option value="all">All history</option>
        </select>
      </div>

      {items === null ? (
        <p className="text-slate-400">Loading…</p>
      ) : items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 p-10 text-center text-slate-400">
          {view === "submitted" ? "No submissions waiting for review." : "No submissions yet."}
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((s) => (
            <div
              key={s.id}
              className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-4"
            >
              <div className="flex flex-wrap items-center gap-3">
                <p className="font-medium">{s.title}</p>
                <span
                  className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                    STATUS_STYLES[s.status] ?? "bg-slate-700/40 text-slate-400"
                  }`}
                >
                  {s.status}
                </span>
                {s.status === "submitted" && (
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] ${
                      (Date.now() - new Date(s.submitted_at ?? 0).getTime()) / 3_600_000 > 48
                        ? "bg-red-500/15 text-red-300"
                        : "bg-slate-700/40 text-slate-400"
                    }`}
                  >
                    waiting {relWait(s.submitted_at)}
                  </span>
                )}
                {s.resubmission_of && (
                  <span className="rounded bg-indigo-500/15 px-1.5 py-0.5 text-[10px] text-indigo-300">
                    resubmission
                  </span>
                )}
                <span className="ml-auto text-xs text-slate-400">
                  by {s.author_name ?? s.author_email}
                  {s.project_name && <> · from &ldquo;{s.project_name}&rdquo;</>}
                </span>
              </div>
              <p className="mt-1 text-sm text-slate-400">{s.description}</p>
              {s.review_feedback && s.status === "rejected" && (
                <p className="mt-2 text-xs text-slate-400">Feedback sent: {s.review_feedback}</p>
              )}
              <div className="mt-3 flex items-center gap-2">
                <Link
                  href={`/admin/marketplace/${s.id}`}
                  className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs hover:border-slate-500"
                >
                  Review snapshot
                </Link>
                {s.status === "submitted" && (
                  <>
                    <button
                      onClick={() => approve(s)}
                      disabled={busy || readonly}
                      className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
                    >
                      Approve → curation
                    </button>
                    <button
                      onClick={() => setRejecting(s)}
                      disabled={busy || readonly}
                      className="rounded-lg bg-red-600/80 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-40"
                    >
                      Reject…
                    </button>
                  </>
                )}
                {s.status === "draft" && (
                  <span className="text-xs text-slate-400">approved — polish &amp; publish from Samples</span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {rejecting && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md rounded-xl border border-slate-700 bg-slate-900 p-6">
            <h2 className="text-lg font-medium">Reject &ldquo;{rejecting.title}&rdquo;</h2>
            <p className="mt-1 text-xs text-slate-400">
              Feedback is required and goes to the author verbatim. They can revise and resubmit.
            </p>
            <textarea
              autoFocus
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
              rows={4}
              placeholder="What needs to change before this can be listed?"
              className="mt-4 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-red-500"
            />
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => {
                  setRejecting(null);
                  setFeedback("");
                }}
                className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                onClick={reject}
                disabled={busy || feedback.trim().length < 3}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-40"
              >
                Reject with feedback
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function AdminMarketplacePage() {
  return (
    <Suspense fallback={<p className="text-slate-400">Loading…</p>}>
      <AdminMarketplaceInner />
    </Suspense>
  );
}
