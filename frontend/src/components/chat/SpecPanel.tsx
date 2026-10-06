"use client";

import Link from "next/link";
import { useState } from "react";
import SpecMarkdown from "@/components/markdown/SpecMarkdown";
import type { DocType, FullSpecResponse, Generation } from "@/lib/types";
import { DOC_TYPES } from "@/lib/types";

const DOC_LABELS: Record<DocType, string> = {
  requirements: "requirements.md",
  design: "design.md",
  tasks: "tasks.md",
};

/** Full spec-set panel: doc tabs, generation progress, per-doc regenerate (S2-01). */
export default function SpecPanel({
  spec,
  generation,
  generating,
  onRegenerateDoc,
  onSave,
  sessionStatus,
  projectId,
}: {
  spec: FullSpecResponse | null;
  generation: Generation | null;
  generating: boolean;
  onRegenerateDoc: (docType: DocType) => void;
  onSave: () => void;
  sessionStatus?: string;
  projectId: string | null;
}) {
  const [activeTab, setActiveTab] = useState<DocType>("requirements");

  const docState = spec?.[activeTab];
  const latest = docState?.latest ?? null;
  const anyDoc = DOC_TYPES.some((t) => spec?.[t]?.latest);
  const generationDoc = generation?.docs.find((d) => d.type === activeTab);

  return (
    <aside className="hidden w-[38%] shrink-0 flex-col rounded-xl border border-slate-800 bg-slate-900/40 xl:flex">
      <div className="flex items-center justify-between border-b border-slate-800 px-3 py-2">
        <div className="flex items-center gap-1">
          {DOC_TYPES.map((docType) => {
            const state = generation?.docs.find((d) => d.type === docType)?.status;
            return (
              <button
                key={docType}
                onClick={() => setActiveTab(docType)}
                className={`rounded-md px-2.5 py-1 text-xs transition ${
                  activeTab === docType
                    ? "bg-slate-800 text-white"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {DOC_LABELS[docType]}
                {state === "generating" && (
                  <span className="ml-1 inline-block h-2 w-2 animate-pulse rounded-full bg-indigo-400 align-middle" />
                )}
                {state === "done" && <span className="ml-1 text-emerald-400">✓</span>}
                {state === "failed" && <span className="ml-1 text-red-400">✗</span>}
              </button>
            );
          })}
        </div>
        {sessionStatus === "saved" && (
          <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] uppercase text-emerald-400">
            saved
          </span>
        )}
      </div>

      {generation?.status === "running" && (
        <div className="border-b border-slate-800 bg-indigo-500/5 px-4 py-2 text-xs text-indigo-300">
          Generating the spec set — documents appear as they finish…
        </div>
      )}
      {generation?.status === "failed" && (
        <div className="border-b border-slate-800 bg-red-500/5 px-4 py-2 text-xs text-red-300">
          {generation.error ?? "Generation failed"}
        </div>
      )}

      <div className="flex items-center justify-between border-b border-slate-800 px-4 py-2 text-xs">
        {latest ? (
          <div className="flex items-center gap-2">
            <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">
              v{latest.version}
            </span>
            <span className="text-slate-400">
              {latest.origin === "generated" ? "generated" : latest.origin}
            </span>
          </div>
        ) : (
          <span className="text-slate-400">no versions yet</span>
        )}
        <div className="flex items-center gap-2">
          {latest && (
            <a
              href={`/api/backend/v1/chat/specs/${latest.id}/download`}
              className="rounded border border-slate-600 px-2 py-1 text-slate-300 hover:border-indigo-400 hover:text-indigo-300"
            >
              ⬇
            </a>
          )}
          {latest && (
            <button
              onClick={() => onRegenerateDoc(activeTab)}
              disabled={generating}
              title={`Regenerate ${DOC_LABELS[activeTab]} only`}
              className="rounded border border-slate-600 px-2 py-1 text-slate-300 hover:border-indigo-400 hover:text-indigo-300 disabled:opacity-40"
            >
              ↻
            </button>
          )}
          {projectId && anyDoc && (
            <Link
              href={`/projects/${projectId}?tab=spec`}
              className="rounded border border-slate-600 px-2 py-1 text-slate-300 hover:border-indigo-400 hover:text-indigo-300"
            >
              Open in editor
            </Link>
          )}
          {projectId && anyDoc && (
            // Chat authors the spec; the build starts on the workbench Build
            // tab (FSD S8-04). This is the handoff the surface was missing.
            <Link
              href={`/projects/${projectId}?tab=build`}
              data-testid="spec-start-build"
              className="rounded border border-indigo-500/50 bg-indigo-500/10 px-2 py-1 text-indigo-300 hover:bg-indigo-500/20"
            >
              Start a build →
            </Link>
          )}
          {anyDoc && sessionStatus !== "saved" && (
            // emerald-700: the 12px label needs ≥4.5:1 (AA); white on
            // emerald-600 is 3.69:1 — first flagged when the axe /chat scan
            // gained a rendered spec panel (13.5Y smoke).
            <button
              onClick={onSave}
              className="rounded bg-emerald-700 px-2 py-1 font-medium text-white hover:bg-emerald-600"
            >
              ✓ Approve
            </button>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {generationDoc?.status === "generating" ? (
          <div className="flex h-full flex-col items-center justify-center text-slate-400">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-slate-700 border-t-indigo-400" />
            <p className="mt-4 text-sm">Generating {DOC_LABELS[activeTab]}…</p>
          </div>
        ) : generationDoc?.status === "failed" && !latest ? (
          <div className="flex h-full flex-col items-center justify-center text-center">
            <p className="text-sm text-red-300">This document failed to generate.</p>
            <p className="mt-1 max-w-xs text-xs text-slate-400">{generationDoc.error}</p>
            <button
              onClick={() => onRegenerateDoc(activeTab)}
              className="mt-4 rounded-lg border border-slate-600 px-3 py-1.5 text-sm hover:border-indigo-400"
            >
              Retry
            </button>
          </div>
        ) : latest ? (
          <SpecMarkdown content={latest.content} />
        ) : (
          <div className="flex h-full flex-col items-center justify-center text-center text-slate-400">
            <p className="text-3xl">📄</p>
            <p className="mt-3 text-sm">Your {DOC_LABELS[activeTab]} will appear here.</p>
            <p className="mt-1 max-w-xs text-xs text-slate-400">
              Chat about your idea, then Generate spec to produce requirements, design,
              and an implementation plan.
            </p>
          </div>
        )}
      </div>
    </aside>
  );
}
