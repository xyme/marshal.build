"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import BuildTab from "@/components/build/BuildTab";
import DeploymentPanel from "@/components/deploy/DeploymentPanel";
import ActivityTab from "@/components/projects/ActivityTab";
import BudgetCard from "@/components/projects/BudgetCard";
import TeamCard from "@/components/projects/TeamCard";
import PresenceDots from "@/components/projects/PresenceDots";
import { AssessmentDrawer } from "@/components/projects/RiskBadge";
import CompositionCard from "@/components/projects/CompositionCard";
import ShareModal from "@/components/projects/ShareModal";
import SubmitToMarketplaceModal, {
  useMySubmissionFor,
} from "@/components/projects/SubmitToMarketplace";
import SpecWorkspace from "@/components/spec/SpecWorkspace";
import { useUser } from "@/components/user-context";
import { api } from "@/lib/api";
import { popForkWarnings } from "@/lib/marketplace-ui";
import type { Project } from "@/lib/types";

export default function ProjectDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { user } = useUser();
  const searchParams = useSearchParams();
  const [project, setProject] = useState<Project | null>(null);
  const initialTab = searchParams.get("tab");
  const [tab, setTab] = useState<"spec" | "build" | "deployment" | "activity">(
    initialTab === "spec" ? "spec" : initialTab === "build" ? "build" : "deployment"
  );
  const [deployBuildId, setDeployBuildId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forkInfo, setForkInfo] = useState<{ warnings: string[]; sampleTitle: string } | null>(null);
  const [showShare, setShowShare] = useState(false);
  const [showSubmit, setShowSubmit] = useState(false);
  const { submission, refresh: refreshSubmission } = useMySubmissionFor(id);

  useEffect(() => {
    setForkInfo(popForkWarnings(id));
  }, [id]);

  const refresh = useCallback(async () => {
    try {
      setProject(await api<Project>(`/v1/projects/${id}`));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (error) {
    return (
      <div className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-red-300">
        {error} — <Link href="/projects" className="underline">back to projects</Link>
      </div>
    );
  }
  if (!project) return <p className="text-slate-400">Loading…</p>;

  const myRole = project.my_role ?? "owner";
  const isOwner = myRole === "owner";
  const canSubmit =
    isOwner && (user?.role === "admin" || user?.persona === "power") && !submission;

  return (
    <div className="space-y-6">
      <div>
        <Link href="/projects" className="text-sm text-slate-400 hover:text-slate-300">
          ← Projects
        </Link>
        <div className="mt-1 flex items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">{project.name}</h1>
          <span className="rounded bg-slate-800 px-2 py-0.5 text-xs uppercase tracking-wide text-slate-400">
            {project.status.replace(/_/g, " ")}
          </span>
          {!isOwner && (
            <span className="rounded bg-indigo-500/15 px-2 py-0.5 text-xs uppercase tracking-wide text-indigo-300">
              Shared · {myRole}
            </span>
          )}
          {submission && (
            <span
              className="rounded bg-amber-500/15 px-2 py-0.5 text-xs uppercase tracking-wide text-amber-300"
              title="This project has a marketplace submission under review"
            >
              Submitted for review
            </span>
          )}
          <div className="ml-auto flex items-center gap-3">
            <PresenceDots
              projectId={project.id}
              surface={tab === "spec" ? "spec" : "project"}
              selfId={user?.id}
            />
            {isOwner && (
              <button
                onClick={() => setShowShare(true)}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-slate-500"
              >
                👥 Share
              </button>
            )}
            {canSubmit && project.spec_version_count > 0 && (
              <button
                onClick={() => setShowSubmit(true)}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-slate-500"
                title="Share this project as a marketplace sample"
              >
                🏪 Submit to marketplace
              </button>
            )}
            <a
              href={`/api/backend/v1/projects/${project.id}/export`}
              download
              title={
                project.spec_version_count === 0
                  ? "Save a spec first"
                  : "Download the latest spec set as a portable zip (.kiro/specs layout)"
              }
              aria-disabled={project.spec_version_count === 0}
              className={`rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-slate-500 ${
                project.spec_version_count === 0 ? "pointer-events-none opacity-40" : ""
              }`}
            >
              📤 Export spec
            </a>
          </div>
        </div>
        {project.description && (
          <p className="mt-1 text-sm text-slate-400">{project.description}</p>
        )}
      </div>

      {forkInfo && (
        <div className="flex items-start justify-between gap-3 rounded-md border border-indigo-500/40 bg-indigo-500/10 px-4 py-3 text-sm text-indigo-200">
          <div>
            <p>
              Forked from &ldquo;{forkInfo.sampleTitle}&rdquo; — customize it to your needs.
            </p>
            {forkInfo.warnings.map((w) => (
              <p key={w} className="mt-1 text-amber-300">⚠ {w}</p>
            ))}
          </div>
          <button
            onClick={() => setForkInfo(null)}
            aria-label="Dismiss"
            className="shrink-0 text-indigo-300 hover:text-white"
          >
            ✕
          </button>
        </div>
      )}

      {project.template_deprecated && !forkInfo && (
        <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-300">
          This project&apos;s template &ldquo;{project.template_name}&rdquo; is deprecated.
          Existing behavior is unaffected; consider migrating to an active template.
        </p>
      )}

      <div className="flex gap-1 border-b border-slate-800">
        {(["spec", "build", "deployment", "activity"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`rounded-t-lg px-4 py-2 text-sm capitalize ${
              tab === t
                ? "border border-b-0 border-slate-800 bg-slate-900/40 text-white"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "spec" ? (
        <SpecWorkspace projectId={project.id} myRole={myRole} />
      ) : tab === "build" ? (
        <BuildTab
          projectId={project.id}
          myRole={myRole}
          onDeployBuild={(buildId) => {
            setDeployBuildId(buildId);
            setTab("deployment");
          }}
        />
      ) : tab === "activity" ? (
        <ActivityTab projectId={project.id} />
      ) : (
        <div className="space-y-4">
          <AssessmentDrawer projectId={project.id} />
          <CompositionCard project={project} />
          <TeamCard project={project} isOwner={isOwner} onChange={refresh} />
          {isOwner ? (
            <BudgetCard project={project} onChange={refresh} />
          ) : (
            <section className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-4 text-sm text-slate-400">
              Monthly budget:{" "}
              {project.budget_override_usd != null
                ? `$${Number(project.budget_override_usd).toFixed(0)}`
                : "platform default"}{" "}
              · managed by the project owner
            </section>
          )}
          {isOwner ? (
            <DeploymentPanel
              project={project}
              onChange={refresh}
              preselectedBuildId={deployBuildId}
            />
          ) : (
            <section className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-4 text-sm">
              <p className="font-medium">Deployment</p>
              {project.deployment ? (
                <p className="mt-2 text-slate-400">
                  Status: {project.deployment.status.replace(/_/g, " ")}
                  {project.deployment.app_url && project.deployment.status === "active" && (
                    <>
                      {" · "}
                      <a
                        href={project.deployment.app_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-emerald-400 hover:underline"
                      >
                        Open app ↗
                      </a>
                    </>
                  )}
                </p>
              ) : (
                <p className="mt-2 text-slate-400">No deployment yet.</p>
              )}
              <p className="mt-2 text-xs text-slate-400">
                Deploy and teardown are owner actions.
              </p>
            </section>
          )}
        </div>
      )}

      {showShare && (
        <ShareModal
          project={project}
          onClose={() => setShowShare(false)}
          onChanged={refresh}
        />
      )}
      {showSubmit && (
        <SubmitToMarketplaceModal
          project={project}
          onClose={() => setShowSubmit(false)}
          onSubmitted={refreshSubmission}
        />
      )}
    </div>
  );
}
