"use client";

import { useEffect, useState } from "react";
import { ModalShell } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { TemplateUser } from "@/lib/types";

/** New-session flow: mode (power/admin) + scratch-or-template (S2-05, S4-05). */
export default function NewSessionModal({
  onClose,
  onCreate,
  canChooseMode = false,
  studioAvailable = false,
}: {
  onClose: () => void;
  onCreate: (
    templateId: string | null,
    starterPrompt: string | null,
    mode?: "freeform" | "guided",
    surface?: "chat" | "studio"
  ) => void;
  /** Power/admin pick per session; business is always guided (FSD §4.1.1). */
  canChooseMode?: boolean;
  /** S18 R4.2: power/admin additionally choose the surface (wizard untouched). */
  studioAvailable?: boolean;
}) {
  const [templates, setTemplates] = useState<TemplateUser[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [mode, setMode] = useState<"freeform" | "guided">("freeform");
  const [surface, setSurface] = useState<"chat" | "studio">(() => {
    if (!studioAvailable) return "chat";
    try {
      return localStorage.getItem("build-surface") === "studio" ? "studio" : "chat";
    } catch {
      return "chat";
    }
  });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<TemplateUser[]>("/v1/templates")
      .then(setTemplates)
      .catch(() => setTemplates([]));
  }, []);

  function create() {
    setBusy(true);
    const template = templates?.find((t) => t.id === selected) ?? null;
    onCreate(
      selected,
      template?.starter_prompts?.[0] ?? null,
      canChooseMode ? mode : undefined,
      // The wizard lives in chat until completion [D17]
      studioAvailable && mode === "freeform" ? surface : "chat"
    );
  }

  return (
    <ModalShell label="New session" onClose={onClose}>
        <h2 className="text-lg font-medium">New session</h2>
        <p className="mt-1 text-sm text-slate-400">
          Start from scratch, or from a template with governance guardrails built in.
        </p>

        {canChooseMode && (
          <div className="mt-4 flex overflow-hidden rounded-lg border border-slate-700 text-sm">
            {(
              [
                ["freeform", "💬 Freeform chat"],
                ["guided", "🧭 Guided wizard"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                onClick={() => setMode(value)}
                className={`flex-1 px-3 py-2 ${
                  mode === value ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        )}

        {studioAvailable && mode === "freeform" && (
          <div className="mt-2 flex overflow-hidden rounded-lg border border-slate-700 text-sm">
            {(
              [
                ["chat", "Classic chat"],
                ["studio", "🎛 Studio (spec beside chat)"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                onClick={() => setSurface(value)}
                className={`flex-1 px-3 py-2 ${
                  surface === value
                    ? "bg-slate-800 text-white"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        )}

        <div className="mt-4 max-h-72 space-y-2 overflow-y-auto">
          <button
            onClick={() => setSelected(null)}
            className={`w-full rounded-lg border p-3 text-left transition ${
              selected === null
                ? "border-indigo-500 bg-indigo-500/10"
                : "border-slate-700 bg-slate-950/60 hover:border-slate-500"
            }`}
          >
            <span className="font-medium">🆕 From scratch</span>
            <p className="mt-0.5 text-xs text-slate-400">
              Blank canvas — platform default models
            </p>
          </button>

          {templates === null ? (
            <p className="px-1 py-3 text-center text-xs text-slate-400">Loading templates…</p>
          ) : (
            templates.map((template) => (
              <button
                key={template.id}
                onClick={() => setSelected(template.id)}
                className={`w-full rounded-lg border p-3 text-left transition ${
                  selected === template.id
                    ? "border-indigo-500 bg-indigo-500/10"
                    : "border-slate-700 bg-slate-950/60 hover:border-slate-500"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">📋 {template.name}</span>
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-400">
                    {template.category.replace(/_/g, " ")}
                  </span>
                </div>
                {template.description && (
                  <p className="mt-0.5 line-clamp-2 text-xs text-slate-400">
                    {template.description}
                  </p>
                )}
                {template.allowed_model_labels.length > 0 && (
                  <p className="mt-1 text-[10px] text-indigo-300">
                    Models: {template.allowed_model_labels.join(", ")}
                  </p>
                )}
              </button>
            ))
          )}
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
          >
            Cancel
          </button>
          <button
            onClick={create}
            disabled={busy}
            className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            {busy ? "Creating…" : "Create session"}
          </button>
        </div>
    </ModalShell>
  );
}
