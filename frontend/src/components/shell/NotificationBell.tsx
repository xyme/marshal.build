"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import type { AppNotification, NotificationList } from "@/lib/types";

const TYPE_ICONS: Record<string, string> = {
  generation_complete: "📝",
  deploy_succeeded: "🚀",
  deploy_failed: "💥",
  cost_threshold: "💸",
  cost_cap_reached: "⛔",
  risk_review_requested: "🛡️",
  risk_decided: "✅",
  risk_changes_requested: "✏️",
  risk_escalated: "⏫",
  sample_published: "🛒",
};

function relTime(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  return hours < 24 ? `${hours}h` : `${Math.round(hours / 24)}d`;
}

/** Header bell: unread poll (30s, paused when hidden) + dropdown + toast bridge. */
export default function NotificationBell() {
  const router = useRouter();
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<AppNotification[] | null>(null);
  const lastUnreadRef = useRef(0);

  const poll = useCallback(async () => {
    try {
      const { unread: count } = await api<{ unread: number }>("/v1/notifications/unread-count");
      // Toast bridge: new arrivals while the app is open (notifications design)
      if (count > lastUnreadRef.current && lastUnreadRef.current >= 0) {
        const delta = count - lastUnreadRef.current;
        if (lastUnreadRef.current > 0 || delta > 0) {
          const latest = await api<NotificationList>("/v1/notifications?page_size=1");
          const newest = latest.items[0];
          if (newest && !newest.read_at && count > lastUnreadRef.current) {
            toast(newest.title, {
              description: newest.body.slice(0, 120),
              action: newest.link
                ? { label: "Open", onClick: () => router.push(newest.link as string) }
                : undefined,
            });
          }
        }
      }
      lastUnreadRef.current = count;
      setUnread(count);
    } catch {
      /* transient */
    }
  }, [router]);

  useEffect(() => {
    lastUnreadRef.current = -1; // first poll: no toast
    poll();
    const interval = setInterval(() => {
      if (document.visibilityState === "visible") poll();
    }, 30_000);
    return () => clearInterval(interval);
  }, [poll]);

  async function openDropdown() {
    setOpen((v) => !v);
    if (!open) {
      try {
        const list = await api<NotificationList>("/v1/notifications?page_size=20");
        setItems(list.items);
      } catch {
        setItems([]);
      }
    }
  }

  async function clickItem(item: AppNotification) {
    setOpen(false);
    if (!item.read_at) {
      api(`/v1/notifications/${item.id}/read`, { method: "POST" })
        .then(poll)
        .catch(() => {});
    }
    if (item.link) router.push(item.link);
  }

  async function markAll() {
    await api("/v1/notifications/read-all", { method: "POST" }).catch(() => {});
    setItems((prev) => prev?.map((i) => ({ ...i, read_at: i.read_at ?? new Date().toISOString() })) ?? null);
    poll();
  }

  return (
    <div className="relative">
      <button
        onClick={openDropdown}
        aria-label={`Notifications${unread ? ` (${unread} unread)` : ""}`}
        className="relative rounded-full border border-slate-700 p-2 text-sm hover:border-slate-500"
      >
        🔔
        {unread > 0 && (
          <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-indigo-500 px-1 text-[10px] font-bold text-white">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>
      {open && (
        <div
          className="absolute right-0 z-50 mt-2 w-96 overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-xl"
          onMouseLeave={() => setOpen(false)}
        >
          <div className="flex items-center justify-between border-b border-slate-800 px-4 py-2.5 text-sm">
            <span className="font-medium">Notifications</span>
            <div className="flex gap-3 text-xs">
              {unread > 0 && (
                <button onClick={markAll} className="text-indigo-400 hover:text-indigo-300">
                  Mark all read
                </button>
              )}
              <Link
                href="/notifications"
                onClick={() => setOpen(false)}
                className="text-slate-400 hover:text-slate-300"
              >
                View all
              </Link>
            </div>
          </div>
          <div className="max-h-96 overflow-y-auto">
            {items === null ? (
              <p className="px-4 py-6 text-center text-sm text-slate-400">Loading…</p>
            ) : items.length === 0 ? (
              <p className="px-4 py-6 text-center text-sm text-slate-400">
                Nothing yet — deployment results, cost warnings and review requests land here.
              </p>
            ) : (
              items.map((item) => (
                <button
                  key={item.id}
                  onClick={() => clickItem(item)}
                  className={`flex w-full items-start gap-3 px-4 py-2.5 text-left hover:bg-slate-800/60 ${
                    item.read_at ? "opacity-60" : ""
                  }`}
                >
                  <span aria-hidden className="mt-0.5">{TYPE_ICONS[item.type] ?? "•"}</span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-slate-200">{item.title}</span>
                    <span className="block truncate text-xs text-slate-400">{item.body}</span>
                  </span>
                  <span className="shrink-0 text-[10px] text-slate-400">{relTime(item.created_at)}</span>
                </button>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
