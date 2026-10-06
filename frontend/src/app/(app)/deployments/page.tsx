"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";

/** Cross-project deployments view (nav "Deployments").
 *  Live defect 17 Sep 2026: the nav item had pointed at /projects since S15
 *  — this page did not exist. Visibility mirrors the projects list (owned ∪
 *  shared ∪ team); deploy/teardown ACTIONS stay on the project's own
 *  deployment tab — this is the fleet view, not a second control surface. */

interface MyDeploymentRow {
  id: string;
  project_id: string;
  project_name: string | null;
  status: string;
  health: string;
  mode: string;
  app_url: string | null;
  mine: boolean;
  deployed_at: string | null;
  expires_at: string | null;
  torn_down_at: string | null;
  created_at: string | null;
}

const STATUS_STYLES: Record<string, string> = {
  active: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  failed: "border-red-500/40 bg-red-500/10 text-red-300",
  torn_down: "border-slate-600 bg-slate-800/40 text-slate-400",
};

function statusChip(status: string) {
  const style =
    STATUS_STYLES[status] ?? "border-indigo-500/40 bg-indigo-500/10 text-indigo-300";
  return (
    <span className={`inline-flex rounded-full border px-2.5 py-0.5 text-xs ${style}`}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

function hoursLeft(expires: string | null): string | null {
  if (!expires) return null;
  const ms = new Date(expires).getTime() - Date.now();
  if (ms <= 0) return "expiring";
  const h = ms / 3_600_000;
  return h < 48 ? `${Math.round(h)}h left` : `${Math.round(h / 24)}d left`;
}

export default function DeploymentsPage() {
  const [rows, setRows] = useState<MyDeploymentRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ deployments: MyDeploymentRow[] }>("/v1/users/me/deployments")
      .then((body) => setRows(body.deployments))
      .catch((e) => setError((e as Error).message));
  }, []);

  if (error) {
    return (
      <div className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-red-300">
        {error}
      </div>
    );
  }
  if (rows === null) return <p className="text-slate-400">Loading…</p>;

  const active = rows.filter((r) => !["torn_down", "failed"].includes(r.status));
  const past = rows.filter((r) => ["torn_down", "failed"].includes(r.status));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Deployments</h1>
        <p className="mt-1 text-sm text-slate-400">
          Every deployment across the projects you can see. Deploy, extend and
          tear down from the project&apos;s own deployment tab.
        </p>
      </div>

      {rows.length === 0 && (
        <section className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-8 text-center text-sm text-slate-400">
          Nothing deployed yet. Build a project, then deploy it from its
          deployment tab —{" "}
          <Link href="/projects" className="text-indigo-300 hover:underline">
            your projects
          </Link>
          .
        </section>
      )}

      {[
        ["Running & in flight", active],
        ["History", past],
      ].map(([label, list]) =>
        (list as MyDeploymentRow[]).length === 0 ? null : (
          <section key={label as string}>
            <h2 className="mb-2 text-sm font-medium text-slate-300">{label as string}</h2>
            <div className="overflow-hidden rounded-xl border border-slate-800">
              <table className="w-full text-sm">
                <thead className="bg-slate-900/60 text-left text-xs text-slate-400">
                  <tr>
                    <th className="px-4 py-2 font-medium">Project</th>
                    <th className="px-4 py-2 font-medium">Status</th>
                    <th className="px-4 py-2 font-medium">Mode</th>
                    <th className="px-4 py-2 font-medium">Lifetime</th>
                    <th className="px-4 py-2 font-medium">App</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60 bg-slate-900/20">
                  {(list as MyDeploymentRow[]).map((row) => (
                    <tr key={row.id}>
                      <td className="px-4 py-2.5">
                        <Link
                          href={`/projects/${row.project_id}?tab=deployment`}
                          className="text-slate-200 hover:text-white hover:underline"
                        >
                          {row.project_name ?? "(unnamed project)"}
                        </Link>
                        {!row.mine && (
                          <span className="ml-2 text-xs text-slate-500">shared</span>
                        )}
                      </td>
                      <td className="px-4 py-2.5">
                        {statusChip(row.status)}
                        {row.status === "active" && row.health === "degraded" && (
                          <span className="ml-2 text-xs text-amber-300">degraded</span>
                        )}
                      </td>
                      <td className="px-4 py-2.5 text-slate-400">
                        {row.mode === "testbed" ? "Testbed" : "Full Governance"}
                      </td>
                      <td className="px-4 py-2.5 text-slate-400">
                        {row.status === "active"
                          ? hoursLeft(row.expires_at) ?? "—"
                          : row.torn_down_at
                            ? `ended ${new Date(row.torn_down_at).toLocaleDateString()}`
                            : row.created_at
                              ? new Date(row.created_at).toLocaleDateString()
                              : "—"}
                      </td>
                      <td className="px-4 py-2.5">
                        {row.app_url && row.status === "active" ? (
                          <a
                            href={row.app_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-emerald-400 hover:underline"
                          >
                            Open ↗
                          </a>
                        ) : (
                          <span className="text-slate-600">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )
      )}
    </div>
  );
}
