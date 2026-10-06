"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import SpecMarkdown from "@/components/markdown/SpecMarkdown";
import { api } from "@/lib/api";
import type {
  DocType,
  FullSpecResponse,
  Generation,
  PromptContextResp,
} from "@/lib/types";
import { DOC_TYPES } from "@/lib/types";

/** Studio artifact column (S18-03): Spec | Prompt context | Export.
 *
 * Real tablist semantics with arrow-key movement (R5.2). Spec tab mirrors
 * SpecPanel's behavior through the same useSessionSpec data; Prompt context
 * renders the read-only endpoint; Export relocates existing S4-06/S8 actions.
 */

const DOC_LABELS: Record<DocType, string> = {
  requirements: "requirements.md",
  design: "design.md",
  tasks: "tasks.md",
};

const TABS = ["Spec", "Prompt context", "Export"] as const;
type Tab = (typeof TABS)[number];

export default function ArtifactColumn({
  sessionId,
  projectId,
  spec,
  generation,
  generating,
  onRegenerateDoc,
  onSave,
  sessionStatus,
  staleness,
  onRefresh,
}: {
  sessionId: string;
  projectId: string | null;
  spec: FullSpecResponse | null;
  generation: Generation | null;
  generating: boolean;
  onRegenerateDoc: (docType: DocType) => void;
  onSave: () => void;
  sessionStatus?: string;
  staleness: { specAt: string | null; messagesSince: number };
  onRefresh: () => void;
}) {
  const [tab, setTab] = useState<Tab>("Spec");
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  // Arrow-key movement across the tablist (R5.2)
  function onTablistKeyDown(e: React.KeyboardEvent) {
    const idx = TABS.indexOf(tab);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const next =
        e.key === "ArrowRight"
          ? (idx + 1) % TABS.length
          : (idx - 1 + TABS.length) % TABS.length;
      setTab(TABS[next]);
      tabRefs.current[next]?.focus();
    }
  }

  return (
    <section
      aria-label="Session artifacts"
      className="flex min-h-0 flex-1 flex-col rounded-xl border border-slate-800 bg-slate-900/40"
    >
      <div
        role="tablist"
        aria-label="Artifact views"
        onKeyDown={onTablistKeyDown}
        className="flex items-center gap-1 border-b border-slate-800 px-3 py-2"
      >
        {TABS.map((t, i) => (
          <button
            key={t}
            ref={(el) => {
              tabRefs.current[i] = el;
            }}
            role="tab"
            id={`studio-tab-${i}`}
            aria-selected={tab === t}
            aria-controls={`studio-panel-${i}`}
            tabIndex={tab === t ? 0 : -1}
            onClick={() => setTab(t)}
            className={`rounded-md px-2.5 py-1 text-xs transition ${
              tab === t ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {t}
          </button>
        ))}
        {sessionStatus === "saved" && (
          <span className="ml-auto rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] uppercase text-emerald-400">
            saved
          </span>
        )}
      </div>

      <div
        role="tabpanel"
        id={`studio-panel-${TABS.indexOf(tab)}`}
        aria-labelledby={`studio-tab-${TABS.indexOf(tab)}`}
        className="flex min-h-0 flex-1 flex-col"
      >
        {tab === "Spec" && (
          <SpecTab
            spec={spec}
            generation={generation}
            generating={generating}
            onRegenerateDoc={onRegenerateDoc}
            onSave={onSave}
            sessionStatus={sessionStatus}
            projectId={projectId}
            staleness={staleness}
            onRefresh={onRefresh}
          />
        )}
        {tab === "Prompt context" && <PromptContextTab sessionId={sessionId} />}
        {tab === "Export" && <ExportTab projectId={projectId} spec={spec} />}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ Spec tab

function SpecTab({
  spec,
  generation,
  generating,
  onRegenerateDoc,
  onSave,
  sessionStatus,
  projectId,
  staleness,
  onRefresh,
}: {
  spec: FullSpecResponse | null;
  generation: Generation | null;
  generating: boolean;
  onRegenerateDoc: (docType: DocType) => void;
  onSave: () => void;
  sessionStatus?: string;
  projectId: string | null;
  staleness: { specAt: string | null; messagesSince: number };
  onRefresh: () => void;
}) {
  const [doc, setDoc] = useState<DocType>("requirements");
  const docState = spec?.[doc];
  const latest = docState?.latest ?? null;
  const anyDoc = DOC_TYPES.some((t) => spec?.[t]?.latest);
  const generationDoc = generation?.docs.find((d) => d.type === doc);

  const specTime = staleness.specAt
    ? new Date(staleness.specAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : null;

  return (
    <>
      <div className="flex items-center justify-between border-b border-slate-800 px-3 py-1.5">
        <div className="flex items-center gap-1">
          {DOC_TYPES.map((docType) => {
            const state = generation?.docs.find((d) => d.type === docType)?.status;
            return (
              <button
                key={docType}
                onClick={() => setDoc(docType)}
                className={`rounded-md px-2 py-1 text-[11px] transition ${
                  doc === docType ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {DOC_LABELS[docType]}
                {/* No opacity animation on text (R5.3) — colored dot only */}
                {state === "generating" && (
                  <span className="ml-1 inline-block h-2 w-2 animate-pulse rounded-full bg-indigo-400 align-middle" />
                )}
                {state === "done" && <span className="ml-1 text-emerald-400">✓</span>}
                {state === "failed" && <span className="ml-1 text-red-400">✗</span>}
              </button>
            );
          })}
        </div>
        <div className="flex items-center gap-2">
          {latest && (
            <button
              onClick={() => onRegenerateDoc(doc)}
              disabled={generating}
              title={`Regenerate ${DOC_LABELS[doc]} only`}
              className="rounded border border-slate-600 px-2 py-0.5 text-xs text-slate-300 hover:border-indigo-400 hover:text-indigo-300 disabled:opacity-40"
            >
              ↻
            </button>
          )}
          {projectId && anyDoc && (
            <Link
              href={`/projects/${projectId}?tab=spec`}
              className="rounded border border-slate-600 px-2 py-0.5 text-xs text-slate-300 hover:border-indigo-400 hover:text-indigo-300"
            >
              Open in editor
            </Link>
          )}
          {projectId && anyDoc && (
            // Same handoff as SpecPanel: the build starts on the workbench
            // Build tab (FSD S8-04), not in the authoring surfaces.
            <Link
              href={`/projects/${projectId}?tab=build`}
              className="rounded border border-indigo-500/50 bg-indigo-500/10 px-2 py-0.5 text-xs text-indigo-300 hover:bg-indigo-500/20"
            >
              Start a build →
            </Link>
          )}
          {anyDoc && sessionStatus !== "saved" && (
            // emerald-700 for AA at 12px — same defect class as SpecPanel's
            // approve button (13.5Y).
            <button
              onClick={onSave}
              className="rounded bg-emerald-700 px-2 py-0.5 text-xs font-medium text-white hover:bg-emerald-600"
            >
              ✓ Approve
            </button>
          )}
        </div>
      </div>

      {/* Staleness honesty (R2.3): checkpoint sync, not per-turn sync */}
      {specTime && (
        <div className="flex items-center justify-between border-b border-slate-800 bg-slate-950/40 px-3 py-1 text-[11px] text-slate-400">
          <span>
            spec generated {specTime}
            {staleness.messagesSince > 0 && (
              <span className="text-amber-300">
                {" "}
                · {staleness.messagesSince} message{staleness.messagesSince === 1 ? "" : "s"} since
              </span>
            )}
          </span>
          <button onClick={onRefresh} className="text-slate-400 underline hover:text-slate-200">
            Refresh
          </button>
        </div>
      )}

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

      <div className="flex-1 overflow-y-auto p-4">
        {generationDoc?.status === "generating" ? (
          <div className="flex h-full flex-col items-center justify-center text-slate-400">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-slate-700 border-t-indigo-400" />
            <p className="mt-4 text-sm">Generating {DOC_LABELS[doc]}…</p>
          </div>
        ) : generationDoc?.status === "failed" && !latest ? (
          <div className="flex h-full flex-col items-center justify-center text-center">
            <p className="text-sm text-red-300">This document failed to generate.</p>
            <p className="mt-1 max-w-xs text-xs text-slate-400">{generationDoc.error}</p>
            <button
              onClick={() => onRegenerateDoc(doc)}
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
            <p className="mt-3 text-sm">Your {DOC_LABELS[doc]} will appear here.</p>
            <p className="mt-1 max-w-xs text-xs text-slate-400">
              Chat on the left, then Generate spec — documents update at every checkpoint.
            </p>
          </div>
        )}
      </div>
    </>
  );
}

