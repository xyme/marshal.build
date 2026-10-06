"use client";

import Link from "next/link";
import type { Project } from "@/lib/types";

/** Composable agents R1.2: the declared dependency graph with live
 *  per-dependency deployment status (enriched server-side). */
export default function CompositionCard({ project }: { project: Project }) {
  const composition = project.composition;
  if (!composition || !(composition.dependencies ?? []).length) return null;
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
      <h2 className="font-medium">
        Composition
        {composition.orchestration && (
          <span className="ml-2 rounded bg-indigo-500/15 px-2 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300">
            {composition.orchestration}
          </span>
        )}
      </h2>
      <p className="mt-1 text-sm text-slate-400">
        This agent calls the deployed agents below. Deploys refuse while a
        dependency is not callable; teardowns of a dependency refuse while
        this agent is live.
      </p>
      <ul className="mt-3 space-y-2">
        {(composition.dependencies ?? []).map((dep) => (
          <li key={dep.slug} className="flex items-center gap-2 text-sm">
            <Link
              href={`/projects/${dep.project_id}`}
              className="text-indigo-300 hover:text-indigo-200"
            >
              {dep.name || dep.slug}
            </Link>
            <code className="rounded bg-slate-950 px-1.5 py-0.5 text-xs text-slate-400">
              {dep.slug}
            </code>
            <span
              className={`ml-auto rounded px-2 py-0.5 text-[10px] uppercase tracking-wide ${
                dep.deployment_status === "active"
                  ? "bg-emerald-500/15 text-emerald-300"
                  : "bg-amber-500/15 text-amber-300"
              }`}
            >
              {dep.deployment_status === "active" ? "active" : "not deployed"}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
