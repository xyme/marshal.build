"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { PresenceUser } from "@/lib/types";

const COLORS = [
  "bg-indigo-500",
  "bg-emerald-500",
  "bg-amber-500",
  "bg-rose-500",
  "bg-cyan-500",
];

function hue(id: string): string {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) % COLORS.length;
  return COLORS[h];
}

/** Heartbeat presence: 30s POST while mounted, render avatar dots (R6). */
export default function PresenceDots({
  projectId,
  surface,
  selfId,
}: {
  projectId: string;
  surface: string;
  selfId?: string;
}) {
  const [active, setActive] = useState<PresenceUser[]>([]);

  useEffect(() => {
    let stopped = false;
    async function beat() {
      try {
        const r = await api<{ active: PresenceUser[] }>(
          `/v1/projects/${projectId}/presence`,
          { method: "POST", body: JSON.stringify({ surface }) }
        );
        if (!stopped) setActive(r.active);
      } catch {
        /* presence is best-effort */
      }
    }
    beat();
    const interval = setInterval(beat, 30_000);
    return () => {
      stopped = true;
      clearInterval(interval);
    };
  }, [projectId, surface]);

  const others = active.filter((a) => a.user_id !== selfId);
  if (others.length === 0) return null;
  return (
    <div className="flex items-center -space-x-1.5" aria-label="Viewing now">
      {others.slice(0, 5).map((a) => (
        <span
          key={a.user_id}
          title={`${a.name} — ${a.surface === "spec" ? "in spec editor" : "viewing project"}`}
          className={`flex h-6 w-6 items-center justify-center rounded-full border-2 border-slate-900 text-[10px] font-semibold text-white ${hue(a.user_id)}`}
        >
          {a.name.slice(0, 1).toUpperCase()}
        </span>
      ))}
      {others.length > 5 && (
        <span className="flex h-6 w-6 items-center justify-center rounded-full border-2 border-slate-900 bg-slate-700 text-[10px] text-slate-200">
          +{others.length - 5}
        </span>
      )}
    </div>
  );
}
