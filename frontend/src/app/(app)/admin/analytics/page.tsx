"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "@/lib/api";

interface FunnelStep {
  step: string;
  users: number;
  events: number;
  conversion: number | null;
}
interface Summary {
  days: number;
  active_users: number;
  registered_users: number;
  events: number;
}
interface TeamRollup {
  team_id: string | null;
  team: string;
  active_users: number;
  events: Record<string, { users: number; count: number }>;
}

const STEP_LABELS: Record<string, string> = {
  sign_in: "Signed in",
  chat_message: "Chatted",
  spec_saved: "Saved a spec",
  build_started: "Built",
  deploy_started: "Deployed",
  teardown: "Tore down",
};

const RANGES = [7, 30, 90] as const;

/** Admin > Analytics (S16-04): in-house funnel, DAU, per-team rollups.
 * Server-side events only — no client beacons, no third parties (D6). */
export default function AdminAnalyticsPage() {
  const [days, setDays] = useState<(typeof RANGES)[number]>(30);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [funnel, setFunnel] = useState<FunnelStep[] | null>(null);
  const [dau, setDau] = useState<{ date: string; users: number }[] | null>(null);
  const [teams, setTeams] = useState<TeamRollup[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, f, d, t] = await Promise.all([
        api<Summary>(`/v1/admin/analytics/summary?days=${days}`),
        api<{ steps: FunnelStep[] }>(`/v1/admin/analytics/funnel?days=${days}`),
        api<{ date: string; users: number }[]>(`/v1/admin/analytics/daily-active?days=${days}`),
        api<TeamRollup[]>(`/v1/admin/analytics/teams?days=${days}`),
      ]);
      setSummary(s);
      setFunnel(f.steps);
      setDau(d);
      setTeams(t);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [days]);

  useEffect(() => {
    load();
  }, [load]);

  if (error) {
    return <p className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-red-300">{error}</p>;
  }
  if (!summary || !funnel) return <p className="text-slate-400">Loading…</p>;

  const maxUsers = Math.max(1, ...funnel.map((s) => s.users));

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Usage Analytics</h1>
          <p className="mt-1 text-sm text-slate-400">
            In-house events, written server-side at six lifecycle seams. No client
            beacons, no third-party analytics (owner decision D6) — the privacy note
            lives in the docs.
          </p>
        </div>
        <div className="flex gap-1 text-sm" role="group" aria-label="Time range">
          {RANGES.map((r) => (
            <button
              key={r}
              onClick={() => setDays(r)}
              className={`rounded-md px-3 py-1.5 ${
                days === r ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {r}d
            </button>
          ))}
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label={`Active users (${days}d)`} value={String(summary.active_users)} />
        <Stat label="Registered users" value={String(summary.registered_users)} />
        <Stat label={`Events (${days}d)`} value={String(summary.events)} />
      </div>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Funnel — distinct users per step
        </h2>
        <div className="mt-4 space-y-2">
          {funnel.map((step) => (
            <div key={step.step} className="flex items-center gap-3">
              <span className="w-28 shrink-0 text-sm text-slate-300">
                {STEP_LABELS[step.step] ?? step.step}
              </span>
              <div className="h-6 flex-1 overflow-hidden rounded bg-slate-800/60">
                <div
                  className="flex h-full items-center rounded bg-indigo-500/80 px-2 text-xs font-medium text-white"
                  style={{ width: `${Math.max((step.users / maxUsers) * 100, step.users ? 6 : 0)}%` }}
                >
                  {step.users > 0 && step.users}
                </div>
              </div>
              <span className="w-24 shrink-0 text-right text-xs text-slate-400">
                {step.conversion != null ? `${Math.round(step.conversion * 100)}% of prev` : ""}
              </span>
              <span className="w-20 shrink-0 text-right text-xs text-slate-400">
                {step.events} events
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Daily active users
        </h2>
        <div className="mt-3 h-48">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={dau ?? []} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
              <XAxis dataKey="date" tickFormatter={(d: string) => d.slice(5)} stroke="#475569" fontSize={11} />
              <YAxis stroke="#475569" fontSize={11} allowDecimals={false} />
              <Tooltip
                contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
              />
              <Area type="monotone" dataKey="users" stroke="#818cf8" fill="#6366f11f" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Team rollups
        </h2>
        {!teams || teams.length === 0 ? (
          <p className="mt-3 text-sm text-slate-400">No activity recorded yet.</p>
        ) : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-3 py-2">Team</th>
                  <th className="px-3 py-2">Active users</th>
                  {Object.keys(STEP_LABELS).map((step) => (
                    <th key={step} className="px-3 py-2">{STEP_LABELS[step]}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {teams.map((team) => (
                  <tr key={team.team}>
                    <td className="px-3 py-2 text-slate-300">{team.team}</td>
                    <td className="px-3 py-2 text-slate-400">{team.active_users}</td>
                    {Object.keys(STEP_LABELS).map((step) => (
                      <td key={step} className="px-3 py-2 text-slate-400">
                        {team.events[step]?.count ?? 0}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <div className="text-xl font-semibold">{value}</div>
      <div className="mt-0.5 text-xs uppercase tracking-wide text-slate-400">{label}</div>
    </div>
  );
}
