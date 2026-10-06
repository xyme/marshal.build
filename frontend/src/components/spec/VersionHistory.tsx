"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";
import { useConfirm } from "@/components/ui/ConfirmDialog";
import { api } from "@/lib/api";
import type { DocType, Spec, SpecVersionInfo } from "@/lib/types";

const MonacoDiff = dynamic(
  () => import("@/lib/monaco-setup").then(() => import("@monaco-editor/react")).then((m) => m.DiffEditor),
  { ssr: false, loading: () => <p className="p-4 text-sm text-slate-400">Loading diff…</p> }
);

/** Version list + side-by-side diff + non-destructive rollback (S2-03). */
export default function VersionHistory({
  projectId,
  docType,
  versions,
  onRolledBack,
  canEdit,
}: {
  projectId: string;
  docType: DocType;
  versions: SpecVersionInfo[];
  onRolledBack: () => void;
  canEdit: boolean;
}) {
  const [selected, setSelected] = useState<number[]>([]);
  const confirmDialog = useConfirm();
  const [diff, setDiff] = useState<{ original: Spec; modified: Spec } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggle(version: number) {
    setSelected((prev) => {
      const next = prev.includes(version)
        ? prev.filter((v) => v !== version)
        : [...prev.slice(-1), version];
      return next.sort((a, b) => a - b);
    });
  }

  useEffect(() => {
    if (selected.length !== 2) {
      setDiff(null);
      return;
    }
    const [a, b] = selected;
    Promise.all([
      api<Spec>(`/v1/projects/${projectId}/specs/${docType}/versions/${a}`),
      api<Spec>(`/v1/projects/${projectId}/specs/${docType}/versions/${b}`),
    ])
      .then(([original, modified]) => setDiff({ original, modified }))
      .catch((e) => setError((e as Error).message));
  }, [selected, projectId, docType]);

  async function rollback(version: number) {
    if (!(await confirmDialog({ title: `Roll back ${docType}.md to v${version}`, body: "Non-destructive: a new version is created with that content.", confirmLabel: "Roll back" }))) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/v1/projects/${projectId}/specs/${docType}/rollback`, {
        method: "POST",
        body: JSON.stringify({ version }),
      });
      setSelected([]);
      onRolledBack();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const originBadge: Record<string, string> = {
    generated: "bg-indigo-500/15 text-indigo-300",
    edited: "bg-amber-500/15 text-amber-300",
    rollback: "bg-slate-600/40 text-slate-300",
  };

  return (
    <div className="flex h-full flex-col gap-3 lg:flex-row">
      <div className="w-full shrink-0 space-y-1.5 overflow-y-auto lg:w-72">
        <p className="px-1 text-xs text-slate-400">
          Select two versions to compare{canEdit ? ", or roll back to any version" : ""}.
        </p>
        {versions.map((v) => (
          <div
            key={v.id}
            className={`flex items-center justify-between rounded-lg border px-3 py-2 text-sm ${
              selected.includes(v.version)
                ? "border-indigo-500 bg-indigo-500/10"
                : "border-slate-800 bg-slate-950/60"
            }`}
          >
            <button onClick={() => toggle(v.version)} className="flex items-center gap-2 text-left">
              <span className="font-mono">v{v.version}</span>
              <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${originBadge[v.origin]}`}>
                {v.origin}
              </span>
              <span className="text-[11px] text-slate-400">
                {v.created_by_name && (
                  <span className="mr-1 text-slate-400">{v.created_by_name} ·</span>
                )}
                {new Date(v.created_at).toLocaleString()}
              </span>
            </button>
            {canEdit && v.version !== versions[0]?.version && (
              <button
                onClick={() => rollback(v.version)}
                disabled={busy}
                className="text-[11px] text-slate-400 hover:text-indigo-300 disabled:opacity-40"
              >
                Roll back
              </button>
            )}
          </div>
        ))}
      </div>

      <div className="min-h-[300px] flex-1 overflow-hidden rounded-lg border border-slate-800">
        {error && <p className="p-3 text-sm text-red-300">{error}</p>}
        {diff ? (
          <MonacoDiff
            language="markdown"
            theme="vs-dark"
            original={diff.original.content}
            modified={diff.modified.content}
            options={{
              readOnly: true,
              renderSideBySide: true,
              minimap: { enabled: false },
              wordWrap: "on",
              fontSize: 12,
            }}
          />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-slate-400">
            {versions.length < 2
              ? "Only one version exists so far."
              : "Select two versions to see the diff."}
          </div>
        )}
      </div>
    </div>
  );
}
