"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import { formatUsd } from "@/lib/governance-ui";
import { modelLabel } from "@/lib/marketplace-ui";
import type { CostBreakdown, CostDashboard, GovernanceAlert } from "@/lib/types";

// S16-03 chargeback shapes (mirrors services/costs.py::chargeback)
interface ChargebackItem {
  email: string;
  project: string;
  team: string | null;
  calls: number;
  model_usd: number;
  enclave_usd: number | null;
  total_usd: number;
}
interface ChargebackReport {
  period: string;
  cost_explorer_enabled: boolean;
  items: ChargebackItem[];
  by_team: { team: string; model_usd: number; enclave_usd: number; total_usd: number }[];
  monthly: { period: string; model_usd: number; enclave_usd: number | null; total_usd: number }[];
}

function currentPeriod(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function periodOptions(): string[] {
  const options: string[] = [];
  const now = new Date();
  for (let i = 0; i < 6; i++) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    options.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  }
  return options;
}

const GROUPS = [
  ["user", "By user"],
  ["project", "By project"],
  ["team", "By team"],
  ["model", "By model"],
  ["purpose", "By purpose"],
  ["day", "By day"],
] as const;

/** Admin > Cost Dashboard (FSD §4.6.5, S4-01) — model spend from invocation logs. */
export default function AdminCostsPage() {
  const [period, setPeriod] = useState(currentPeriod());
  const [dash, setDash] = useState<CostDashboard | null>(null);
  const [group, setGroup] = useState<(typeof GROUPS)[number][0]>("user");
  const [breakdown, setBreakdown] = useState<CostBreakdown | null>(null);
  const [chargeback, setChargeback] = useState<ChargebackReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDash(await api<CostDashboard>(`/v1/admin/costs?period=${period}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [period]);

  const loadBreakdown = useCallback(async () => {
    try {
      setBreakdown(
        await api<CostBreakdown>(`/v1/admin/costs/breakdown?period=${period}&group_by=${group}`)
      );
    } catch {
      /* table shows empty */
    }
  }, [period, group]);

  const loadChargeback = useCallback(async () => {
    try {
      setChargeback(
        await api<ChargebackReport>(`/v1/admin/costs/chargeback?period=${period}&months=6`)
      );
    } catch {
      /* section shows empty */
    }
  }, [period]);

  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    loadBreakdown();
  }, [loadBreakdown]);
  useEffect(() => {
    loadChargeback();
  }, [loadChargeback]);

  function exportCsv() {
    if (!breakdown) return;
    const keys = breakdown.items.length ? Object.keys(breakdown.items[0]) : [];
    const rows = [
      keys.join(","),
      ...breakdown.items.map((item) =>
        keys.map((k) => JSON.stringify(item[k] ?? "")).join(",")
      ),
    ];
    const blob = new Blob([rows.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `marshal-costs-${period}-${group}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  if (error && !dash) {
    return <p className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-red-300">{error}</p>;
  }
  if (!dash) return <p className="text-slate-400">Loading…</p>;

  const budgetPct =
    dash.budget_usd != null && dash.budget_usd > 0
      ? Math.min((dash.total_usd / dash.budget_usd) * 100, 100)
      : null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Cost Dashboard</h1>
          <p className="mt-1 text-sm text-slate-400">
            Model spend from platform invocation logs. Enclave infrastructure spend arrives with
            ISB cost surfacing (S5).
          </p>
        </div>
        <select
          value={period}
          onChange={(e) => setPeriod(e.target.value)}
          aria-label="Billing period"
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
        >
          {periodOptions().map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Total model spend" value={formatUsd(dash.total_usd, 4)} />
        <StatCard
          label="Budget"
          value={dash.budget_usd != null ? formatUsd(dash.budget_usd) : "not set"}
          sub={
            budgetPct != null
              ? `${budgetPct.toFixed(1)}% used`
              : "Set one in Model Controls"
          }
        />
        <StatCard label="Projected month-end" value={formatUsd(dash.projected_usd)} />
        <StatCard
          label="Calls"
          value={String(dash.calls)}
          sub={
            dash.unpriced_calls > 0
              ? `${dash.unpriced_calls} unpriced`
              : `${((dash.input_tokens + dash.output_tokens) / 1000).toFixed(1)}k tokens`
          }
        />
      </div>

      {budgetPct != null && (
        <div
          className="h-2 overflow-hidden rounded bg-slate-800"
          role="progressbar"
          aria-valuenow={Math.round(budgetPct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Budget used"
        >
          <div
            className={`h-full ${budgetPct > 90 ? "bg-red-500" : budgetPct > 70 ? "bg-amber-500" : "bg-emerald-500"}`}
            style={{ width: `${budgetPct}%` }}
          />
        </div>
      )}

      <AlertsStrip alerts={(dash as CostDashboard & { alerts?: GovernanceAlert[] }).alerts ?? []} onChange={load} />

      <TeamBudgetsSection period={period} />

      {(dash as CostDashboard & { sandbox_spend_usd?: number | null }).sandbox_spend_usd != null && (
        <p className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-3 text-sm text-slate-400">
          Enclave infrastructure spend:{" "}
          <strong className="text-slate-200">
            {formatUsd((dash as CostDashboard & { sandbox_spend_usd?: number }).sandbox_spend_usd)}
          </strong>{" "}
          all-time
          {(dash as CostDashboard & { enclave_usd?: number | null }).enclave_usd != null && (
            <>
              {" · "}
              <strong className="text-slate-200">
                {formatUsd((dash as CostDashboard & { enclave_usd?: number }).enclave_usd)}
              </strong>{" "}
              this period
            </>
          )}{" "}
          — informational; not counted against model-spend caps (S12 R4).
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
        <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
          <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
            Spend over time
          </h2>
          <div className="mt-3 h-56">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={dash.daily} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis
                  dataKey="date"
                  tickFormatter={(d: string) => d.slice(8)}
                  stroke="#475569"
                  fontSize={11}
                />
                <YAxis stroke="#475569" fontSize={11} tickFormatter={(v: number) => `$${v}`} />
                <Tooltip
                  contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
                  formatter={(value) => [`$${Number(value).toFixed(4)}`, "spend"]}
                />
                <Area type="monotone" dataKey="usd" stroke="#818cf8" fill="#6366f11f" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </section>

        <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
          <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">By model</h2>
          <div className="mt-3 h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={dash.by_model.map((m) => ({ ...m, label: modelLabel(m.model_id) }))}
                layout="vertical"
                margin={{ top: 4, right: 8, bottom: 0, left: 8 }}
              >
                <XAxis type="number" stroke="#475569" fontSize={11} tickFormatter={(v: number) => `$${v}`} />
                <YAxis type="category" dataKey="label" stroke="#475569" fontSize={11} width={64} />
                <Tooltip
                  contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
                  formatter={(value) => [`$${Number(value).toFixed(4)}`, "spend"]}
                />
                <Bar dataKey="usd" fill="#818cf8" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>
      </div>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex gap-1 text-sm">
            {GROUPS.map(([key, label]) => (
              <button
                key={key}
                onClick={() => setGroup(key)}
                className={`rounded-md px-3 py-1.5 ${
                  group === key
                    ? "bg-slate-800 text-white"
                    : "text-slate-400 hover:bg-slate-900 hover:text-slate-200"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            onClick={exportCsv}
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-slate-500"
          >
            Export CSV
          </button>
        </div>
        {breakdown === null || breakdown.items.length === 0 ? (
          <p className="mt-4 text-sm text-slate-400">No spend recorded for this pivot yet.</p>
        ) : (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  {Object.keys(breakdown.items[0])
                    .filter((k) => !k.endsWith("_id"))
                    .map((k) => (
                      <th key={k} className="px-3 py-2">
                        {k.replace(/_/g, " ")}
                      </th>
                    ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {breakdown.items.map((item, i) => (
                  <tr key={i}>
                    {Object.entries(item)
                      .filter(([k]) => !k.endsWith("_id"))
                      .map(([k, v]) => (
                        <td key={k} className="px-3 py-2 text-slate-300">
                          {k === "usd" ? formatUsd(Number(v), 4)
                            : k === "model_id" || k === "model" ? modelLabel(String(v))
                            : String(v ?? "—")}
                        </td>
                      ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* S16-03 chargeback/showback: who spent what, on which project, in
          which team — the Preview commercial-validation substrate. */}
      {chargeback && (
        <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
                Chargeback — {chargeback.period}
              </h2>
              <p className="mt-1 text-xs text-slate-400">
                Model spend attributes to the caller; Enclave infrastructure spend to the
                project owner.{" "}
                {chargeback.cost_explorer_enabled
                  ? "Cost Explorer live: infrastructure spend included."
                  : "Infrastructure spend appears once Cost Explorer is enabled [D3]."}
              </p>
            </div>
            <a
              href={`/api/backend/v1/admin/costs/chargeback?period=${period}&format=csv`}
              className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-slate-500"
            >
              Export CSV
            </a>
          </div>

          {chargeback.by_team.length > 0 && (
            <div className="mt-4 flex flex-wrap gap-2">
              {chargeback.by_team.map((team) => (
                <span
                  key={team.team}
                  className="rounded-full border border-slate-700 bg-slate-950/60 px-3 py-1 text-xs text-slate-300"
                >
                  {team.team}: <strong>{formatUsd(team.total_usd, 4)}</strong>
                </span>
              ))}
            </div>
          )}

          {chargeback.items.length === 0 ? (
            <p className="mt-4 text-sm text-slate-400">No spend recorded this period.</p>
          ) : (
            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
                  <tr>
                    <th className="px-3 py-2">User</th>
                    <th className="px-3 py-2">Project</th>
                    <th className="px-3 py-2">Team</th>
                    <th className="px-3 py-2">Calls</th>
                    <th className="px-3 py-2">Model</th>
                    <th className="px-3 py-2">Enclave</th>
                    <th className="px-3 py-2">Total</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {chargeback.items.map((item, i) => (
                    <tr key={i}>
                      <td className="px-3 py-2 text-slate-300">{item.email}</td>
                      <td className="px-3 py-2 text-slate-400">{item.project}</td>
                      <td className="px-3 py-2 text-slate-400">{item.team ?? "—"}</td>
                      <td className="px-3 py-2 text-slate-400">{item.calls}</td>
                      <td className="px-3 py-2 text-slate-400">{formatUsd(item.model_usd, 4)}</td>
                      <td className="px-3 py-2 text-slate-400">
                        {item.enclave_usd != null ? formatUsd(item.enclave_usd) : "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-300">{formatUsd(item.total_usd, 4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {chargeback.monthly.length > 1 && (
            <div className="mt-5">
              <h3 className="text-xs font-medium uppercase tracking-wide text-slate-400">
                Monthly rollups
              </h3>
              <div className="mt-2 flex flex-wrap gap-2">
                {chargeback.monthly.map((month) => (
                  <span
                    key={month.period}
                    className="rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-1.5 text-xs text-slate-300"
                  >
                    {month.period}: <strong>{formatUsd(month.total_usd, 4)}</strong>
                    {month.enclave_usd != null && (
                      <span className="text-slate-400"> (incl. {formatUsd(month.enclave_usd)} infra)</span>
                    )}
                  </span>
                ))}
              </div>
            </div>
          )}
        </section>
      )}

      {dash.top_spenders.length > 0 && (
        <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
          <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
            Top spenders
          </h2>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-3 py-2">User</th>
                  <th className="px-3 py-2">Projects</th>
                  <th className="px-3 py-2">Calls</th>
                  <th className="px-3 py-2">Tokens</th>
                  <th className="px-3 py-2">Spend</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {dash.top_spenders.map((s) => (
                  <tr key={s.email}>
                    <td className="px-3 py-2 text-slate-300">{s.email}</td>
                    <td className="px-3 py-2 text-slate-400">{s.projects}</td>
                    <td className="px-3 py-2 text-slate-400">{s.calls}</td>
                    <td className="px-3 py-2 text-slate-400">{(s.tokens / 1000).toFixed(1)}k</td>
                    <td className="px-3 py-2 text-slate-300">{formatUsd(s.usd, 4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

const SEVERITY_STYLE: Record<string, string> = {
  info: "border-slate-700 text-slate-300",
  warning: "border-amber-500/50 text-amber-300",
  critical: "border-red-500/50 text-red-300",
};

function AlertsStrip({ alerts, onChange }: { alerts: GovernanceAlert[]; onChange: () => void }) {
  const readonly = useAdminReadonly();
  if (alerts.length === 0) return null;
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
      <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">Alerts</h2>
      <ul className="mt-2 space-y-2">
        {alerts.map((alert) => (
          <li
            key={alert.id}
            className={`flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm ${SEVERITY_STYLE[alert.severity]}`}
          >
            <span>
              {alert.severity === "critical" ? "⛔" : "⚠️"} {alert.message}
            </span>
            <button
              onClick={async () => {
                await api(`/v1/admin/alerts/${alert.id}/acknowledge`, { method: "PUT" }).catch(() => {});
                onChange();
              }}
              disabled={readonly}
              className="shrink-0 rounded-md border border-slate-700 px-2 py-1 text-xs hover:border-slate-500 disabled:opacity-40"
            >
              Acknowledge
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <div className="text-xl font-semibold">{value}</div>
      <div className="mt-0.5 text-xs uppercase tracking-wide text-slate-400">{label}</div>
      {sub && <div className="mt-1 text-xs text-slate-400">{sub}</div>}
    </div>
  );
}

// ------------------------------------------- team budgets vs spend (5 Sep)

interface TeamCostRow {
  team_id: string | null;
  team: string;
  status: string;
  budget_usd: number | null;
  model_usd: number;
  enclave_usd: number | null;
  total_usd: number;
  pct_used: number | null;
}

/** Per-team budget vs spend. Budgets are set on Admin → Teams; this view is
 *  accountability, not enforcement — caps stay on users/projects/platform. */
function TeamBudgetsSection({ period }: { period: string }) {
  const [rows, setRows] = useState<TeamCostRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ items: TeamCostRow[] }>(`/v1/admin/costs/teams?period=${period}`)
      .then((body) => {
        setRows(body.items);
        setError(null);
      })
      .catch((e) => setError((e as Error).message));
  }, [period]);

  if (error) {
    return (
      <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
        Team budgets: {error}
      </p>
    );
  }
  if (rows === null || rows.length === 0) return null;

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Team budgets — {period}
        </h2>
        <span className="text-xs text-slate-400">
          Budgets are set on Admin → Teams. Accountability view — enforcement stays on
          user/project/platform caps.
        </span>
      </div>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
            <tr>
              <th className="px-3 py-2">Team</th>
              <th className="px-3 py-2">Budget</th>
              <th className="px-3 py-2">Spend</th>
              <th className="px-3 py-2">Used</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {rows.map((row) => {
              const pct = row.pct_used;
              const barColor =
                pct == null
                  ? "bg-slate-600"
                  : pct >= 100
                    ? "bg-red-500"
                    : pct >= 75
                      ? "bg-amber-500"
                      : "bg-emerald-500";
              return (
                <tr key={row.team}>
                  <td className="px-3 py-2 text-slate-300">
                    {row.team}
                    {row.status === "archived" && (
                      <span className="ml-2 rounded bg-slate-700/60 px-1.5 py-0.5 text-[10px] uppercase text-slate-300">
                        archived
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-slate-300">
                    {row.budget_usd != null ? formatUsd(row.budget_usd) : "—"}
                  </td>
                  <td className="px-3 py-2 text-slate-300" title={
                    row.enclave_usd != null
                      ? `model ${formatUsd(row.model_usd, 4)} + enclave ${formatUsd(row.enclave_usd)}`
                      : undefined
                  }>
                    {formatUsd(row.total_usd, 4)}
                  </td>
                  <td className="px-3 py-2">
                    {pct == null ? (
                      <span className="text-xs text-slate-400">no budget</span>
                    ) : (
                      <span className="flex items-center gap-2">
                        <span
                          className="h-2 w-28 overflow-hidden rounded bg-slate-800"
                          role="progressbar"
                          aria-valuenow={Math.min(Math.round(pct), 100)}
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-label={`${row.team} budget used`}
                        >
                          <span
                            className={`block h-full ${barColor}`}
                            style={{ width: `${Math.min(pct, 100)}%` }}
                          />
                        </span>
                        <span
                          className={`text-xs ${
                            pct >= 100 ? "font-medium text-red-300" : "text-slate-400"
                          }`}
                        >
                          {pct.toFixed(1)}%
                        </span>
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