// --------------------------------------------------------- Prompt context tab

function PromptContextTab({ sessionId }: { sessionId: string }) {
  const [ctx, setCtx] = useState<PromptContextResp | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api<PromptContextResp>(`/v1/chat/sessions/${sessionId}/prompt-context`)
      .then((c) => {
        setCtx(c);
        setError(null);
      })
      .catch((e) => setError((e as Error).message));
  }, [sessionId]);

  useEffect(() => {
    load();
  }, [load]);

  if (error) {
    return <p className="m-4 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</p>;
  }
  if (!ctx) return <p className="p-4 text-sm text-slate-400">Loading…</p>;

  return (
    <div className="flex-1 space-y-4 overflow-y-auto p-4">
      <p className="text-xs text-slate-400">
        What the platform sends with your <strong className="text-slate-300">next</strong> message.
        Read-only — this view never calls a model.
      </p>
      <div className="rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-xs">
        <span className="text-slate-400">Model: </span>
        <code className="text-slate-300">{ctx.model_id}</code>
        {ctx.model_id.startsWith("ext/") && (
          <span
            className="ml-2 rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-sky-300"
            title="Custom endpoint hosted outside Bedrock — prompts leave AWS"
          >
            External
          </span>
        )}
      </div>
      {ctx.segments.map((segment) => (
        <section key={segment.label}>
          <h3 className="text-xs font-medium uppercase tracking-wide text-slate-400">
            {segment.label}
          </h3>
          <pre className="mt-1 whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs leading-relaxed text-slate-300">
            {segment.content}
          </pre>
        </section>
      ))}
      <button onClick={load} className="text-xs text-slate-400 underline hover:text-slate-200">
        Refresh
      </button>
    </div>
  );
}

