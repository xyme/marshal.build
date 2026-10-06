"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import type { MembersResponse, Project } from "@/lib/types";

/** Share modal: member management + ownership transfer (collaboration R2/R5). */
export default function ShareModal({
  project,
  onClose,
  onChanged,
}: {
  project: Project;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [data, setData] = useState<MembersResponse | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"viewer" | "editor">("editor");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [transferTarget, setTransferTarget] = useState<string>("");
  const [transferConfirm, setTransferConfirm] = useState("");
  const [showTransfer, setShowTransfer] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await api<MembersResponse>(`/v1/projects/${project.id}/members`));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [project.id]);

  useEffect(() => {
    load();
  }, [load]);

  async function addMember(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/v1/projects/${project.id}/members`, {
        method: "POST",
        body: JSON.stringify({ email: email.trim(), role }),
      });
      setEmail("");
      toast.success("Member added — they've been notified.");
      await load();
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function changeRole(userId: string, newRole: string) {
    try {
      await api(`/v1/projects/${project.id}/members/${userId}`, {
        method: "PATCH",
        body: JSON.stringify({ role: newRole }),
      });
      await load();
      onChanged();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function removeMember(userId: string) {
    try {
      await api(`/v1/projects/${project.id}/members/${userId}`, { method: "DELETE" });
      await load();
      onChanged();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function transfer() {
    if (!transferTarget) return;
    setBusy(true);
    try {
      await api(`/v1/projects/${project.id}/transfer-ownership`, {
        method: "POST",
        body: JSON.stringify({ user_id: transferTarget }),
      });
      toast.success("Ownership transferred — you're now an editor on this project.");
      onChanged();
      onClose();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-xl border border-slate-700 bg-slate-900 p-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-medium">Share &ldquo;{project.name}&rdquo;</h2>
          <button onClick={onClose} aria-label="Close" className="text-slate-400 hover:text-white">
            ✕
          </button>
        </div>
        <p className="mt-1 text-xs text-slate-400">
          Viewers can read and comment. Editors can also edit specs and use chat sessions.
          Deployment and budget stay with the owner.
        </p>

        <form onSubmit={addMember} className="mt-4 flex gap-2">
          <input
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="teammate@company.com"
            type="email"
            className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
          />
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as "viewer" | "editor")}
            className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm"
            aria-label="Role for the new member"
          >
            <option value="editor">Editor</option>
            <option value="viewer">Viewer</option>
          </select>
          <button
            type="submit"
            disabled={busy || !email.trim()}
            className="rounded-lg bg-indigo-500 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            Add
          </button>
        </form>
        {error && <p className="mt-2 text-xs text-red-300">{error}</p>}

        <div className="mt-4 space-y-2">
          {data?.owner && (
            <div className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm">
              <div className="min-w-0">
                <p className="truncate">{data.owner.name ?? data.owner.email}</p>
                <p className="truncate text-xs text-slate-400">{data.owner.email}</p>
              </div>
              <span className="rounded bg-indigo-500/15 px-2 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300">
                Owner
              </span>
            </div>
          )}
          {(data?.members ?? []).map((m) => (
            <div
              key={m.user_id}
              className="flex items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm"
            >
              <div className="min-w-0">
                <p className="truncate">{m.name ?? m.email}</p>
                <p className="truncate text-xs text-slate-400">{m.email}</p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <select
                  value={m.role}
                  onChange={(e) => changeRole(m.user_id, e.target.value)}
                  className="rounded border border-slate-700 bg-slate-950 px-1.5 py-1 text-xs"
                  aria-label={`Role for ${m.email}`}
                >
                  <option value="editor">Editor</option>
                  <option value="viewer">Viewer</option>
                </select>
                <button
                  onClick={() => removeMember(m.user_id)}
                  title="Remove access"
                  className="text-slate-400 hover:text-red-400"
                >
                  ✕
                </button>
              </div>
            </div>
          ))}
          {data && data.members.length === 0 && (
            <p className="py-2 text-center text-xs text-slate-400">
              Not shared with anyone yet.
            </p>
          )}
        </div>

        {data && data.members.length > 0 && (
          <div className="mt-6 rounded-lg border border-red-900/50 bg-red-950/20 p-4">
            <h3 className="text-sm font-medium text-red-300">Transfer ownership</h3>
            {!showTransfer ? (
              <button
                onClick={() => setShowTransfer(true)}
                className="mt-2 rounded-lg border border-red-800/60 px-3 py-1.5 text-xs text-red-300 hover:bg-red-950/40"
              >
                Transfer this project…
              </button>
            ) : (
              <div className="mt-2 space-y-2 text-sm">
                <p className="text-xs text-slate-400">
                  The new owner controls deployment, budget, sharing and deletion. You become
                  an editor.
                </p>
                <select
                  value={transferTarget}
                  onChange={(e) => setTransferTarget(e.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm"
                  aria-label="New owner"
                >
                  <option value="">Choose a member…</option>
                  {data.members.map((m) => (
                    <option key={m.user_id} value={m.user_id}>
                      {m.name ?? m.email}
                    </option>
                  ))}
                </select>
                <input
                  value={transferConfirm}
                  onChange={(e) => setTransferConfirm(e.target.value)}
                  placeholder={`Type "${project.name}" to confirm`}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-red-500"
                />
                <button
                  onClick={transfer}
                  disabled={busy || !transferTarget || transferConfirm !== project.name}
                  className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-40"
                >
                  Transfer ownership
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
