"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useEffect, useState } from "react";
import SpecMarkdown from "@/components/markdown/SpecMarkdown";
import { api } from "@/lib/api";
import {
  CATEGORY_ART,
  COMPLEXITY_STYLE,
  modelLabel,
  stashForkWarnings,
} from "@/lib/marketplace-ui";
import type { SampleDetail } from "@/lib/types";

const DOC_TABS = [
  { key: "requirements_md", label: "requirements.md" },
  { key: "design_md", label: "design.md" },
  { key: "tasks_md", label: "tasks.md" },
] as const;

function includedSummary(sample: SampleDetail): string[] {
  const lines: string[] = [];
  const req = sample.spec_snapshot.requirements_md;
  const design = sample.spec_snapshot.design_md;
  const tasks = sample.spec_snapshot.tasks_md;
  if (req) {
    const stories = (req.match(/^- US-\d+/gm) ?? []).length;
    lines.push(`requirements.md${stories ? ` (${stories} user stories)` : ""}`);
  }
  if (design) {
    lines.push(`design.md${/```mermaid/.test(design) ? " (architecture diagram)" : ""}`);
  }
  if (tasks) {
    const count = (tasks.match(/^- \[ \]/gm) ?? []).length;
    lines.push(`tasks.md${count ? ` (${count} tasks)` : ""}`);
  }
  return lines;
}

export default function SampleDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const [sample, setSample] = useState<SampleDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [specOpen, setSpecOpen] = useState(false);
  const [specTab, setSpecTab] = useState<(typeof DOC_TABS)[number]["key"]>("requirements_md");
  const [forkOpen, setForkOpen] = useState(false);
  const [forkName, setForkName] = useState("");
  const [forking, setForking] = useState(false);
  const [forkError, setForkError] = useState<string | null>(null);

  useEffect(() => {
    api<SampleDetail>(`/v1/marketplace/samples/${id}`)
      .then((s) => {
        setSample(s);
        setForkName(s.title);
      })
      .catch((e) => setError(e.message));
  }, [id]);

  async function doFork(e: React.FormEvent) {
    e.preventDefault();
    if (!sample || !forkName.trim()) return;
    setForking(true);
    setForkError(null);
    try {
      const result = await api<{ project_id: string; warnings: string[] }>(
        `/v1/marketplace/samples/${sample.id}/fork`,
        { method: "POST", body: JSON.stringify({ name: forkName.trim() }) }
      );
      stashForkWarnings(result.project_id, result.warnings, sample.title);
      router.push(`/projects/${result.project_id}?tab=spec`);
    } catch (err) {
      setForkError((err as Error).message);
      setForking(false);
    }
  }

  if (error) {
    return (
      <div className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-red-300">
        {error} — <Link href="/marketplace" className="underline">back to marketplace</Link>
      </div>
    );
  }
  if (!sample) return <p className="text-slate-400">Loading…</p>;

  const included = includedSummary(sample);
  const meta = sample.metadata_extra;

  return (
    <div className="space-y-6">
      <Link href="/marketplace" className="text-sm text-slate-400 hover:text-slate-300">
        ← Back to Marketplace
      </Link>

      <div className="grid gap-6 lg:grid-cols-[1fr_290px]">
        <div className="space-y-5">
          <div className="flex items-start gap-4">
            <div className="text-4xl">{CATEGORY_ART[sample.category] ?? "🧩"}</div>
            <div>
              <h1 className="text-2xl font-semibold tracking-tight">{sample.title}</h1>
              <p className="mt-1 text-slate-400">{sample.description}</p>
            </div>
          </div>

          {sample.template_deprecated && (
            <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
              This sample was built with a deprecated template. You can still fork it —
              consider migrating to an active template afterwards.
            </p>
          )}

          {sample.long_description && (
            <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
              <SpecMarkdown content={sample.long_description} />
            </div>
          )}

          {included.length > 0 && (
            <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
              <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
                What&apos;s included
              </h2>
              <ul className="mt-3 space-y-1.5 text-sm">
                {included.map((line) => (
                  <li key={line} className="flex items-center gap-2 text-slate-300">
                    <span className="text-emerald-400">☑</span> {line}
                  </li>
                ))}
              </ul>
              <div className="mt-4 flex gap-2">
                <button
                  onClick={() => setSpecOpen(true)}
                  className="rounded-lg border border-slate-700 px-4 py-2 text-sm hover:border-slate-500"
                >
                  View Spec
                </button>
                <button
                  onClick={() => setForkOpen(true)}
                  className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400"
                >
                  Fork to My Project
                </button>
              </div>
            </div>
          )}
        </div>

        <aside className="space-y-4">
          <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5 text-sm">
            <h2 className="text-xs font-medium uppercase tracking-wide text-slate-400">Metadata</h2>
            <dl className="mt-3 space-y-2">
              <MetaRow label="Category">
                <span className="capitalize">{sample.category.replace(/_/g, " ")}</span>
              </MetaRow>
              <MetaRow label="Complexity">
                <span className={`rounded px-1.5 py-0.5 text-[11px] capitalize ${COMPLEXITY_STYLE[sample.complexity]}`}>
                  {sample.complexity}
                </span>
              </MetaRow>
              <MetaRow label="Models">
                {sample.models_used.map(modelLabel).join(", ") || "—"}
              </MetaRow>
              <MetaRow label="Author">
                {sample.contributed_by
                  ? `${sample.contributed_by} (community contribution)`
                  : sample.author_name ?? "marshal"}
              </MetaRow>
              <MetaRow label="Forks">{sample.fork_count}</MetaRow>
              <MetaRow label="Published">
                {sample.published_at ? new Date(sample.published_at).toLocaleDateString() : "—"}
              </MetaRow>
              {sample.template_name && (
                <MetaRow label="Template">
                  {sample.template_name}
                  {sample.template_deprecated && (
                    <span className="ml-1 text-amber-400">(deprecated)</span>
                  )}
                </MetaRow>
              )}
            </dl>
          </div>

          {(meta.est_build_usd != null || meta.est_run_usd_month != null) && (
            <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5 text-sm">
              <h2 className="text-xs font-medium uppercase tracking-wide text-slate-400">
                Estimated cost
              </h2>
              <dl className="mt-3 space-y-2">
                {meta.est_build_usd != null && (
                  <MetaRow label="Build">~${meta.est_build_usd.toFixed(2)}</MetaRow>
                )}
                {meta.est_run_usd_month != null && (
                  <MetaRow label="Run">~${meta.est_run_usd_month}/mo</MetaRow>
                )}
              </dl>
            </div>
          )}

          {meta.tech_stack && meta.tech_stack.length > 0 && (
            <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5 text-sm">
              <h2 className="text-xs font-medium uppercase tracking-wide text-slate-400">
                Tech stack
              </h2>
              <ul className="mt-3 space-y-1 text-slate-300">
                {meta.tech_stack.map((t) => (
                  <li key={t}>• {t}</li>
                ))}
              </ul>
            </div>
          )}

          <button
            onClick={() => setForkOpen(true)}
            className="w-full rounded-lg bg-indigo-500 px-4 py-2.5 text-sm font-medium text-white hover:bg-indigo-400"
          >
            Fork to Project
          </button>
        </aside>
      </div>

      {specOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="flex h-[85vh] w-full max-w-4xl flex-col rounded-xl border border-slate-700 bg-slate-900">
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
              <div className="flex gap-1">
                {DOC_TABS.map((t) => (
                  <button
                    key={t.key}
                    onClick={() => setSpecTab(t.key)}
                    className={`rounded-md px-3 py-1.5 font-mono text-xs ${
                      specTab === t.key
                        ? "bg-slate-800 text-white"
                        : "text-slate-400 hover:text-slate-200"
                    }`}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              <button
                onClick={() => setSpecOpen(false)}
                aria-label="Close spec viewer"
                className="rounded-md px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-white"
              >
                ✕
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-6">
              <SpecMarkdown content={sample.spec_snapshot[specTab] ?? "*Not included in this sample.*"} />
            </div>
          </div>
        </div>
      )}

      {forkOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <form
            onSubmit={doFork}
            className="w-full max-w-md rounded-xl border border-slate-700 bg-slate-900 p-6"
          >
            <h2 className="text-lg font-medium">Fork &ldquo;{sample.title}&rdquo;</h2>
            <p className="mt-1 text-sm text-slate-400">
              Creates an independent project pre-populated with this sample&apos;s full spec.
            </p>
            <label className="mt-4 block text-sm text-slate-400">
              Project name
              <input
                autoFocus
                value={forkName}
                onChange={(e) => setForkName(e.target.value)}
                maxLength={120}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-indigo-500"
              />
            </label>
            {sample.template_name && (
              <p className="mt-3 text-xs text-slate-400">
                Template: {sample.template_name}
                {sample.template_deprecated && (
                  <span className="text-amber-400"> — deprecated; the project will show a migration hint</span>
                )}
              </p>
            )}
            {forkError && (
              <p className="mt-3 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {forkError}
              </p>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setForkOpen(false)}
                className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!forkName.trim() || forking}
                className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
              >
                {forking ? "Creating…" : "Fork"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-slate-400">{label}</dt>
      <dd className="text-right text-slate-300">{children}</dd>
    </div>
  );
}
