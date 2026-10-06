"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useMemo, useState } from "react";
// Named import on purpose: js-yaml v5 dropped the default export. The stale
// @types/js-yaml v4 typings still DECLARE one, so `import yaml from "js-yaml"`
// type-checks, the Next build only WARNS, and `yaml` is undefined at runtime —
// which broke this page silently after the S14 dependency bump (live finding,
// 4 Aug 2026: "Cannot read properties of undefined (reading 'dump')").
import { dump as yamlDump } from "js-yaml";
import { useConfirm } from "@/components/ui/ConfirmDialog";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import type { ModelRegistryEntry, TemplateAdmin } from "@/lib/types";

const CATEGORIES = [
  "chatbot",
  "document_processing",
  "data_analysis",
  "workflow_automation",
  "content_generation",
  "custom",
];

type EditorTab = "general" | "model" | "yaml";

/** Admin template editor: General | Model Guardrails | YAML preview (S2-04). */
export default function TemplateEditorPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const isNew = id === "new";
  const router = useRouter();
  const readonly = useAdminReadonly();

  const [tab, setTab] = useState<EditorTab>("general");
  const [registry, setRegistry] = useState<ModelRegistryEntry[]>([]);
  const [template, setTemplate] = useState<TemplateAdmin | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const confirmDialog = useConfirm();
  const [notice, setNotice] = useState<string | null>(null);

  // Form state
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("custom");
  const [starterPrompt, setStarterPrompt] = useState("");
  const [requiredSections, setRequiredSections] = useState("");
  const [allowedModels, setAllowedModels] = useState<string[]>([]);
  const [maxTokens, setMaxTokens] = useState(4096);
  const [tempMin, setTempMin] = useState(0);
  const [tempMax, setTempMax] = useState(0.7);

  useEffect(() => {
    api<ModelRegistryEntry[]>("/v1/admin/templates/model-registry")
      .then(setRegistry)
      .catch(() => {});
  }, []);

  const hydrate = useCallback((t: TemplateAdmin) => {
    setTemplate(t);
    setName(t.name);
    setDescription(t.description ?? "");
    setCategory(t.category);
    setStarterPrompt(t.scaffolding?.starter_prompts?.[0] ?? "");
    setRequiredSections((t.scaffolding?.required_spec_sections ?? []).join(", "));
    setAllowedModels(t.guardrails?.model?.allowed_models ?? []);
    setMaxTokens(t.guardrails?.model?.max_tokens ?? 4096);
    setTempMin(t.guardrails?.model?.temperature?.min ?? 0);
    setTempMax(t.guardrails?.model?.temperature?.max ?? 0.7);
  }, []);

  useEffect(() => {
    if (!isNew) {
      api<TemplateAdmin>(`/v1/admin/templates/${id}`)
        .then(hydrate)
        .catch((e) => setError(e.message));
    }
  }, [id, isNew, hydrate]);

  const payload = useMemo(
    () => ({
      name,
      description: description || null,
      category,
      guardrails: {
        ...(template?.guardrails ?? {}),
        model: {
          allowed_models: allowedModels,
          max_tokens: maxTokens,
          temperature: { min: tempMin, max: tempMax },
          top_p: template?.guardrails?.model?.top_p ?? { min: 0.1, max: 0.95 },
        },
      },
      scaffolding: {
        ...(template?.scaffolding ?? {}),
        starter_prompts: starterPrompt ? [starterPrompt] : [],
        required_spec_sections: requiredSections
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      },
    }),
    [name, description, category, allowedModels, maxTokens, tempMin, tempMax, starterPrompt, requiredSections, template]
  );

  const yamlPreview = useMemo(
    () =>
      yamlDump(
        { template: { ...payload, status: template?.status ?? "draft", version: template?.version ?? 1 } },
        { lineWidth: 100 }
      ),
    [payload, template]
  );

  async function act(action: () => Promise<void>, doneMsg: string) {
    setBusy(true);
    setError(null);
    try {
      await action();
      setNotice(doneMsg);
      setTimeout(() => setNotice(null), 2500);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const saveDraft = () =>
    act(async () => {
      if (isNew) {
        const created = await api<TemplateAdmin>("/v1/admin/templates", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        router.replace(`/admin/templates/${created.id}`);
        hydrate(created);
      } else {
        hydrate(
          await api<TemplateAdmin>(`/v1/admin/templates/${id}`, {
            method: "PUT",
            body: JSON.stringify(payload),
          })
        );
      }
    }, "Saved");

  const publish = () =>
    act(async () => {
      await api(`/v1/admin/templates/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      hydrate(await api<TemplateAdmin>(`/v1/admin/templates/${id}/publish`, { method: "POST" }));
    }, "Published");

  const deprecate = () =>
    act(async () => {
      const count = template?.active_project_count ?? 0;
      if (!(await confirmDialog({ title: "Deprecate template", body: `${count} project(s) reference it — they keep working; new projects can't select it.`, confirmLabel: "Deprecate" })))
        return;
      hydrate(await api<TemplateAdmin>(`/v1/admin/templates/${id}/deprecate`, { method: "POST" }));
    }, "Deprecated");

  const remove = () =>
    act(async () => {
      if (!(await confirmDialog({ title: "Delete draft template", body: "This draft is removed permanently.", confirmLabel: "Delete", destructive: true }))) return;
      await api(`/v1/admin/templates/${id}`, { method: "DELETE" });
      router.push("/admin/templates");
    }, "Deleted");

  function toggleModel(modelId: string) {
    const entry = registry.find((model) => model.id === modelId);
    if (entry?.selectable === false && !allowedModels.includes(modelId)) return;
    setAllowedModels((prev) =>
      prev.includes(modelId) ? prev.filter((m) => m !== modelId) : [...prev, modelId]
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <div>
        <Link href="/admin/templates" className="text-sm text-slate-400 hover:text-slate-300">
          ← Templates
        </Link>
        <div className="mt-1 flex items-center justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">
            {isNew ? "New template" : name || "Template"}
          </h1>
          {template && (
            <span className="rounded bg-slate-800 px-2 py-0.5 text-xs uppercase tracking-wide text-slate-400">
              {template.status} · v{template.version}
            </span>
          )}
        </div>
      </div>

      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}
      {notice && <p className="text-sm text-emerald-400">{notice}</p>}

      <div className="flex gap-1 border-b border-slate-800">
        {(["general", "model", "yaml"] as EditorTab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`rounded-t-lg px-4 py-2 text-sm ${
              tab === t ? "border border-b-0 border-slate-800 bg-slate-900/40 text-white" : "text-slate-400"
            }`}
          >
            {t === "general" ? "General" : t === "model" ? "Model Guardrails" : "YAML"}
          </button>
        ))}
      </div>

      {tab === "general" && (
        <div className="space-y-4 rounded-xl border border-slate-800 bg-slate-900/40 p-5">
          <label className="block text-sm text-slate-400">
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={80}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-indigo-500" />
          </label>
          <label className="block text-sm text-slate-400">
            Description
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2}
              className="mt-1 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-indigo-500" />
          </label>
          <label className="block text-sm text-slate-400">
            Category
            <select value={category} onChange={(e) => setCategory(e.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100">
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>{c.replace(/_/g, " ")}</option>
              ))}
            </select>
          </label>
          <label className="block text-sm text-slate-400">
            Starter prompt (pre-fills the chat composer)
            <textarea value={starterPrompt} onChange={(e) => setStarterPrompt(e.target.value)} rows={2}
              className="mt-1 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-indigo-500" />
          </label>
          <label className="block text-sm text-slate-400">
            Required spec sections (comma-separated)
            <input value={requiredSections} onChange={(e) => setRequiredSections(e.target.value)}
              placeholder="data_sources, security_controls"
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-indigo-500" />
          </label>
        </div>
      )}

      {tab === "model" && (
        <div className="space-y-5 rounded-xl border border-slate-800 bg-slate-900/40 p-5">
          <div>
            <p className="text-sm font-medium">Allowed models</p>
            <p className="text-xs text-slate-400">Server-side enforced for every Bedrock call in sessions using this template.</p>
            <div className="mt-3 space-y-2">
              {registry
                .filter((model) => model.selectable !== false || allowedModels.includes(model.id))
                .map((model) => {
                  const checked = allowedModels.includes(model.id);
                  return (
                    <label key={model.id} className="block rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm">
                      <span className="flex items-center gap-3">
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={model.selectable === false && !checked}
                          onChange={() => toggleModel(model.id)}
                          className="h-4 w-4 accent-indigo-500"
                        />
                        <span>{model.label}</span>
                        <span className="text-xs text-slate-500">{model.provider_name}</span>
                        {model.source === "external" && (
                          <span
                            className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-sky-300"
                            title="Hosted outside Bedrock — prompts sent here leave AWS"
                          >
                            External
                          </span>
                        )}
                        <span className="ml-auto rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase text-slate-400">{model.tier}</span>
                      </span>
                      {model.disabled_reason && (
                        <span className="mt-1 block pl-7 text-xs text-amber-300/80">{model.disabled_reason}</span>
                      )}
                    </label>
                  );
                })}
            </div>
          </div>
          <label className="block text-sm text-slate-400">
            Max tokens per call
            <input type="number" value={maxTokens} min={256} max={8192}
              onChange={(e) => setMaxTokens(Number(e.target.value))}
              className="mt-1 w-40 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" />
          </label>
          <div className="flex gap-6">
            <label className="block text-sm text-slate-400">
              Temperature min
              <input type="number" step={0.1} min={0} max={1} value={tempMin}
                onChange={(e) => setTempMin(Number(e.target.value))}
                className="mt-1 w-28 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" />
            </label>
            <label className="block text-sm text-slate-400">
              Temperature max
              <input type="number" step={0.1} min={0} max={1} value={tempMax}
                onChange={(e) => setTempMax(Number(e.target.value))}
                className="mt-1 w-28 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" />
            </label>
          </div>
          <p className="text-xs text-slate-400">
            Temperature/top-p bounds are stored now and take effect when parameter tuning ships (S4).
          </p>
        </div>
      )}

      {tab === "yaml" && (
        <pre className="overflow-x-auto rounded-xl border border-slate-800 bg-slate-950 p-5 text-xs leading-relaxed text-slate-300">
          {yamlPreview}
        </pre>
      )}

      <div className="flex items-center gap-2">
        <button onClick={saveDraft} disabled={busy || readonly || !name.trim()}
          className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40">
          {isNew ? "Create draft" : "Save"}
        </button>
        {!isNew && template?.status !== "active" && template?.status !== "deprecated" && (
          <button onClick={publish} disabled={busy || readonly}
            className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-40">
            Publish
          </button>
        )}
        {!isNew && template?.status === "active" && (
          <button onClick={deprecate} disabled={busy || readonly}
            className="rounded-lg border border-amber-500/50 px-4 py-2 text-sm text-amber-300 hover:bg-amber-500/10 disabled:opacity-40">
            Deprecate
          </button>
        )}
        {!isNew && template?.status === "draft" && (
          <button onClick={remove} disabled={busy || readonly}
            className="rounded-lg border border-red-500/50 px-4 py-2 text-sm text-red-300 hover:bg-red-500/10 disabled:opacity-40">
            Delete
          </button>
        )}
      </div>
    </div>
  );
}
