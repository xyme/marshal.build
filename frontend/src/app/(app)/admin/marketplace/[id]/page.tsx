"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useEffect, useState } from "react";
import SpecMarkdown from "@/components/markdown/SpecMarkdown";
import { useConfirm } from "@/components/ui/ConfirmDialog";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import { COMPLEXITY_STYLE, modelLabel } from "@/lib/marketplace-ui";
import type {
  ModelRegistryEntry,
  Project,
  ProjectList,
  SampleAdmin,
  TemplateAdmin,
} from "@/lib/types";

const CATEGORIES = [
  "chatbot",
  "document_processing",
  "data_analysis",
  "workflow_automation",
  "content_generation",
  "custom",
];
const DOCS = ["requirements_md", "design_md", "tasks_md"] as const;

const EMPTY: Omit<SampleAdmin, "id" | "created_at" | "updated_at" | "status"> & { status: string } = {
  title: "",
  description: "",
  long_description: "",
  category: "chatbot",
  complexity: "beginner",
  models_used: [],
  template_id: null,
  spec_snapshot: {},
  assets: {},
  keywords: [],
  metadata_extra: {},
  author_name: null,
  template_name: null,
  template_deprecated: false,
  fork_count: 0,
  view_count: 0,
  published_at: null,
  status: "draft",
};

