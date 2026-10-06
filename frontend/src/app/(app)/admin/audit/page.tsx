"use client";

import { useCallback, useEffect, useState } from "react";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import type { AdminUserList, AuditEntry, AuditList } from "@/lib/types";

const CATEGORY_STYLE: Record<string, string> = {
  user: "bg-indigo-500/15 text-indigo-300",
  admin: "bg-purple-500/15 text-purple-300",
  deployment: "bg-emerald-500/15 text-emerald-400",
  security: "bg-red-500/15 text-red-400",
  model: "bg-amber-500/15 text-amber-400",
};

const DATE_PRESETS = [
  ["24h", 1],
  ["7d", 7],
  ["30d", 30],
  ["All", 0],
] as const;

function isoDaysAgo(days: number): string {
  const d = new Date(Date.now() - days * 24 * 3600 * 1000);
  return d.toISOString();
}

/** Admin > Audit Logs (S3-05/06, FSD §4.6.2): unified action + model timeline. */
export default function AdminAuditPage() {
  const readonly = useAdminReadonly();
  const [q, setQ] = useState("");
  const [userId, setUserId] = useState("");
  const [category, setCategory] = useState("");
  const [days, setDays] = useState<number>(7);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<AuditList | null>(null);
  const [users, setUsers] = useState<{ id: string; email: string }[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<AuditEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  const filterParams = useCallback(() => {
    const params = new URLSearchParams();
    if (q.trim()) params.set("q", q.trim());
    if (userId) params.set("user_id", userId);
    if (category) params.set("category", category);
    if (days > 0) params.set("from", isoDaysAgo(days));
    return params;
  }, [q, userId, category, days]);

  const load = useCallback(async () => {
    try {
      const params = filterParams();
      params.set("page", String(page));
      params.set("page_size", "50");
      setData(await api<AuditList>(`/v1/admin/audit-logs?${params}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [filterParams, page]);

  useEffect(() => {
    const t = setTimeout(load, 250);
    return () => clearTimeout(t);
  }, [load]);

  useEffect(() => {
    api<AdminUserList>("/v1/admin/users?page_size=200")
      .then((l) => setUsers(l.items.map((u) => ({ id: u.id, email: u.email }))))
      .catch(() => {});
  }, []);

  async function toggleExpand(entry: AuditEntry) {
    if (expanded === entry.id) {
      setExpanded(null);
      setDetail(null);
      return;
    }
    setExpanded(entry.id);
    setDetail(null);
    try {
      setDetail(await api<AuditEntry>(`/v1/admin/audit-logs/${entry.id}`));
    } catch {
      setDetail(entry);
    }
  }

  async function exportCsv() {
    setExporting(true);
    try {
      const response = await fetch(`/api/backend/v1/admin/audit-logs/export?${filterParams()}`, {
        method: "POST",
      });
      if (!response.ok) throw new Error(`Export failed (${response.status})`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `marshal-audit-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setExporting(false);
    }
  }

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Audit Logs</h1>
          <p className="mt-1 text-sm text-slate-400">
            Every user action and model invocation, with full detail.
          </p>
        </div>
        <button
          onClick={exportCsv}
          disabled={exporting || readonly}
          title={readonly ? "Exports are available to administrators only" : undefined}
          className="rounded-lg border border-slate-700 px-4 py-2 text-sm hover:border-slate-500 disabled:opacity-40"
        >
          {exporting ? "Exporting…" : "Export CSV"}
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-800 bg-slate-900/40 p-3 text-sm">
        <input
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setPage(1);
          }}
          placeholder="Search action / resource / model…"
          className="w-full max-w-xs rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 outline-none focus:border-indigo-500"
        />
        <select
          value={userId}
          onChange={(e) => {
            setUserId(e.target.value);
            setPage(1);
          }}
          className="max-w-[200px] rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5"
        >
          <option value="">All users</option>
          {users.map((u) => (
            <option key={u.id} value={u.id}>
              {u.email}
            </option>
          ))}
        </select>
        <select
          value={category}
          onChange={(e) => {
            setCategory(e.target.value);
            setPage(1);
          }}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5"
        >
          <option value="">All categories</option>
          <option value="user">User</option>
          <option value="admin">Admin</option>
          <option value="deployment">Deployment</option>
          <option value="security">Security</option>
          <option value="model">Model</option>
        </select>
        <div className="flex overflow-hidden rounded-lg border border-slate-700">
          {DATE_PRESETS.map(([label, value]) => (
            <button
              key={label}
              onClick={() => {
                setDays(value);
                setPage(1);
              }}
              className={`px-3 py-1.5 ${
                days === value ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {data === null ? (
        <p className="text-slate-400">Loading…</p>
      ) : data.items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 p-10 text-center text-slate-400">
          No audit entries match these filters.
        </div>
      ) : (
        <>
          <div className="overflow-x-auto rounded-xl border border-slate-800">
            <table className="w-full text-sm">
              <thead className="bg-slate-900/80 text-left text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-4 py-3">Time</th>
                  <th className="px-4 py-3">User</th>
                  <th className="px-4 py-3">Category</th>
                  <th className="px-4 py-3">Action</th>
                  <th className="px-4 py-3">Resource</th>
                  <th className="px-4 py-3" aria-label="Expand" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {data.items.map((entry) => (
                  <>
                    <tr
                      key={entry.id}
                      onClick={() => toggleExpand(entry)}
                      className="cursor-pointer bg-slate-900/40 hover:bg-slate-900/70"
                    >
                      <td className="whitespace-nowrap px-4 py-3 font-mono text-xs text-slate-400">
                        {new Date(entry.created_at).toLocaleString()}
                      </td>
                      <td className="max-w-[180px] truncate px-4 py-3 text-slate-300">
                        {entry.actor_email ?? "system"}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                            CATEGORY_STYLE[entry.category] ?? "bg-slate-700/40 text-slate-400"
                          }`}
                        >
                          {entry.category}
                        </span>
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-slate-300">{entry.action}</td>
                      <td className="max-w-[260px] truncate px-4 py-3 text-xs text-slate-400">
                        {entry.summary ?? [entry.resource_type, entry.resource_id].filter(Boolean).join(" ")}
                      </td>
                      <td className="px-4 py-3 text-slate-400">{expanded === entry.id ? "▾" : "▸"}</td>
                    </tr>
                    {expanded === entry.id && (
                      <tr key={`${entry.id}-detail`} className="bg-slate-950/60">
                        <td colSpan={6} className="px-6 py-4">
                          {detail === null ? (
                            <p className="text-sm text-slate-400">Loading detail…</p>
                          ) : (
                            <ExpandedDetail entry={detail} />
                          )}
                        </td>
                      </tr>
                    )}
                  </>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between text-sm text-slate-400">
            <span>
              {data.total} entr{data.total === 1 ? "y" : "ies"} · page {data.page} of {totalPages}
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="rounded-lg border border-slate-700 px-3 py-1.5 hover:border-slate-500 disabled:opacity-40"
              >
                ← Prev
              </button>
              <button
                onClick={() => setPage((p) => p + 1)}
                disabled={page >= totalPages}
                className="rounded-lg border border-slate-700 px-3 py-1.5 hover:border-slate-500 disabled:opacity-40"
              >
                Next →
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function ExpandedDetail({ entry }: { entry: AuditEntry }) {
  const detail = entry.detail ?? {};
  const prompt = typeof detail.prompt_text === "string" ? detail.prompt_text : null;
  const response = typeof detail.response_text === "string" ? detail.response_text : null;
  const rest: Record<string, unknown> = { ...detail };
  delete rest.prompt_text;
  delete rest.response_text;

  return (
    <div className="space-y-3 text-sm">
      {prompt != null && <CollapsibleText label="Prompt" text={prompt} />}
      {response != null && <CollapsibleText label="Response" text={response} />}
      <div>
        <h4 className="text-xs font-medium uppercase tracking-wide text-slate-400">Detail</h4>
        <pre className="mt-1 max-h-64 overflow-auto rounded-lg border border-slate-800 bg-slate-950 p-3 font-mono text-xs text-slate-300">
          {JSON.stringify(rest, null, 2)}
        </pre>
      </div>
    </div>
  );
}

function CollapsibleText({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = useState(text.length <= 2000);
  return (
    <div>
      <div className="flex items-center gap-2">
        <h4 className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</h4>
        {text.length > 2000 && (
          <button
            onClick={() => setOpen((v) => !v)}
            className="text-xs text-indigo-400 hover:text-indigo-300"
          >
            {open ? "collapse" : `expand (${(text.length / 1024).toFixed(1)} KB)`}
          </button>
        )}
      </div>
      <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950 p-3 font-mono text-xs text-slate-300">
        {open ? text : `${text.slice(0, 2000)}…`}
      </pre>
    </div>
  );
}
