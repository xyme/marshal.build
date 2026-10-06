"use client";

import { useState } from "react";
import { useConfirm } from "@/components/ui/ConfirmDialog";
import type { ChatSession } from "@/lib/types";

export default function SessionSidebar({
  sessions,
  activeId,
  onSelect,
  onNew,
  onDelete,
  onRename,
}: {
  sessions: ChatSession[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
}) {
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const confirmDialog = useConfirm();

  return (
    <aside className="flex w-60 shrink-0 flex-col rounded-xl border border-slate-800 bg-slate-900/40">
      <div className="border-b border-slate-800 p-3">
        <button
          onClick={onNew}
          className="w-full rounded-lg bg-indigo-500 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-400"
        >
          + New session
        </button>
      </div>
      <div className="flex-1 space-y-1 overflow-y-auto p-2">
        {sessions.map((session) => (
          <div
            key={session.id}
            className={`group rounded-lg px-3 py-2 text-sm transition ${
              session.id === activeId
                ? "bg-slate-800 text-white"
                : "text-slate-400 hover:bg-slate-800/60"
            }`}
          >
            {renamingId === session.id ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  if (draft.trim()) onRename(session.id, draft.trim());
                  setRenamingId(null);
                }}
              >
                <input
                  autoFocus
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onBlur={() => setRenamingId(null)}
                  className="w-full rounded border border-slate-600 bg-slate-950 px-1 py-0.5 text-sm"
                />
              </form>
            ) : (
              <div className="flex items-center justify-between gap-1">
                <button
                  onClick={() => onSelect(session.id)}
                  className="min-w-0 flex-1 truncate text-left"
                  title={
                    session.mode === "guided"
                      ? `${session.title} — guided${session.guided_state?.step && session.guided_state.step !== "done" ? ` (${session.guided_state.step.replace(/_/g, " ")})` : ""}`
                      : session.title
                  }
                >
                  {session.mode === "guided" && <span aria-hidden>🧭 </span>}
                  {session.title}
                </button>
                <span className="hidden shrink-0 gap-1 group-hover:flex">
                  <button
                    title="Rename"
                    onClick={() => {
                      setRenamingId(session.id);
                      setDraft(session.title);
                    }}
                    className="text-slate-400 hover:text-slate-200"
                  >
                    ✏️
                  </button>
                  <button
                    title="Delete"
                    onClick={() => {
                      confirmDialog({ title: "Delete session", body: "The session and its messages are removed permanently.", confirmLabel: "Delete", destructive: true }).then((ok) => { if (ok) onDelete(session.id); });
                    }}
                    className="text-slate-400 hover:text-red-400"
                  >
                    🗑️
                  </button>
                </span>
              </div>
            )}
            <div className="mt-0.5 flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-slate-400">
              <span>{session.status.replace(/_/g, " ")}</span>
              {session.is_mine === false && (
                <span
                  className="truncate rounded bg-indigo-500/15 px-1 py-px normal-case text-indigo-300"
                  title={`Shared project session${session.creator_name ? ` by ${session.creator_name}` : ""}${session.project_name ? ` — ${session.project_name}` : ""}`}
                >
                  {session.creator_name ?? "shared"}
                  {session.project_name ? ` · ${session.project_name}` : ""}
                </span>
              )}
            </div>
          </div>
        ))}
        {sessions.length === 0 && (
          <p className="px-3 py-6 text-center text-xs text-slate-400">No sessions yet</p>
        )}
      </div>
    </aside>
  );
}
