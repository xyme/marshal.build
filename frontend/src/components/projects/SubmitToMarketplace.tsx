"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import type { Project, Submission } from "@/lib/types";

const CATEGORIES = [
  "chatbot",
  "document_processing",
  "data_analysis",
  "content_generation",
  "workflow_automation",
  "custom",
];

/** Submit a project as a marketplace sample (US-018, submissions spec R1). */
export function useMySubmissionFor(projectId: string) {
  const [submission, setSubmission] = useState<Submission | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    api<Submission[]>("/v1/marketplace/my-submissions")
      .then((all) => {
        const open = all.find(
          (s) => s.source_project_id === projectId && s.status === "submitted"
        );
        setSubmission(open ?? null);
      })
      .catch(() => {});
  }, [projectId, reload]);
  return { submission, refresh: () => setReload((n) => n + 1) };
}

export default function SubmitToMarketplaceModal({
  project,
  onClose,
  onSubmitted,
}: {
  project: Project;
  onClose: () => void;
  onSubmitted: () => void;
}) {
  const [title, setTitle] = useState(project.name.slice(0, 60));
  const [summary, setSummary] = useState(project.description ?? "");
  const [category, setCategory] = useState("custom");
  const [keywords, setKeywords] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/v1/projects/${project.id}/submit-to-marketplace`, {
        method: "POST",
        body: JSON.stringify({
          title: title.trim(),
          summary: summary.trim(),
          category,
          keywords: keywords
            .split(",")
            .map((k) => k.trim())
            .filter(Boolean),
        }),
      });
      toast.success("Submitted — an admin will review it. Track status in your profile.");
      onSubmitted();
      onClose();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-md rounded-xl border border-slate-700 bg-slate-900 p-6"
      >
        <h2 className="text-lg font-medium">Submit to marketplace</h2>
        <p className="mt-1 text-xs text-slate-400">
          A snapshot of the current spec set is sent for admin review. Later edits to the
          project won&apos;t affect the submission. You&apos;ll be credited as the contributor.
        </p>
        <label className="mt-4 block text-sm text-slate-400">
          Title
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={60}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
          />
        </label>
        <label className="mt-3 block text-sm text-slate-400">
          Summary
          <textarea
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            rows={3}
            maxLength={500}
            placeholder="What does this app do, and for whom?"
            className="mt-1 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
          />
        </label>
        <label className="mt-3 block text-sm text-slate-400">
          Category
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <label className="mt-3 block text-sm text-slate-400">
          Keywords (comma-separated)
          <input
            value={keywords}
            onChange={(e) => setKeywords(e.target.value)}
            placeholder="invoices, finance"
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
          />
        </label>
        {error && <p className="mt-3 text-xs text-red-300">{error}</p>}
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || title.trim().length < 3 || summary.trim().length < 10}
            className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            {busy ? "Submitting…" : "Submit for review"}
          </button>
        </div>
      </form>
    </div>
  );
}