/** Admin sample editor: General | Spec Snapshot | Preview (S3-04, spec R5.2). */
export default function AdminSampleEditor({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const isNew = id === "new";
  const router = useRouter();
  const readonly = useAdminReadonly();

  const [sample, setSample] = useState(EMPTY);
  const [tab, setTab] = useState<"general" | "snapshot" | "preview">("general");
  const [models, setModels] = useState<ModelRegistryEntry[]>([]);
  const [templates, setTemplates] = useState<TemplateAdmin[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [importId, setImportId] = useState("");
  const [keywordsText, setKeywordsText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const confirmDialog = useConfirm();

  useEffect(() => {
    api<ModelRegistryEntry[]>("/v1/admin/templates/model-registry")
      .then((entries) => setModels(entries.filter((model) => model.selectable !== false)))
      .catch(() => {});
    api<TemplateAdmin[]>("/v1/admin/templates").then(setTemplates).catch(() => {});
    api<ProjectList>("/v1/projects?page_size=100")
      .then((l) => setProjects(l.items))
      .catch(() => {});
    if (!isNew) {
      api<SampleAdmin>(`/v1/admin/marketplace/samples/${id}`)
        .then((s) => {
          setSample({ ...s, long_description: s.long_description ?? "" });
          setKeywordsText(s.keywords.join(", "));
        })
        .catch((e) => setError(e.message));
    }
  }, [id, isNew]);

  function upsertPayload() {
    return {
      title: sample.title,
      description: sample.description,
      long_description: sample.long_description || null,
      category: sample.category,
      complexity: sample.complexity,
      models_used: sample.models_used,
      template_id: sample.template_id,
      spec_snapshot: sample.spec_snapshot,
      assets: sample.assets,
      keywords: keywordsText
        .split(",")
        .map((k) => k.trim().toLowerCase())
        .filter(Boolean),
      metadata_extra: sample.metadata_extra,
    };
  }

  async function save(): Promise<string | null> {
    setBusy(true);
    setError(null);
    try {
      if (isNew) {
        const created = await api<SampleAdmin>("/v1/admin/marketplace/samples", {
          method: "POST",
          body: JSON.stringify(upsertPayload()),
        });
        router.replace(`/admin/marketplace/${created.id}`);
        setNotice("Draft created.");
        return created.id;
      }
      const updated = await api<SampleAdmin>(`/v1/admin/marketplace/samples/${id}`, {
        method: "PUT",
        body: JSON.stringify(upsertPayload()),
      });
      setSample({ ...updated, long_description: updated.long_description ?? "" });
      setNotice("Saved.");
      return id;
    } catch (e) {
      setError((e as Error).message);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function lifecycle(action: "publish" | "archive") {
    const savedId = await save();
    if (!savedId) return;
    setBusy(true);
    try {
      const updated = await api<SampleAdmin>(
        `/v1/admin/marketplace/samples/${savedId}/${action}`,
        { method: "POST" }
      );
      setSample({ ...updated, long_description: updated.long_description ?? "" });
      setNotice(action === "publish" ? "Published — live in the catalog." : "Archived.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function doDelete() {
    if (!(await confirmDialog({ title: "Delete draft sample", body: "This draft is removed permanently.", confirmLabel: "Delete", destructive: true }))) return;
    try {
      await api(`/v1/admin/marketplace/samples/${id}`, { method: "DELETE" });
      router.push("/admin/marketplace");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function importFromProject() {
    if (!importId) return;
    const savedId = await save();
    if (!savedId) return;
    setBusy(true);
    try {
      const updated = await api<SampleAdmin>(
        `/v1/admin/marketplace/samples/${savedId}/import-spec`,
        { method: "POST", body: JSON.stringify({ project_id: importId }) }
      );
      setSample({ ...updated, long_description: updated.long_description ?? "" });
      setNotice("Latest spec documents imported.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function toggleModel(modelId: string) {
    setSample((s) => ({
      ...s,
      models_used: s.models_used.includes(modelId)
        ? s.models_used.filter((m) => m !== modelId)
        : [...s.models_used, modelId],
    }));
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Link href="/admin/marketplace" className="text-sm text-slate-400 hover:text-slate-300">
            ← Marketplace
          </Link>
          <div className="mt-1 flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">
              {isNew ? "New sample" : sample.title || "Edit sample"}
            </h1>
            <span className="rounded bg-slate-800 px-2 py-0.5 text-xs uppercase tracking-wide text-slate-400">
              {sample.status}
            </span>
          </div>
        </div>
        <div className="flex gap-2">
          <button
            onClick={save}
            disabled={busy || readonly || !sample.title.trim()}
            className="rounded-lg border border-slate-700 px-4 py-2 text-sm hover:border-slate-500 disabled:opacity-40"
          >
            Save draft
          </button>
          {sample.status !== "published" && (
            <button
              onClick={() => lifecycle("publish")}
              disabled={busy || readonly || isNew}
              className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
            >
              Publish
            </button>
          )}
          {sample.status === "published" && (
            <button
              onClick={() => lifecycle("archive")}
              disabled={busy || readonly}
              className="rounded-lg border border-amber-600/50 px-4 py-2 text-sm text-amber-300 hover:border-amber-500 disabled:opacity-40"
            >
              Archive
            </button>
          )}
          {sample.status === "draft" && !isNew && (
            <button
              onClick={doDelete}
              disabled={busy || readonly}
              className="rounded-lg border border-red-900/60 px-4 py-2 text-sm text-red-400 hover:border-red-700 disabled:opacity-40"
            >
              Delete
            </button>
          )}
        </div>
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

      <div className="flex gap-1 border-b border-slate-800 text-sm">
        {(
          [
            ["general", "General"],
            ["snapshot", "Spec Snapshot"],
            ["preview", "Preview"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
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

      {tab === "general" && (
        <div className="grid max-w-3xl gap-4">
          <label className="block text-sm text-slate-400">
            Title <span className="text-slate-400">({sample.title.length}/60)</span>
            <input
              value={sample.title}
              onChange={(e) => setSample({ ...sample, title: e.target.value })}
              maxLength={60}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
            />
          </label>
          <label className="block text-sm text-slate-400">
            Card description <span className="text-slate-400">({sample.description.length}/500)</span>
            <textarea
              value={sample.description}
              onChange={(e) => setSample({ ...sample, description: e.target.value })}
              maxLength={500}
              rows={2}
              className="mt-1 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
            />
          </label>
          <label className="block text-sm text-slate-400">
            Long description (markdown)
            <textarea
              value={sample.long_description ?? ""}
              onChange={(e) => setSample({ ...sample, long_description: e.target.value })}
              maxLength={5000}
              rows={6}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs outline-none focus:border-indigo-500"
            />
          </label>
          <div className="grid grid-cols-2 gap-4">
            <label className="block text-sm text-slate-400">
              Category
              <select
                value={sample.category}
                onChange={(e) => setSample({ ...sample, category: e.target.value })}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
              >
                {CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm text-slate-400">
              Complexity
              <select
                value={sample.complexity}
                onChange={(e) =>
                  setSample({ ...sample, complexity: e.target.value as SampleAdmin["complexity"] })
                }
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
              >
                <option value="beginner">Beginner</option>
                <option value="intermediate">Intermediate</option>
                <option value="advanced">Advanced</option>
              </select>
            </label>
          </div>
          <label className="block text-sm text-slate-400">
            Template (pre-filled on fork)
            <select
              value={sample.template_id ?? ""}
              onChange={(e) => setSample({ ...sample, template_id: e.target.value || null })}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
            >
              <option value="">None</option>
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name} ({t.status})
                </option>
              ))}
            </select>
          </label>
          <fieldset className="text-sm text-slate-400">
            <legend>Models used</legend>
            <div className="mt-1 flex flex-wrap gap-3 rounded-lg border border-slate-700 bg-slate-950 p-3">
              {models.map((m) => (
                <label key={m.id} className="flex items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={sample.models_used.includes(m.id)}
                    onChange={() => toggleModel(m.id)}
                  />
                  {m.label ?? modelLabel(m.id)}
                  {m.source === "external" && (
                    <span
                      className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-sky-300"
                      title="Hosted outside Bedrock — prompts sent here leave AWS"
                    >
                      External
                    </span>
                  )}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="block text-sm text-slate-400">
            Keywords (comma-separated)
            <input
              value={keywordsText}
              onChange={(e) => setKeywordsText(e.target.value)}
              placeholder="rag, chatbot, hr"
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
            />
          </label>
        </div>
      )}

      {tab === "snapshot" && (
        <div className="space-y-4">
          <div className="flex items-end gap-2 rounded-xl border border-slate-800 bg-slate-900/40 p-4">
            <label className="block flex-1 text-sm text-slate-400">
              Import latest documents from a project (primary curation path)
              <select
                value={importId}
                onChange={(e) => setImportId(e.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
              >
                <option value="">Select a project…</option>
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.spec_version_count} versions)
                  </option>
                ))}
              </select>
            </label>
            <button
              onClick={importFromProject}
              disabled={!importId || busy || readonly || isNew}
              className="rounded-lg border border-slate-700 px-4 py-2 text-sm hover:border-slate-500 disabled:opacity-40"
            >
              Import
            </button>
          </div>
          {isNew && (
            <p className="text-xs text-slate-400">Save the draft first to enable importing.</p>
          )}
          {DOCS.map((doc) => (
            <label key={doc} className="block text-sm text-slate-400">
              <span className="font-mono text-xs">{doc.replace("_md", ".md")}</span>
              <textarea
                value={sample.spec_snapshot[doc] ?? ""}
                onChange={(e) =>
                  setSample({
                    ...sample,
                    spec_snapshot: { ...sample.spec_snapshot, [doc]: e.target.value },
                  })
                }
                rows={8}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs outline-none focus:border-indigo-500"
              />
            </label>
          ))}
        </div>
      )}

      {tab === "preview" && (
        <div className="space-y-5 rounded-xl border border-slate-800 bg-slate-900/40 p-6">
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-semibold">{sample.title || "Untitled"}</h2>
            <span className={`rounded px-1.5 py-0.5 text-[11px] capitalize ${COMPLEXITY_STYLE[sample.complexity]}`}>
              {sample.complexity}
            </span>
          </div>
          <p className="text-slate-400">{sample.description}</p>
          {sample.long_description && <SpecMarkdown content={sample.long_description} />}
          <div className="text-sm text-slate-400">
            Snapshot docs present:{" "}
            {DOCS.filter((d) => (sample.spec_snapshot[d] ?? "").trim()).length}/3 — publish requires
            all three.
          </div>
        </div>
      )}
    </div>
  );
}
