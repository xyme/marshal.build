"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { RiskBadge } from "@/components/projects/RiskBadge";
import { useUser } from "@/components/user-context";
import { api } from "@/lib/api";
import { formatUsd } from "@/lib/governance-ui";
import type { Project, ProjectList, TemplateUser } from "@/lib/types";

const STATUS_OPTIONS = [
  ["", "All active"],
  ["draft", "Draft"],
  ["spec_complete", "Spec complete"],
  ["building", "Building"],
  ["deployed", "Deployed"],
  ["inactive", "Inactive"],
  ["archived", "Archived"],
] as const;

const PROJECT_STATUS_STYLE: Record<string, string> = {
  draft: "bg-slate-700/40 text-slate-400",
  spec_complete: "bg-indigo-500/15 text-indigo-300",
  // S15-06 fix: `animate-pulse` used to sit on the whole badge, which fades the
  // TEXT too — mid-animation the amber dropped to 3.21:1 against the card
  // (AA needs 4.5:1), so the axe scan failed intermittently depending on where
  // the animation was when it sampled. The motion now lives on a dot instead,
  // and the label itself is a fixed, compliant amber-300.
  building: "bg-amber-500/15 text-amber-300",
  deployed: "bg-emerald-500/15 text-emerald-400",
  inactive: "bg-slate-700/40 text-slate-400",
  archived: "bg-slate-800/80 text-slate-400",
};

function ProjectStatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
        PROJECT_STATUS_STYLE[status] ?? "bg-slate-700/40 text-slate-400"
      }`}
    >
      {status === "building" && (
        <span
          className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-amber-300"
          aria-hidden="true"
        />
      )}
      {status.replace(/_/g, " ")}
    </span>
  );
}

function relTime(iso: string | null): string {
  if (!iso) return "—";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days < 30 ? `${days}d ago` : new Date(iso).toLocaleDateString();
}

function ProjectsInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const status = searchParams.get("status") ?? "";
  const sort = searchParams.get("sort") ?? "recent";

  const [q, setQ] = useState(searchParams.get("q") ?? "");
  const [view, setView] = useState<"cards" | "list">("cards");
  const [data, setData] = useState<ProjectList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Project | null>(null);
  const [deleteText, setDeleteText] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [templates, setTemplates] = useState<TemplateUser[]>([]);
  const [templateId, setTemplateId] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const stored = localStorage.getItem("projects-view");
    if (stored === "list" || stored === "cards") setView(stored);
  }, []);

  const setParam = useCallback(
    (key: string, value: string) => {
      const params = new URLSearchParams(searchParams.toString());
      if (value) params.set(key, value);
      else params.delete(key);
      router.replace(`/projects?${params.toString()}`);
    },
    [router, searchParams]
  );

  const load = useCallback(async () => {
    try {
      const params = new URLSearchParams({ sort, page_size: "60" });
      const qParam = searchParams.get("q");
      if (qParam) params.set("q", qParam);
      if (status) params.set("status", status);
      setData(await api<ProjectList>(`/v1/projects?${params}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [searchParams, status, sort]);

  useEffect(() => {
    load();
    api<TemplateUser[]>("/v1/templates").then(setTemplates).catch(() => {});
  }, [load]);

  function switchView(v: "cards" | "list") {
    setView(v);
    localStorage.setItem("projects-view", v);
  }

  function onSearchChange(value: string) {
    setQ(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setParam("q", value.trim()), 300);
  }

  const { user } = useUser();
  // Import is Power-User surface area — the same line spec editing draws.
  const canImport = user?.persona === "power" || user?.role === "admin";
  const [showImport, setShowImport] = useState(false);

  async function createProject(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    try {
      const project = await api<Project>("/v1/projects", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          description: description.trim() || null,
          template_id: templateId,
        }),
      });
      setShowCreate(false);
      setName("");
      setDescription("");
      setTemplateId(null);
      router.push(`/projects/${project.id}`);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function archive(project: Project) {
    try {
      await api(`/v1/projects/${project.id}/archive`, { method: "POST" });
      toast.success(`"${project.name}" archived.`, {
        action: {
          label: "Undo",
          onClick: async () => {
            await api(`/v1/projects/${project.id}/restore`, { method: "POST" }).catch(() => {});
            load();
          },
        },
      });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function restore(project: Project) {
    try {
      const restored = await api<Project>(`/v1/projects/${project.id}/restore`, {
        method: "POST",
      });
      setNotice(
        restored.template_deprecated
          ? `"${project.name}" restored — its template is deprecated; see the project page.`
          : `"${project.name}" restored.`
      );
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function doDelete() {
    if (!confirmDelete) return;
    try {
      await api(`/v1/projects/${confirmDelete.id}`, { method: "DELETE" });
      setNotice(`"${confirmDelete.name}" deleted.`);
      setConfirmDelete(null);
      setDeleteText("");
      load();
    } catch (e) {
      setConfirmDelete(null);
      setDeleteText("");
      setError((e as Error).message);
    }
  }

  const projects = data?.items ?? null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">My Projects</h1>
        <div className="flex items-center gap-2">
          {canImport && (
            <button
              onClick={() => setShowImport(true)}
              className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:border-slate-500"
            >
              Import
            </button>
          )}
          <button
            onClick={() => setShowCreate(true)}
            className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400"
          >
            + New Project
          </button>
        </div>
      </div>

      {showImport && <ImportProjectModal onClose={() => setShowImport(false)} />}

      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-800 bg-slate-900/40 p-3 text-sm">
        <div className="flex overflow-hidden rounded-lg border border-slate-700">
          {(["cards", "list"] as const).map((v) => (
            <button
              key={v}
              onClick={() => switchView(v)}
              className={`px-3 py-1.5 capitalize ${
                view === v ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {v}
            </button>
          ))}
        </div>
        <select
          value={status}
          aria-label="Filter projects by status"
          onChange={(e) => setParam("status", e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5"
        >
          {STATUS_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <select
          value={sort}
          aria-label="Sort projects"
          onChange={(e) => setParam("sort", e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5"
        >
          <option value="recent">Recent activity</option>
          <option value="name">Name</option>
          <option value="created">Created</option>
        </select>
        <input
          value={q}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Search projects…"
          className="ml-auto w-full max-w-[220px] rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 outline-none focus:border-indigo-500"
        />
      </div>

      {notice && (
        <p className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">
          {notice}
        </p>
      )}
      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {projects === null ? (
        <p className="text-slate-400">Loading…</p>
      ) : projects.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 p-10 text-center">
          <p className="text-slate-400">
            {status === "archived" ? "No archived projects." : "No projects match."}
          </p>
          <p className="mt-1 text-sm text-slate-400">
            Create one here, fork a marketplace sample, or chat with marshal — a project is
            created automatically.
          </p>
        </div>
      ) : view === "cards" ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {projects.map((project) => (
            <div
              key={project.id}
              className="group relative rounded-xl border border-slate-800 bg-slate-900/60 p-5 transition hover:border-indigo-500/50"
            >
              <Link href={`/projects/${project.id}`} className="block">
                <div className="flex items-start justify-between gap-2 pr-6">
                  <h2 className="truncate font-medium">{project.name}</h2>
                  <span className="flex shrink-0 items-center gap-1">
                    {/* S15-02: team badge explains WHY a project you don't own
                        is visible; explicit shares keep the "Shared" badge. */}
                    {project.team_name && (
                      <span
                        className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-sky-300"
                        title={`Team workspace: ${project.team_name}`}
                      >
                        {project.team_name}
                      </span>
                    )}
                    {project.my_role && project.my_role !== "owner" && !project.team_name && (
                      <span className="rounded bg-indigo-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300">
                        Shared · {project.my_role}
                      </span>
                    )}
                    <ProjectStatusBadge status={project.status} />
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-sm text-slate-400">
                  {project.description ?? "No description"}
                </p>
                <div className="mt-4 flex items-center justify-between text-xs text-slate-400">
                  <span>
                    {project.template_name ?? (project.origin === "marketplace_fork" ? "Forked" : "From scratch")}
                  </span>
                  <span>{relTime(project.last_activity_at)}</span>
                </div>
                <div className="mt-2 flex items-center justify-between text-xs">
                  <RiskBadge level={project.risk_level} />
                  <CostChip project={project} />
                </div>
              </Link>
              {(!project.my_role || project.my_role === "owner") && (
                <RowMenu
                  project={project}
                  onArchive={archive}
                  onRestore={restore}
                  onDelete={(p) => setConfirmDelete(p)}
                  className="absolute right-3 top-3 opacity-0 transition group-hover:opacity-100"
                />
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/80 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Template</th>
                <th className="px-4 py-3">Risk</th>
                <th className="px-4 py-3">Cost MTD</th>
                <th className="px-4 py-3">Versions</th>
                <th className="px-4 py-3">Deployment</th>
                <th className="px-4 py-3">Last activity</th>
                <th className="px-4 py-3" aria-label="Actions" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {projects.map((project) => (
                <tr key={project.id} className="bg-slate-900/40 hover:bg-slate-900/70">
                  <td className="max-w-[240px] px-4 py-3">
                    <Link
                      href={`/projects/${project.id}`}
                      className="block truncate font-medium text-indigo-300 hover:text-indigo-200"
                    >
                      {project.name}
                      {project.my_role && project.my_role !== "owner" && (
                        <span className="ml-2 rounded bg-indigo-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300">
                          {project.my_role}
                        </span>
                      )}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <ProjectStatusBadge status={project.status} />
                  </td>
                  <td className="px-4 py-3 text-slate-400">
                    {project.template_name ?? "—"}
                    {project.template_deprecated && (
                      <span className="ml-1 text-amber-400" title="Template deprecated">⚠</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <RiskBadge level={project.risk_level} />
                  </td>
                  <td className="px-4 py-3 text-slate-400">
                    {project.cost_mtd_usd != null ? formatUsd(project.cost_mtd_usd) : "—"}
                  </td>
                  <td className="px-4 py-3 text-slate-400">{project.spec_version_count}</td>
                  <td className="px-4 py-3">
                    {project.deployment?.status === "active" && project.deployment.app_url ? (
                      <a
                        href={project.deployment.app_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-emerald-400 hover:underline"
                      >
                        Live ↗
                      </a>
                    ) : (
                      <span className="text-slate-400">
                        {project.deployment?.status?.replace(/_/g, " ") ?? "—"}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-slate-400">{relTime(project.last_activity_at)}</td>
                  <td className="px-4 py-3 text-right">
                    {(!project.my_role || project.my_role === "owner") && (
                      <RowMenu
                        project={project}
                        onArchive={archive}
                        onRestore={restore}
                        onDelete={(p) => setConfirmDelete(p)}
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data && (
        <p className="text-sm text-slate-400">
          Showing {projects?.length ?? 0} project{(projects?.length ?? 0) === 1 ? "" : "s"}
          {data.archived_count > 0 && status !== "archived" && (
            <>
              {" · "}
              <button
                onClick={() => setParam("status", "archived")}
                className="text-indigo-400 hover:text-indigo-300"
              >
                {data.archived_count} archived
              </button>
            </>
          )}
        </p>
      )}

      {showCreate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <form
            onSubmit={createProject}
            className="w-full max-w-md rounded-xl border border-slate-700 bg-slate-900 p-6"
          >
            <h2 className="text-lg font-medium">New project</h2>
            <label className="mt-4 block text-sm text-slate-400">
              Name
              <input
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={120}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-indigo-500"
              />
            </label>
            <label className="mt-3 block text-sm text-slate-400">
              Description (optional)
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                className="mt-1 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-indigo-500"
              />
            </label>
            {templates.length > 0 && (
              <label className="mt-3 block text-sm text-slate-400">
                Template
                <select
                  value={templateId ?? ""}
                  onChange={(e) => setTemplateId(e.target.value || null)}
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
                >
                  <option value="">From scratch (no template)</option>
                  {templates.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name} — {t.category.replace(/_/g, " ")}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <p className="mt-3 text-xs text-slate-400">
              Or start from a working example in the{" "}
              <Link href="/marketplace" className="text-indigo-400 hover:text-indigo-300">
                Marketplace
              </Link>
              .
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowCreate(false)}
                className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!name.trim()}
                className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
              >
                Create
              </button>
            </div>
          </form>
        </div>
      )}

      {confirmDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md rounded-xl border border-red-900/60 bg-slate-900 p-6">
            <h2 className="text-lg font-medium text-red-300">Delete project</h2>
            <p className="mt-2 text-sm text-slate-400">
              This hides &ldquo;{confirmDelete.name}&rdquo; permanently (specs, history and
              sessions go with it). Type the project name to confirm.
            </p>
            <input
              autoFocus
              value={deleteText}
              onChange={(e) => setDeleteText(e.target.value)}
              placeholder={confirmDelete.name}
              className="mt-4 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-red-500"
            />
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => {
                  setConfirmDelete(null);
                  setDeleteText("");
                }}
                className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                onClick={doDelete}
                disabled={deleteText !== confirmDelete.name}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-40"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function CostChip({ project }: { project: Project }) {
  if (project.cost_mtd_usd == null) return <span />;
  const budget = project.budget_override_usd;
  const pct = budget ? (project.cost_mtd_usd / budget) * 100 : null;
  const tone =
    pct == null ? "text-slate-400" : pct >= 100 ? "text-red-400" : pct >= 75 ? "text-amber-400" : "text-slate-400";
  return (
    <span className={tone} title={budget ? `${pct?.toFixed(0)}% of $${budget} budget` : undefined}>
      {formatUsd(project.cost_mtd_usd)} MTD
    </span>
  );
}

function RowMenu({
  project,
  onArchive,
  onRestore,
  onDelete,
  className = "",
}: {
  project: Project;
  onArchive: (p: Project) => void;
  onRestore: (p: Project) => void;
  onDelete: (p: Project) => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const archived = project.status === "archived";
  return (
    <div className={`relative ${className}`}>
      <button
        onClick={(e) => {
          e.preventDefault();
          setOpen((v) => !v);
        }}
        aria-label={`Actions for ${project.name}`}
        className="rounded-md px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-white"
      >
        ⋯
      </button>
      {open && (
        <div
          className="absolute right-0 z-20 mt-1 w-36 overflow-hidden rounded-lg border border-slate-700 bg-slate-900 text-sm shadow-xl"
          onMouseLeave={() => setOpen(false)}
        >
          <Link href={`/projects/${project.id}`} className="block px-3 py-2 hover:bg-slate-800">
            Open
          </Link>
          {archived ? (
            <button
              onClick={() => {
                setOpen(false);
                onRestore(project);
              }}
              className="block w-full px-3 py-2 text-left hover:bg-slate-800"
            >
              Restore
            </button>
          ) : (
            <button
              onClick={() => {
                setOpen(false);
                onArchive(project);
              }}
              disabled={project.status === "building"}
              className="block w-full px-3 py-2 text-left hover:bg-slate-800 disabled:opacity-40"
            >
              Archive
            </button>
          )}
          <button
            onClick={() => {
              setOpen(false);
              onDelete(project);
            }}
            className="block w-full px-3 py-2 text-left text-red-400 hover:bg-slate-800"
          >
            Delete…
          </button>
        </div>
      )}
    </div>
  );
}

export default function ProjectsPage() {
  return (
    <Suspense fallback={<p className="text-slate-400">Loading…</p>}>
      <ProjectsInner />
    </Suspense>
  );
}

/** External spec-set import (I1/I2, external-import-connectors spec).
 *  Paste the three documents or upload a marshal-layout zip; the imported
 *  project flows through the identical risk/conformance/deploy gates as
 *  authored content. */
function ImportProjectModal({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [tab, setTab] = useState<"paste" | "zip" | "url">("paste");
  const [name, setName] = useState("");
  const [source, setSource] = useState("");
  const [url, setUrl] = useState("");
  const [requirements, setRequirements] = useState("");
  const [design, setDesign] = useState("");
  const [tasksDoc, setTasksDoc] = useState("");
  const [archiveB64, setArchiveB64] = useState<string | null>(null);
  // pdf-docx-ingestion: PDF/Word rides a separate request member so the
  // server dispatches on magic bytes, never the extension we branch on here.
  const [documentB64, setDocumentB64] = useState<string | null>(null);
  const [documentName, setDocumentName] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    setArchiveB64(null);
    setDocumentB64(null);
    setDocumentName(null);
    setFileName(null);
    setError(null);
    if (!file) return;
    if (file.size > 1_400_000) {
      setError("File must be under 1.4MB.");
      return;
    }
    const isZip = /\.zip$/i.test(file.name);
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result ?? "");
      const b64 = url.slice(url.indexOf(",") + 1); // strip data: prefix
      if (isZip) {
        setArchiveB64(b64);
      } else {
        setDocumentB64(b64);
        setDocumentName(file.name.slice(0, 255));
      }
      setFileName(file.name);
    };
    reader.onerror = () => setError("Could not read the file.");
    reader.readAsDataURL(file);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || busy) return;
    const body: Record<string, unknown> = { name: name.trim() };
    if (source.trim()) body.source = source.trim();
    if (tab === "paste") {
      const docs: Record<string, string> = {};
      if (requirements.trim()) docs.requirements = requirements;
      if (design.trim()) docs.design = design;
      if (tasksDoc.trim()) docs.tasks = tasksDoc;
      body.docs = docs;
    } else if (tab === "zip") {
      if (archiveB64) {
        body.archive_b64 = archiveB64;
      } else {
        body.document_b64 = documentB64;
        if (documentName) body.document_name = documentName;
      }
    } else {
      body.url = url.trim();
    }
    setBusy(true);
    setError(null);
    try {
      const project = await api<Project>("/v1/projects/import", {
        method: "POST",
        body: JSON.stringify(body),
      });
      toast.success(`"${project.name}" imported — risk scoring runs automatically.`);
      router.push(`/projects/${project.id}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  const submitDisabled =
    busy ||
    !name.trim() ||
    (tab === "paste"
      ? !requirements.trim()
      : tab === "zip"
        ? !archiveB64 && !documentB64
        : !/^https:\/\/.+/.test(url.trim()));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-lg rounded-xl border border-slate-700 bg-slate-900 p-6"
      >
        <h2 className="text-lg font-medium">Import a project</h2>
        <p className="mt-1 text-sm text-slate-400">
          Bring an existing specification — a marshal export, a
          <code className="mx-1 rounded bg-slate-950 px-1">.kiro/specs</code>
          workspace, plain markdown, or a PDF / Word document (text is
          extracted verbatim). Imports are governed exactly like authored
          specs.
        </p>

        <div className="mt-4 flex gap-1 border-b border-slate-800 text-sm">
          {(
            [
              ["paste", "Paste documents"],
              ["zip", "Upload file"],
              ["url", "From URL"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={`rounded-t-lg px-4 py-2 ${
                tab === key
                  ? "border border-b-0 border-slate-800 bg-slate-900/40 text-white"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        <label className="mt-4 block text-sm text-slate-400">
          Project name
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={120}
            required
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
          />
        </label>
        <label className="mt-3 block text-sm text-slate-400">
          Source label <span className="text-xs">(optional, for the audit trail)</span>
          <input
            value={source}
            onChange={(e) => setSource(e.target.value)}
            maxLength={120}
            placeholder="e.g. github.com/acme/claims-agent"
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
          />
        </label>

        {tab === "paste" ? (
          <div className="mt-3 space-y-3">
            {(
              [
                ["requirements.md (required)", requirements, setRequirements],
                ["design.md (optional)", design, setDesign],
                ["tasks.md (optional)", tasksDoc, setTasksDoc],
              ] as const
            ).map(([label, value, setter]) => (
              <label key={label} className="block text-sm text-slate-400">
                {label}
                <textarea
                  value={value}
                  onChange={(e) => setter(e.target.value)}
                  rows={3}
                  className="mt-1 w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs outline-none focus:border-indigo-500"
                />
              </label>
            ))}
          </div>
        ) : tab === "zip" ? (
          <label className="mt-3 block text-sm text-slate-400">
            Spec zip (.kiro/specs layout), PDF, or Word .docx
            <input
              type="file"
              accept=".zip,.pdf,.docx"
              onChange={onFile}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm file:mr-3 file:rounded file:border-0 file:bg-slate-800 file:px-3 file:py-1 file:text-slate-200"
            />
            {fileName && (
              <span className="mt-1 block text-xs text-slate-400">Ready: {fileName}</span>
            )}
          </label>
        ) : (
          <label className="mt-3 block text-sm text-slate-400">
            Public https URL (markdown, marshal export zip, PDF or Word)
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://raw.githubusercontent.com/acme/agent/main/requirements.md"
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
            />
            <span className="mt-1 block text-xs text-slate-500">
              Public sources only — no sign-in, private networks are refused.
              Markdown, PDF or Word becomes the requirements document; a zip
              imports the full spec set. Scanned PDFs are refused (no OCR).
            </span>
          </label>
        )}

        {error && (
          <p className="mt-3 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
            {error}
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitDisabled}
            className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            {busy ? "Importing…" : "Import project"}
          </button>
        </div>
      </form>
    </div>
  );
}
