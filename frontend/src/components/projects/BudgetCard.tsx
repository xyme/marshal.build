"use client";

import { useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import type { Project } from "@/lib/types";

/** Per-project monthly budget (cost-caps-alerts spec R4). */
export default function BudgetCard({
  project,
  onChange,
}: {
  project: Project;
  onChange: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(
    project.budget_override_usd != null ? String(project.budget_override_usd) : ""
  );
  const [busy, setBusy] = useState(false);

  const spend = project.cost_mtd_usd ?? 0;
  const budget = project.budget_override_usd;
  const pct = budget ? Math.min((spend / budget) * 100, 100) : null;

  async function save() {
    setBusy(true);
    try {
      await api(`/v1/projects/${project.id}/budget`, {
        method: "PUT",
        body: JSON.stringify({
          budget_override_usd: value.trim() === "" ? null : Number(value),
        }),
      });
      toast.success(value.trim() === "" ? "Budget cleared — platform default applies." : "Budget saved.");
      setEditing(false);
      onChange();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-4">
      <div className="flex items-center justify-between gap-3 text-sm">
        <div>
          <span className="font-medium">Monthly budget</span>
          <span className="ml-3 text-slate-400">
            ${spend.toFixed(2)} spent
            {budget != null ? ` of $${budget.toFixed(0)}` : " · no project budget (platform default applies)"}
          </span>
        </div>
        {editing ? (
          <span className="flex items-center gap-2">
            <input
              type="number"
              min={0}
              step={10}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="default"
              aria-label="Monthly budget in USD"
              className="w-28 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm outline-none focus:border-indigo-500"
            />
            <button
              onClick={save}
              disabled={busy}
              className="rounded-lg bg-indigo-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
            >
              Save
            </button>
            <button
              onClick={() => setEditing(false)}
              className="text-xs text-slate-400 hover:text-slate-200"
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            onClick={() => setEditing(true)}
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs hover:border-slate-500"
          >
            {budget != null ? "Edit budget" : "Set budget"}
          </button>
        )}
      </div>
      {pct != null && (
        <div
          className="mt-3 h-1.5 overflow-hidden rounded bg-slate-800"
          role="progressbar"
          aria-valuenow={Math.round(pct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Budget used"
        >
          <div
            className={`h-full ${pct >= 100 ? "bg-red-500" : pct >= 75 ? "bg-amber-500" : "bg-emerald-500"}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </section>
  );
}