// ------------------------------------------------------------------ Export tab

interface BuildItem {
  id: string;
  status: string;
  artifact_profile?: string;
  created_at: string;
}

function ExportTab({ projectId, spec }: { projectId: string | null; spec: FullSpecResponse | null }) {
  const [bundleBuild, setBundleBuild] = useState<BuildItem | null>(null);

  useEffect(() => {
    if (!projectId) return;
    api<{ items: BuildItem[] }>(`/v1/projects/${projectId}/builds`)
      .then(({ items }) =>
        setBundleBuild(
          items.find((b) => b.status === "READY" && b.artifact_profile === "cdk-app") ?? null
        )
      )
      .catch(() => {});
  }, [projectId]);

  if (!projectId) {
    return (
      <p className="p-4 text-sm text-slate-400">
        Export unlocks once a spec is generated — the session gets a project, and the
        project carries the artifacts.
      </p>
    );
  }

  return (
    <div className="flex-1 space-y-4 overflow-y-auto p-4 text-sm">
      <section className="rounded-lg border border-slate-800 bg-slate-950/60 p-4">
        <h3 className="font-medium">Specification set</h3>
        <p className="mt-1 text-xs text-slate-400">
          requirements.md + design.md + tasks.md, zipped with a manifest.
        </p>
        <a
          href={`/api/backend/v1/projects/${projectId}/export`}
          className="mt-3 inline-block rounded-lg border border-slate-600 px-3 py-1.5 text-xs hover:border-indigo-400 hover:text-indigo-300"
        >
          ⬇ Download spec zip
        </a>
        <div className="mt-2 flex flex-wrap gap-2">
          {DOC_TYPES.map((t) => {
            const latest = spec?.[t]?.latest;
            if (!latest) return null;
            return (
              <a
                key={t}
                href={`/api/backend/v1/chat/specs/${latest.id}/download`}
                className="rounded border border-slate-700 px-2 py-1 text-[11px] text-slate-300 hover:border-indigo-400"
              >
                {DOC_LABELS[t]} v{latest.version}
              </a>
            );
          })}
        </div>
      </section>

      <section className="rounded-lg border border-slate-800 bg-slate-950/60 p-4">
        <h3 className="font-medium">Handoff bundle</h3>
        <p className="mt-1 text-xs text-slate-400">
          Working CDK repository from the latest ready build — specs, source, README.
        </p>
        {bundleBuild ? (
          <a
            href={`/api/backend/v1/builds/${bundleBuild.id}/bundle`}
            download
            className="mt-3 inline-block rounded-lg border border-slate-600 px-3 py-1.5 text-xs hover:border-indigo-400 hover:text-indigo-300"
          >
            📦 Download handoff bundle
          </a>
        ) : (
          <p className="mt-3 text-xs text-slate-400">
            No ready CDK build yet —{" "}
            <Link href={`/projects/${projectId}?tab=build`} className="underline hover:text-slate-200">
              run one from the project Build tab
            </Link>
            .
          </p>
        )}
      </section>
    </div>
  );
}
