"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { useUser } from "@/components/user-context";
import type { CommentThread, DocType, ProjectRole } from "@/lib/types";

/** Heading slugs from markdown — mirror of the anchor scheme (R4). */
export function extractHeadings(content: string): { anchor: string; text: string }[] {
  const out: { anchor: string; text: string }[] = [];
  const seen = new Set<string>();
  for (const line of content.split("\n")) {
    const m = /^#{1,6}\s+(.+?)\s*$/.exec(line);
    if (!m) continue;
    const text = m[1].replace(/[*_`~]/g, "").trim();
    let anchor = text
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, "")
      .trim()
      .replace(/\s+/g, "-")
      .slice(0, 256);
    while (seen.has(anchor)) anchor = `${anchor}-x`;
    seen.add(anchor);
    if (anchor) out.push({ anchor, text });
  }
  return out;
}

function relTime(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  return hours < 24 ? `${hours}h` : `${Math.round(hours / 24)}d`;
}

/** Spec comments drawer: heading-anchored threads + resolve (collaboration R4). */
export default function CommentsPanel({
  projectId,
  docType,
  docContent,
  myRole,
  onCountChange,
}: {
  projectId: string;
  docType: DocType;
  docContent: string;
  myRole: ProjectRole;
  onCountChange?: (n: number) => void;
}) {
  const { user } = useUser();
  const [threads, setThreads] = useState<CommentThread[]>([]);
  const [showResolved, setShowResolved] = useState(false);
  const [newAnchor, setNewAnchor] = useState<string>("");
  const [body, setBody] = useState("");
  const [replyFor, setReplyFor] = useState<string | null>(null);
  const [replyBody, setReplyBody] = useState("");
  const [busy, setBusy] = useState(false);

  const headings = useMemo(() => extractHeadings(docContent), [docContent]);
  const headingSet = useMemo(() => new Set(headings.map((h) => h.anchor)), [headings]);

  const load = useCallback(async () => {
    try {
      const r = await api<{ threads: CommentThread[] }>(
        `/v1/projects/${projectId}/comments?doc_type=${docType}&include_resolved=true`
      );
      setThreads(r.threads);
      onCountChange?.(r.threads.filter((t) => !t.resolved).length);
    } catch {
      /* panel is secondary */
    }
  }, [projectId, docType, onCountChange]);

  useEffect(() => {
    load();
  }, [load]);

  async function addComment(e: React.FormEvent) {
    e.preventDefault();
    if (!body.trim() || !newAnchor) return;
    setBusy(true);
    try {
      const heading = headings.find((h) => h.anchor === newAnchor);
      await api(`/v1/projects/${projectId}/comments`, {
        method: "POST",
        body: JSON.stringify({
          doc_type: docType,
          anchor: newAnchor,
          anchor_text: heading?.text ?? newAnchor,
          body: body.trim(),
        }),
      });
      setBody("");
      await load();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function reply(threadId: string) {
    if (!replyBody.trim()) return;
    setBusy(true);
    try {
      await api(`/v1/comments/${threadId}/reply`, {
        method: "POST",
        body: JSON.stringify({ body: replyBody.trim() }),
      });
      setReplyBody("");
      setReplyFor(null);
      await load();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function setResolved(threadId: string, resolved: boolean) {
    try {
      await api(`/v1/comments/${threadId}/${resolved ? "resolve" : "unresolve"}`, {
        method: "POST",
      });
      await load();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function remove(commentId: string) {
    try {
      await api(`/v1/comments/${commentId}`, { method: "DELETE" });
      await load();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  const open = threads.filter((t) => !t.resolved);
  const resolved = threads.filter((t) => t.resolved);
  const orphaned = open.filter((t) => !headingSet.has(t.anchor));
  const anchored = open.filter((t) => headingSet.has(t.anchor));

  const canModerate = (t: CommentThread) =>
    t.author_id === user?.id || myRole === "editor" || myRole === "owner";
  const canDelete = (t: CommentThread) => t.author_id === user?.id || myRole === "owner";

  function Thread({ t, orphan }: { t: CommentThread; orphan?: boolean }) {
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-sm">
        <div className="flex items-center justify-between gap-2 text-xs text-slate-400">
          <span className={orphan ? "italic" : "text-indigo-300"}>
            {orphan ? `(removed) ${t.anchor_text || t.anchor}` : t.anchor_text || t.anchor}
          </span>
          <span>{relTime(t.created_at)}</span>
        </div>
        <p className="mt-1.5 whitespace-pre-wrap">{t.body}</p>
        <p className="mt-1 text-xs text-slate-400">{t.author_name}</p>
        {t.replies.map((r) => (
          <div key={r.id} className="mt-2 border-l-2 border-slate-800 pl-3">
            <p className="whitespace-pre-wrap text-sm">{r.body}</p>
            <p className="mt-0.5 text-xs text-slate-400">
              {r.author_name} · {relTime(r.created_at)}
              {canDelete(r) && (
                <button
                  onClick={() => remove(r.id)}
                  className="ml-2 text-slate-400 hover:text-red-400"
                >
                  delete
                </button>
              )}
            </p>
          </div>
        ))}
        <div className="mt-2 flex items-center gap-3 text-xs">
          {replyFor === t.id ? (
            <form
              className="flex w-full gap-1"
              onSubmit={(e) => {
                e.preventDefault();
                reply(t.id);
              }}
            >
              <input
                autoFocus
                value={replyBody}
                onChange={(e) => setReplyBody(e.target.value)}
                placeholder="Reply…"
                className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 outline-none focus:border-indigo-500"
              />
              <button
                type="submit"
                disabled={busy || !replyBody.trim()}
                className="rounded bg-indigo-500 px-2 py-1 text-white disabled:opacity-40"
              >
                Send
              </button>
            </form>
          ) : (
            <>
              {!t.resolved && (
                <button
                  onClick={() => setReplyFor(t.id)}
                  className="text-slate-400 hover:text-slate-200"
                >
                  Reply
                </button>
              )}
              {canModerate(t) && (
                <button
                  onClick={() => setResolved(t.id, !t.resolved)}
                  className="text-slate-400 hover:text-emerald-300"
                >
                  {t.resolved ? "Reopen" : "Resolve"}
                </button>
              )}
              {canDelete(t) && (
                <button onClick={() => remove(t.id)} className="text-slate-400 hover:text-red-400">
                  Delete
                </button>
              )}
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-3">
      <form onSubmit={addComment} className="space-y-2 rounded-lg border border-slate-800 p-3">
        <p className="text-xs font-medium text-slate-400">New comment</p>
        <select
          value={newAnchor}
          onChange={(e) => setNewAnchor(e.target.value)}
          className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs"
          aria-label="Section"
        >
          <option value="">Choose a section…</option>
          {headings.map((h) => (
            <option key={h.anchor} value={h.anchor}>
              {h.text}
            </option>
          ))}
        </select>
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={2}
          placeholder={headings.length === 0 ? "No headings in this document yet" : "Comment on this section…"}
          disabled={headings.length === 0}
          className="w-full resize-none rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm outline-none focus:border-indigo-500 disabled:opacity-40"
        />
        <button
          type="submit"
          disabled={busy || !body.trim() || !newAnchor}
          className="rounded bg-indigo-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
        >
          Comment
        </button>
      </form>

      {anchored.length === 0 && orphaned.length === 0 && resolved.length === 0 && (
        <p className="py-4 text-center text-xs text-slate-400">
          No comments on {docType}.md yet.
        </p>
      )}
      {anchored.map((t) => (
        <Thread key={t.id} t={t} />
      ))}
      {orphaned.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs uppercase tracking-wide text-slate-400">
            Orphaned (section no longer exists)
          </p>
          {orphaned.map((t) => (
            <Thread key={t.id} t={t} orphan />
          ))}
        </div>
      )}
      {resolved.length > 0 && (
        <div className="space-y-2">
          <button
            onClick={() => setShowResolved((v) => !v)}
            className="text-xs text-slate-400 hover:text-slate-300"
          >
            {showResolved ? "Hide" : "Show"} resolved ({resolved.length})
          </button>
          {showResolved &&
            resolved.map((t) => <Thread key={t.id} t={t} orphan={!headingSet.has(t.anchor)} />)}
        </div>
      )}
    </div>
  );
}
