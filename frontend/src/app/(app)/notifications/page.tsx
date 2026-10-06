"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { AppNotification, NotificationList } from "@/lib/types";

const GROUPS = [
  ["", "All"],
  ["generation", "Generation"],
  ["deployments", "Deployments"],
  ["cost", "Cost"],
  ["risk", "Risk"],
  ["marketplace", "Marketplace"],
] as const;

/** Full notification history (notifications spec R1.3). */
export default function NotificationsPage() {
  const router = useRouter();
  const [group, setGroup] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [data, setData] = useState<NotificationList | null>(null);
  const [page, setPage] = useState(1);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ page: String(page), page_size: "50" });
    if (group) params.set("group", group);
    if (unreadOnly) params.set("unread", "true");
    setData(await api<NotificationList>(`/v1/notifications?${params}`));
  }, [group, unreadOnly, page]);

  useEffect(() => {
    load().catch(() => setData({ items: [], total: 0, page: 1, page_size: 50 }));
  }, [load]);

  async function open(item: AppNotification) {
    if (!item.read_at) await api(`/v1/notifications/${item.id}/read`, { method: "POST" }).catch(() => {});
    if (item.link) router.push(item.link);
    else load();
  }

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Notifications</h1>
        <button
          onClick={async () => {
            await api("/v1/notifications/read-all", { method: "POST" }).catch(() => {});
            load();
          }}
          className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-slate-500"
        >
          Mark all read
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-sm">
        {GROUPS.map(([value, label]) => (
          <button
            key={value}
            onClick={() => {
              setGroup(value);
              setPage(1);
            }}
            className={`rounded-md px-3 py-1.5 ${
              group === value ? "bg-slate-800 text-white" : "text-slate-400 hover:bg-slate-900"
            }`}
          >
            {label}
          </button>
        ))}
        <label className="ml-auto flex items-center gap-2 text-slate-400">
          <input
            type="checkbox"
            checked={unreadOnly}
            onChange={(e) => {
              setUnreadOnly(e.target.checked);
              setPage(1);
            }}
          />
          Unread only
        </label>
      </div>

      {data === null ? (
        <p className="text-slate-400">Loading…</p>
      ) : data.items.length === 0 ? (
        <EmptyState
          icon="🔔"
          title="No notifications"
          body="Deployment results, cost warnings and review requests will land here."
        />
      ) : (
        <div className="divide-y divide-slate-800 overflow-hidden rounded-xl border border-slate-800">
          {data.items.map((item) => (
            <button
              key={item.id}
              onClick={() => open(item)}
              className={`flex w-full items-start justify-between gap-4 bg-slate-900/40 px-5 py-3 text-left hover:bg-slate-900/70 ${
                item.read_at ? "opacity-60" : ""
              }`}
            >
              <span className="min-w-0">
                <span className="block text-sm font-medium text-slate-200">{item.title}</span>
                <span className="block text-sm text-slate-400">{item.body}</span>
              </span>
              <span className="shrink-0 text-xs text-slate-400">
                {new Date(item.created_at).toLocaleString()}
              </span>
            </button>
          ))}
        </div>
      )}

      {data && totalPages > 1 && (
        <div className="flex justify-end gap-2 text-sm">
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
      )}
    </div>
  );
}
