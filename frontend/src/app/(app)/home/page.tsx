"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import AppTour from "@/components/tour/AppTour";
import { useUser } from "@/components/user-context";
import { api } from "@/lib/api";
import type { ChatSession, Project, ProjectList, UserStats, Workbench } from "@/lib/types";

export default function HomePage() {
  const { user } = useUser();
  const [stats, setStats] = useState<UserStats | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [workbench, setWorkbench] = useState<Workbench | null>(null);

  const [resumeSession, setResumeSession] = useState<ChatSession | null>(null);

  useEffect(() => {
    api<UserStats>("/v1/users/me/stats").then(setStats).catch(() => {});
    // Needs-your-attention strip (B14) — fire-and-forget like the rest of home
    api<Workbench>("/v1/users/me/workbench").then(setWorkbench).catch(() => {});
    api<ProjectList>("/v1/projects?page_size=6")
      .then((list) => setProjects(list.items))
      .catch(() => {});
    // Resume card (S2-07): most recent session touched in the last 7 days
    api<ChatSession[]>("/v1/chat/sessions")
      .then((sessions) => {
        const last = sessions[0];
        if (
          last &&
          Date.now() - new Date(last.updated_at).getTime() < 7 * 24 * 3600 * 1000
        ) {
          setResumeSession(last);
        }
      })
      .catch(() => {});
  }, []);

  const business = user?.persona !== "power";
  const firstName = user?.name?.split(" ")[0] ?? "there";

  return (
    <div className="space-y-8">
      <Suspense fallback={null}>
        <AppTour />
      </Suspense>
      <section>
        <h1 className="text-2xl font-semibold tracking-tight">
          Welcome back, {firstName}
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          {business
            ? "Describe an idea in plain English and marshal will build the plan with you."
            : "Generate specs from chat, then deploy to a governed Enclave."}
          <span className="ml-2 text-xs uppercase tracking-widest text-indigo-400/80">
            Build · Govern · Deploy
          </span>
        </p>
      </section>

      {workbench && <AttentionStrip workbench={workbench} business={business} admin={user?.role === "admin"} />}

      {resumeSession && (
        <section className="flex items-center justify-between rounded-xl border border-indigo-500/40 bg-indigo-500/5 px-5 py-4">
          <div className="min-w-0">
            <h2 className="font-medium">Resume where you left off?</h2>
            <p className="mt-0.5 truncate text-sm text-slate-400">
              &ldquo;{resumeSession.title}&rdquo; ·{" "}
              {new Date(resumeSession.updated_at).toLocaleString()}
            </p>
          </div>
          <Link
            href="/chat"
            className="shrink-0 rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400"
          >
            Resume →
          </Link>
        </section>
      )}

      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Link
          href="/chat"
          className="group rounded-xl border border-slate-800 bg-slate-900/60 p-5 transition hover:border-indigo-500/60"
        >
          <div className="text-2xl">💬</div>
          <h2 className="mt-3 font-medium">
            {business ? "Build your app" : "AI Chat"}
          </h2>
          <p className="mt-1 text-sm text-slate-400">
            {business
              ? "Chat with marshal about your idea"
              : "Freeform chat with live spec context"}
          </p>
        </Link>
        <div
          data-tour="generate"
          className="rounded-xl border border-slate-800 bg-slate-900/60 p-5"
        >
          <div className="text-2xl">📝</div>
          <h2 className="mt-3 font-medium">
            {business ? "Get your plan" : "Generate requirements"}
          </h2>
          <p className="mt-1 text-sm text-slate-400">
            One click turns your chat into a versioned requirements.md
          </p>
        </div>
        <Link
          href="/projects"
          className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 transition hover:border-indigo-500/60"
        >
          <div className="text-2xl">🗂️</div>
          <h2 className="mt-3 font-medium">My Projects</h2>
          <p className="mt-1 text-sm text-slate-400">
            {stats ? `${stats.projects} projects · ${stats.specs} specs` : "…"}
          </p>
        </Link>
        <div
          data-tour="deploy-card"
          className="rounded-xl border border-slate-800 bg-slate-900/60 p-5"
        >
          <div className="text-2xl">🚀</div>
          <h2 className="mt-3 font-medium">Deploy to Enclave</h2>
          <p className="mt-1 text-sm text-slate-400">
            One-click deployment into an isolated AWS account
          </p>
        </div>
      </section>

      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-medium">Recent projects</h2>
          <Link href="/projects" className="text-sm text-indigo-400 hover:text-indigo-300">
            View all →
          </Link>
        </div>
        {projects.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-800 p-8 text-center text-sm text-slate-400">
            No projects yet. Start a chat and generate your first requirements document.
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {projects.slice(0, 6).map((project) => (
              <Link
                key={project.id}
                href={`/projects/${project.id}`}
                className="rounded-lg border border-slate-800 bg-slate-900/60 p-4 transition hover:border-slate-600"
              >
                <div className="flex items-center justify-between">
                  <span className="truncate font-medium">{project.name}</span>
                  <StatusBadge status={project.deployment?.status ?? project.status} />
                </div>
                <p className="mt-1 truncate text-xs text-slate-400">
                  {project.description ?? "No description"}
                </p>
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

/** Needs-your-attention strip (beta-usability R1.3/R1.4 — FSD B14): sections
 * render only when non-empty, each row links to the acting surface. Business
 * personas see review asks + their own held resubmissions; power adds
 * deployments + builds; the ops line is admin-only. */
function AttentionStrip({
  workbench,
  business,
  admin,
}: {
  workbench: Workbench;
  business: boolean;
  admin: boolean;
}) {
  const rows: { key: string; tone: string; icon: string; text: string; cta: string; href: string }[] = [];

  if (workbench.reviews.is_reviewer && workbench.reviews.pending > 0) {
    rows.push({
      key: "reviews",
      tone: "border-indigo-500/40 bg-indigo-500/5",
      icon: "🛡️",
      text: `${workbench.reviews.pending} risk review${workbench.reviews.pending === 1 ? "" : "s"} waiting on your group`,
      cta: "Review",
      href: "/reviews",
    });
  }
  for (const item of workbench.awaiting_resubmission) {
    rows.push({
      key: `resub-${item.project_id}`,
      tone: "border-amber-500/40 bg-amber-500/5",
      icon: "📝",
      text: `Reviewers requested changes on "${item.project_name ?? "a project"}" — revise the spec to re-enter review`,
      cta: "Open project",
      href: `/projects/${item.project_id}`,
    });
  }
  if (!business) {
    for (const item of workbench.expiring_deployments) {
      rows.push({
        key: `exp-${item.project_id}`,
        tone: "border-amber-500/40 bg-amber-500/5",
        icon: "⏳",
        text: `"${item.project_name ?? "A deployment"}" expires in ${item.hours_left}h — extend it or let it retire`,
        cta: "Deployment",
        href: `/projects/${item.project_id}`,
      });
    }
    for (const item of workbench.failed_builds) {
      rows.push({
        key: `build-${item.build_id}`,
        tone: "border-red-500/40 bg-red-500/5",
        icon: "🧱",
        text: `Build failed on "${item.project_name ?? "a project"}"${item.error_code ? ` (${item.error_code.replace(/_/g, " ")})` : ""}`,
        cta: "Build tab",
        href: `/projects/${item.project_id}?tab=build`,
      });
    }
  }
  if (admin && workbench.ops && workbench.ops.active_alerts > 0) {
    rows.push({
      key: "ops",
      tone:
        workbench.ops.max_severity === "critical"
          ? "border-red-500/40 bg-red-500/5"
          : "border-amber-500/40 bg-amber-500/5",
      icon: "🚨",
      text: `${workbench.ops.active_alerts} active platform alert${workbench.ops.active_alerts === 1 ? "" : "s"} (${workbench.ops.max_severity})`,
      cta: "Costs & alerts",
      href: "/admin/costs",
    });
  }

  if (rows.length === 0) return null;
  return (
    <section aria-label="Needs your attention" data-testid="attention-strip">
      <h2 className="mb-3 font-medium">Needs your attention</h2>
      <div className="space-y-2">
        {rows.map((row) => (
          <Link
            key={row.key}
            href={row.href}
            className={`flex items-center justify-between gap-3 rounded-xl border px-5 py-3 transition hover:brightness-125 ${row.tone}`}
          >
            <span className="flex min-w-0 items-center gap-3 text-sm">
              <span aria-hidden>{row.icon}</span>
              <span className="truncate text-slate-200">{row.text}</span>
            </span>
            <span className="shrink-0 text-sm text-indigo-400">{row.cta} →</span>
          </Link>
        ))}
      </div>
    </section>
  );
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: "bg-emerald-500/15 text-emerald-400",
    deploying: "bg-amber-500/15 text-amber-400",
    failed: "bg-red-500/15 text-red-400",
    spec_complete: "bg-indigo-500/15 text-indigo-300",
    draft: "bg-slate-700/40 text-slate-400",
  };
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
        styles[status] ?? "bg-slate-700/40 text-slate-400"
      }`}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}
