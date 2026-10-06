"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import MarkdownEditor from "@/components/spec/MarkdownEditor";
import CommentsPanel from "@/components/spec/CommentsPanel";
import VersionHistory from "@/components/spec/VersionHistory";
import SpecMarkdown from "@/components/markdown/SpecMarkdown";
import HelpLink from "@/components/shell/HelpLink";
import { useUser } from "@/components/user-context";
import { api } from "@/lib/api";
import type {
  DocType,
  FullSpecResponse,
  ProjectRole,
  SpecDraftOut,
  SpecSaveResult,
} from "@/lib/types";
import { DOC_TYPES } from "@/lib/types";

type ViewMode = "rendered" | "edit" | "split" | "history";

const DOC_LABELS: Record<DocType, string> = {
  requirements: "requirements.md",
  design: "design.md",
  tasks: "tasks.md",
};

/** Project spec workspace: tabs, view modes, Monaco editing, versions (S2-02/03).
 * S7: role-aware — viewers are read-only regardless of persona; comments drawer. */
export default function SpecWorkspace({
  projectId,
  myRole = "owner",
}: {
  projectId: string;
  myRole?: ProjectRole;
}) {
  const { user } = useUser();
  const hasEditorPersona = user?.role === "admin" || user?.persona === "power";
  const canEdit = hasEditorPersona && (myRole === "owner" || myRole === "editor");
  const [showComments, setShowComments] = useState(false);
  const [commentCount, setCommentCount] = useState(0);

  const [spec, setSpec] = useState<FullSpecResponse | null>(null);
  const [docType, setDocType] = useState<DocType>("requirements");
  const [mode, setMode] = useState<ViewMode>("rendered");
  const [editorValue, setEditorValue] = useState("");
  const [draftRestored, setDraftRestored] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const editorValueRef = useRef(editorValue);
  editorValueRef.current = editorValue;

  const load = useCallback(async () => {
    const data = await api<FullSpecResponse>(`/v1/projects/${projectId}/specs`);
    setSpec(data);
    return data;
  }, [projectId]);

  useEffect(() => {
    load().catch((e) => setError((e as Error).message));
  }, [load]);

  const latest = spec?.[docType]?.latest ?? null;
  const versions = spec?.[docType]?.versions ?? [];

  // Entering edit mode: hydrate the editor from draft (if any) or latest version
  useEffect(() => {
    if (mode !== "edit" && mode !== "split") return;
    let cancelled = false;
    (async () => {
      setDraftRestored(false);
      try {
        const draft = await api<SpecDraftOut | null>(
          `/v1/projects/${projectId}/specs/${docType}/draft`
        );
        if (cancelled) return;
        if (draft && draft.content !== (latest?.content ?? "")) {
          setEditorValue(draft.content);
          setDraftRestored(true);
        } else {
          setEditorValue(latest?.content ?? "");
        }
      } catch {
        setEditorValue(latest?.content ?? "");
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, docType, projectId, latest?.id]);

  const autosaveDraft = useCallback(() => {
    api(`/v1/projects/${projectId}/specs/${docType}/draft`, {
      method: "PUT",
      body: JSON.stringify({ content: editorValueRef.current }),
    }).catch(() => {});
  }, [projectId, docType]);

  async function saveVersion() {
    if (saving) return;
    setSaving(true);
    setError(null);
    setWarnings([]);
    try {
      const result = await api<SpecSaveResult>(
        `/v1/projects/${projectId}/specs/${docType}`,
        { method: "PUT", body: JSON.stringify({ content: editorValueRef.current }) }
      );
      setWarnings(result.warnings);
      setDraftRestored(false);
      setNotice(`Saved v${result.spec.version}`);
      setTimeout(() => setNotice(null), 3000);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function discardDraft() {
    await api(`/v1/projects/${projectId}/specs/${docType}/draft`, { method: "DELETE" }).catch(
      () => {}
    );
    setEditorValue(latest?.content ?? "");
    setDraftRestored(false);
  }

  const modes: { id: ViewMode; label: string; editorOnly?: boolean }[] = [
    { id: "rendered", label: "Rendered" },
    { id: "edit", label: "Edit", editorOnly: true },
    { id: "split", label: "Split", editorOnly: true },
    { id: "history", label: "History" },
  ];

  return (
    <section className="flex h-[calc(100vh-16rem)] min-h-[480px] flex-col rounded-xl border border-slate-800 bg-slate-900/40">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-2.5">
        <div className="flex items-center gap-1">
          {DOC_TYPES.map((t) => (
            <button
              key={t}
              onClick={() => {
                setDocType(t);
                setWarnings([]);
              }}
              className={`rounded-md px-2.5 py-1 text-xs ${
                docType === t ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {DOC_LABELS[t]}
              {spec?.[t]?.latest && (
                <span className="ml-1 text-[10px] text-slate-400">
                  v{spec[t].latest!.version}
                </span>
              )}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          {notice && <span className="text-xs text-emerald-400">{notice}</span>}
          <div className="flex rounded-lg border border-slate-700 p-0.5">
            {modes
              .filter((m) => !m.editorOnly || canEdit)
              .map((m) => (
                <button
                  key={m.id}
                  onClick={() => setMode(m.id)}
                  className={`rounded-md px-2.5 py-1 text-xs ${
                    mode === m.id ? "bg-slate-700 text-white" : "text-slate-400"
                  }`}
                >
                  {m.label}
                </button>
              ))}
          </div>
          {(mode === "edit" || mode === "split") && canEdit && (
            <button
              onClick={saveVersion}
              disabled={saving}
              title="Save as new version (Ctrl/Cmd+S)"
              className="rounded-lg bg-indigo-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
            >
              {saving ? "Saving…" : "Save version"}
            </button>
          )}
          <button
            onClick={() => setShowComments((v) => !v)}
            className={`rounded-lg border px-3 py-1.5 text-xs ${
              showComments
                ? "border-indigo-500 text-indigo-300"
                : "border-slate-600 text-slate-300 hover:border-indigo-400"
            }`}
            title="Section comments"
            aria-label={`Section comments${commentCount > 0 ? ` (${commentCount})` : ""}`}
          >
            💬 {commentCount > 0 ? commentCount : ""}
          </button>
          {latest && (
            <a
              href={`/api/backend/v1/chat/specs/${latest.id}/download`}
              className="rounded-lg border border-slate-600 px-3 py-1.5 text-xs text-slate-300 hover:border-indigo-400"
            >
              ⬇ Download
            </a>
          )}
          <HelpLink doc="quickstart" label="How specs work" />
        </div>
      </header>

      {myRole === "viewer" && (
        <div className="border-b border-slate-800 bg-slate-950/60 px-4 py-1.5 text-xs text-slate-400">
          👁 View-only — you can read and comment on this project.
        </div>
      )}

      {draftRestored && (
        <div className="flex items-center justify-between border-b border-amber-500/30 bg-amber-500/5 px-4 py-1.5 text-xs text-amber-300">
          <span>Unsaved draft restored (autosaved earlier).</span>
          <button onClick={discardDraft} className="underline hover:text-amber-200">
            Discard draft
          </button>
        </div>
      )}
      {warnings.length > 0 && (
        <div className="border-b border-amber-500/30 bg-amber-500/5 px-4 py-1.5 text-xs text-amber-300">
          Saved with warnings: {warnings.join(" · ")}
        </div>
      )}
      {error && (
        <div className="flex items-center justify-between gap-3 border-b border-red-500/40 bg-red-500/10 px-4 py-1.5 text-xs text-red-300">
          <span>{error}</span>
          <HelpLink doc="troubleshooting" label="Troubleshooting" />
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <div className="min-h-0 min-w-0 flex-1">
          {mode === "rendered" && (
            <div className="h-full overflow-y-auto p-5">
              {latest ? (
                <SpecMarkdown content={latest.content} />
              ) : (
                <p className="pt-16 text-center text-sm text-slate-400">
                  No {DOC_LABELS[docType]} yet — generate it from the chat session.
                </p>
              )}
            </div>
          )}
          {mode === "edit" && canEdit && (
            <MarkdownEditor
              value={editorValue}
              onChange={setEditorValue}
              onSave={saveVersion}
              onAutosave={autosaveDraft}
            />
          )}
          {mode === "split" && canEdit && (
            <div className="grid h-full grid-cols-2 divide-x divide-slate-800">
              <MarkdownEditor
                value={editorValue}
                onChange={setEditorValue}
                onSave={saveVersion}
                onAutosave={autosaveDraft}
              />
              <div className="overflow-y-auto p-5">
                <SpecMarkdown content={editorValue} />
              </div>
            </div>
          )}
          {mode === "history" && (
            <div className="h-full overflow-hidden p-4">
              <VersionHistory
                projectId={projectId}
                docType={docType}
                versions={versions}
                canEdit={!!canEdit}
                onRolledBack={() => load()}
              />
            </div>
          )}
        </div>
        {showComments && (
          <aside className="w-80 shrink-0 overflow-hidden border-l border-slate-800">
            <CommentsPanel
              projectId={projectId}
              docType={docType}
              docContent={latest?.content ?? ""}
              myRole={myRole}
              onCountChange={setCommentCount}
            />
          </aside>
        )}
      </div>
    </section>
  );
}
