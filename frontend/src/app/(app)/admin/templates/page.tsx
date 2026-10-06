"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useUser } from "@/components/user-context";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import type { TemplateAdmin } from "@/lib/types";

const STATUS_STYLES: Record<string, string> = {
  draft: "bg-slate-700/40 text-slate-300",
  active: "bg-emerald-500/15 text-emerald-400",
  deprecated: "bg-amber-500/15 text-amber-400",
};

/** Admin > Templates list (S2-04, FSD §4.2.3). */
export default function AdminTemplatesPage() {
  const readonly = useAdminReadonly();
  const { user } = useUser();
  const [templates, setTemplates] = useState<TemplateAdmin[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api<TemplateAdmin[]>("/v1/admin/templates").then(setTemplates).catch((e) => setError(e.message));

  useEffect(() => {
    load();
  }, []);

  // Mirror the admin-layout guard: view-only admin accounts (product decision,
  // 4 Sep 2026) read this surface; the backend refuses their mutations.
  if (user && user.role !== "admin" && !user.admin_readonly) {
    return (
      <p className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-red-300">
        Admin role required.
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Templates</h1>
          <p className="mt-1 text-sm text-slate-400">
            Governance templates with model guardrails — users build within these rails.
          </p>
        </div>
        {!readonly && (
          <Link
            href="/admin/templates/new"
            className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400"
          >
            + New Template
          </Link>
        )}
      </div>

      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {templates === null ? (
        <p className="text-slate-400">Loading…</p>
      ) : templates.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 p-10 text-center text-slate-400">
          No templates yet. Create one to give users a governed starting point.
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/80 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">Category</th>
                <th className="px-4 py-3">Version</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Usage</th>
                <th className="px-4 py-3">Updated</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {templates.map((t) => (
                <tr key={t.id} className="bg-slate-900/40 hover:bg-slate-900/70">
                  <td className="px-4 py-3">
                    <Link href={`/admin/templates/${t.id}`} className="font-medium text-indigo-300 hover:text-indigo-200">
                      {t.name}
                    </Link>
                  </td>
                  <td className="px-4 py-3 capitalize text-slate-400">
                    {t.category.replace(/_/g, " ")}
                  </td>
                  <td className="px-4 py-3 font-mono text-slate-400">v{t.version}</td>
                  <td className="px-4 py-3">
                    <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${STATUS_STYLES[t.status]}`}>
                      {t.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-400">{t.usage_count}</td>
                  <td className="px-4 py-3 text-slate-400">
                    {new Date(t.updated_at).toLocaleDateString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
