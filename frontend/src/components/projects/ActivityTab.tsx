"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { ActivityItem, ActivityList } from "@/lib/types";

const FILTERS = [
  ["all", "All"],
  ["spec", "Spec changes"],
  ["deployments", "Deployments"],
  ["system", "System"],
] as const;

const ICONS: Record<string, string> = {
  user: "📝",
  admin: "🛡️",
  deployment: "🚀",
  security: "🔒",
};

/** Human phrasing per audit action (fallback: humanized verb). */
function phrase(item: ActivityItem): string {
  const d = item.detail as Record<string, unknown>;
  switch (item.action) {
    case "project_created":
      return `Project created${d.name ? ` — "${d.name}"` : ""}`;
    case "project_updated":
      return "Project details updated";
    case "project_archived":
      return "Project archived";
    case "project_restored":
      return "Project restored from archive";
    case "spec_saved":
      return "Spec saved";
    case "spec_rolled_back":
      return "Spec rolled back to an earlier version";
    case "spec_draft_saved":
      return "Draft autosaved";
    case "spec_draft_discarded":
      return "Draft discarded";
    case "session_spec_saved":
      return "Generated spec saved from chat";
    case "generation_triggered":
      return "Spec generation started";
    case "doc_regenerate_triggered":
      return "Document regeneration started";
    case "deploy_triggered":
      return "Deployment started";
    case "teardown_triggered":
      return "Teardown started";
    case "sample_forked":
      return `Forked from "${d.sample_title ?? "a marketplace sample"}"`;
    default:
      return item.action.replace(/_/g, " ");
  }
}

function dayLabel(iso: string): string {
  const date = new Date(iso);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return date.toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" });
}

export default function ActivityTab({ projectId }: { projectId: string }) {
  const [filter, setFilter] = useState<(typeof FILTERS)[number][0]>("all");
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (pageToLoad: number, append: boolean) => {
      setLoading(true);
      try {
        const body = await api<ActivityList>(
          `/v1/projects/${projectId}/activity?filter=${filter}&page=${pageToLoad}&page_size=30`
        );
        setItems((prev) => (append ? [...prev, ...body.items] : body.items));
        setTotal(body.total);
        setPage(pageToLoad);
        setError(null);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setLoading(false);
      }
    },
    [projectId, filter]
  );

  useEffect(() => {
    load(1, false);
  }, [load]);

  const groups: { label: string; items: ActivityItem[] }[] = [];
  for (const item of items) {
    const label = dayLabel(item.created_at);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.items.push(item);
    else groups.push({ label, items: [item] });
  }

  return (
    <div className="space-y-4">
      <div className="flex gap-1 text-sm">
        {FILTERS.map(([key, label]) => (
          <button
            key={key}
            onClick={() => setFilter(key)}
            className={`rounded-md px-3 py-1.5 ${
              filter === key
                ? "bg-slate-800 text-white"
                : "text-slate-400 hover:bg-slate-900 hover:text-slate-200"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {loading && items.length === 0 ? (
        <p className="text-slate-400">Loading…</p>
      ) : items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 p-8 text-center text-sm text-slate-400">
          No activity recorded yet{filter !== "all" ? " for this filter" : ""}.
        </div>
      ) : (
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
          {groups.map((group) => (
            <div key={group.label} className="mb-5 last:mb-0">
              <h3 className="text-xs font-medium uppercase tracking-wide text-slate-400">
                {group.label}
              </h3>
              <ul className="mt-2 space-y-1.5">
                {group.items.map((item) => (
                  <li key={item.id} className="flex items-baseline gap-3 text-sm">
                    <span className="w-12 shrink-0 font-mono text-xs text-slate-400">
                      {new Date(item.created_at).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                    <span aria-hidden>{ICONS[item.category] ?? "•"}</span>
                    <span className="text-slate-300">{phrase(item)}</span>
                    {item.actor_email && (
                      <span className="text-xs text-slate-400">{item.actor_email}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {items.length < total && (
            <button
              onClick={() => load(page + 1, true)}
              disabled={loading}
              className="mt-2 rounded-lg border border-slate-700 px-4 py-2 text-sm hover:border-slate-500 disabled:opacity-40"
            >
              {loading ? "Loading…" : "Load more"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
