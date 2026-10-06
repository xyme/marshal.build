"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { RiskBadge } from "@/components/projects/RiskBadge";
import { api } from "@/lib/api";
import type {
  ChatSession,
  FullSpecResponse,
  Generation,
  GuidedState,
  ProjectRisk,
} from "@/lib/types";

const STEP_ORDER = ["use_case", "context", "behavior", "clarify", "generate"] as const;
const STEP_LABELS: Record<string, string> = {
  use_case: "Use case",
  context: "Users & data",
  behavior: "Behavior",
  clarify: "Clarify",
  generate: "Generate",
};

const CATEGORIES = [
  ["chatbot", "Chatbot / Virtual assistant"],
  ["document_processing", "Document processing / Summarization"],
  ["data_analysis", "Data analysis / Insights"],
  ["workflow_automation", "Workflow automation"],
  ["content_generation", "Content generation"],
  ["other", "Other"],
] as const;

const DATA_SOURCES = [
  ["documents", "Documents"],
  ["apis", "APIs"],
  ["databases", "Databases"],
  ["user_input", "User input"],
  ["files", "Files"],
] as const;

interface Props {
  session: ChatSession;
  generation: Generation | null;
  spec: FullSpecResponse | null;
  canSwitchFreeform: boolean;
  onSessionUpdate: (session: ChatSession) => void;
  onGenerate: () => void;
  onSave: () => Promise<void>;
  /** S18 R4.3: wizard completion offers the studio to power users (null = hidden). */
  studioHref?: string | null;
}

/** Guided-mode wizard (FSD §4.1.2, S4-05) — server owns state, this renders it. */
export default function GuidedWizard({
  session,
  generation,
  spec,
  canSwitchFreeform,
  onSessionUpdate,
  onGenerate,
  onSave,
  studioHref = null,
}: Props) {
  const state = session.guided_state as GuidedState;
  const frontier = state.step;
  const [viewStep, setViewStep] = useState<string>(frontier);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setViewStep(state.step), [state.step]);

  const post = useCallback(
    async (path: string, body?: unknown) => {
      setBusy(true);
      setError(null);
      try {
        const updated = await api<ChatSession>(
          `/v1/chat/sessions/${session.id}/guided/${path}`,
          { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }
        );
        onSessionUpdate(updated);
        return updated;
      } catch (e) {
        setError((e as Error).message);
        return null;
      } finally {
        setBusy(false);
      }
    },
    [session.id, onSessionUpdate]
  );

  async function switchFreeform() {
    const updated = await post("switch-freeform");
    if (updated) toast.info("Switched to freeform chat — your answers are in the thread.");
  }

  const frontierIndex = STEP_ORDER.indexOf(frontier as (typeof STEP_ORDER)[number]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
        <ol className="flex items-center gap-1 text-xs" aria-label="Wizard progress">
          {STEP_ORDER.map((step, i) => {
            const reachable = i <= frontierIndex && frontier !== "generate";
            const isForm = i < 3;
            return (
              <li key={step} className="flex items-center gap-1">
                {i > 0 && <span className="text-slate-700">→</span>}
                <button
                  disabled={!reachable || !isForm || busy}
                  onClick={() => setViewStep(step)}
                  className={`rounded-md px-2.5 py-1 ${
                    viewStep === step
                      ? "bg-indigo-500/20 text-indigo-300"
                      : i <= frontierIndex
                        ? "text-slate-300 hover:bg-slate-800"
                        : "text-slate-400"
                  } disabled:cursor-default`}
                >
                  {i + 1}. {STEP_LABELS[step]}
                </button>
              </li>
            );
          })}
        </ol>
        {canSwitchFreeform && frontier !== "generate" && (
          <button
            onClick={switchFreeform}
            disabled={busy}
            className="text-xs text-slate-400 hover:text-slate-300"
          >
            Switch to freeform chat →
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-6">
        {error && (
          <p className="mb-4 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
            {error}
          </p>
        )}
        {viewStep === "use_case" && (
          <UseCaseForm state={state} busy={busy} onSubmit={(answers) => post("answer", { step: "use_case", answers })} />
        )}
        {viewStep === "context" && (
          <ContextForm state={state} busy={busy} onSubmit={(answers) => post("answer", { step: "context", answers })} />
        )}
        {viewStep === "behavior" && (
          <BehaviorForm state={state} busy={busy} onSubmit={(answers) => post("answer", { step: "behavior", answers })} />
        )}
        {viewStep === "clarify" && (
          <ClarifyStep state={state} busy={busy} post={post} />
        )}
        {viewStep === "generate" && (
          <GenerateStep
            session={session}
            state={state}
            generation={generation}
            spec={spec}
            busy={busy}
            onGenerate={onGenerate}
            onSave={onSave}
            studioHref={studioHref}
          />
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ step 1-3

function UseCaseForm({
  state, busy, onSubmit,
}: {
  state: GuidedState;
  busy: boolean;
  onSubmit: (answers: Record<string, unknown>) => void;
}) {
  const saved = state.answers.use_case;
  const [problem, setProblem] = useState(saved?.problem ?? "");
  const [category, setCategory] = useState(saved?.category ?? "");
  const [other, setOther] = useState(saved?.category_other ?? "");
  const valid = problem.trim().length >= 20 && category && (category !== "other" || other.trim());

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({ problem: problem.trim(), category, category_other: other.trim() || null });
      }}
      className="mx-auto max-w-xl space-y-5"
    >
      <div>
        <h2 className="text-lg font-medium">What problem are you trying to solve?</h2>
        <p className="mt-1 text-sm text-slate-400">
          Plain language is perfect — marshal fills in the technical detail.
        </p>
        <textarea
          autoFocus
          value={problem}
          onChange={(e) => setProblem(e.target.value)}
          rows={4}
          maxLength={4000}
          placeholder="e.g. Our HR team answers the same policy questions every day…"
          className="mt-3 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
        />
        {problem.length > 0 && problem.trim().length < 20 && (
          <p className="mt-1 text-xs text-slate-400">A sentence or two more helps ({problem.trim().length}/20 characters minimum).</p>
        )}
      </div>
      <fieldset>
        <legend className="text-sm font-medium">What category best fits?</legend>
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {CATEGORIES.map(([value, label]) => (
            <label
              key={value}
              className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
                category === value
                  ? "border-indigo-500 bg-indigo-500/10"
                  : "border-slate-700 hover:border-slate-500"
              }`}
            >
              <input
                type="radio"
                name="category"
                className="sr-only"
                checked={category === value}
                onChange={() => setCategory(value)}
              />
              {label}
            </label>
          ))}
        </div>
        {category === "other" && (
          <input
            value={other}
            onChange={(e) => setOther(e.target.value)}
            maxLength={200}
            placeholder="Describe your category"
            className="mt-2 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
          />
        )}
      </fieldset>
      <div className="flex justify-end">
        <button
          type="submit"
          disabled={!valid || busy}
          className="rounded-lg bg-indigo-500 px-5 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
        >
          Continue →
        </button>
      </div>
    </form>
  );
}

function ContextForm({
  state, busy, onSubmit,
}: {
  state: GuidedState;
  busy: boolean;
  onSubmit: (answers: Record<string, unknown>) => void;
}) {
  const saved = state.answers.context;
  const [audience, setAudience] = useState(saved?.audience ?? "");
  const [sources, setSources] = useState<string[]>(saved?.data_sources ?? []);
  const [sensitive, setSensitive] = useState(saved?.sensitive_data ?? "");
  const valid = audience && sources.length > 0 && sensitive;

  function toggleSource(source: string) {
    setSources((prev) =>
      prev.includes(source) ? prev.filter((s) => s !== source) : [...prev, source]
    );
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({ audience, data_sources: sources, sensitive_data: sensitive });
      }}
      className="mx-auto max-w-xl space-y-5"
    >
      <fieldset>
        <legend className="text-lg font-medium">Who will use this app?</legend>
        <div className="mt-2 flex gap-2">
          {(
            [["customers", "End customers"], ["internal", "Internal staff"], ["both", "Both"]] as const
          ).map(([value, label]) => (
            <label
              key={value}
              className={`cursor-pointer rounded-lg border px-3 py-2 text-sm ${
                audience === value
                  ? "border-indigo-500 bg-indigo-500/10"
                  : "border-slate-700 hover:border-slate-500"
              }`}
            >
              <input type="radio" name="audience" className="sr-only" checked={audience === value} onChange={() => setAudience(value)} />
              {label}
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend className="text-sm font-medium">What data will it work with?</legend>
        <div className="mt-2 flex flex-wrap gap-2">
          {DATA_SOURCES.map(([value, label]) => (
            <label
              key={value}
              className={`cursor-pointer rounded-lg border px-3 py-2 text-sm ${
                sources.includes(value)
                  ? "border-indigo-500 bg-indigo-500/10"
                  : "border-slate-700 hover:border-slate-500"
              }`}
            >
              <input type="checkbox" className="sr-only" checked={sources.includes(value)} onChange={() => toggleSource(value)} />
              {label}
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend className="text-sm font-medium">Any sensitive data involved?</legend>
        <div className="mt-2 flex flex-wrap gap-2">
          {(
            [["pii", "Personal data (PII)"], ["financial", "Financial"], ["health", "Health"], ["none", "None"], ["unsure", "Not sure"]] as const
          ).map(([value, label]) => (
            <label
              key={value}
              className={`cursor-pointer rounded-lg border px-3 py-2 text-sm ${
                sensitive === value
                  ? "border-indigo-500 bg-indigo-500/10"
                  : "border-slate-700 hover:border-slate-500"
              }`}
            >
              <input type="radio" name="sensitive" className="sr-only" checked={sensitive === value} onChange={() => setSensitive(value)} />
              {label}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="flex justify-end">
        <button
          type="submit"
          disabled={!valid || busy}
          className="rounded-lg bg-indigo-500 px-5 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
        >
          Continue →
        </button>
      </div>
    </form>
  );
}

function BehaviorForm({
  state, busy, onSubmit,
}: {
  state: GuidedState;
  busy: boolean;
  onSubmit: (answers: Record<string, unknown>) => void;
}) {
  const saved = state.answers.behavior;
  const [actions, setActions] = useState(saved?.key_actions ?? "");
  const [constraints, setConstraints] = useState(saved?.constraints ?? "");
  const valid = actions.trim().length >= 10;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({ key_actions: actions.trim(), constraints: constraints.trim() || null });
      }}
      className="mx-auto max-w-xl space-y-5"
    >
      <div>
        <h2 className="text-lg font-medium">Describe the key actions your app should perform</h2>
        <textarea
          autoFocus
          value={actions}
          onChange={(e) => setActions(e.target.value)}
          rows={4}
          maxLength={4000}
          placeholder="e.g. Answer questions with citations, escalate to a human when unsure, log every answer…"
          className="mt-3 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
        />
      </div>
      <div>
        <h3 className="text-sm font-medium">
          Any constraints or rules? <span className="font-normal text-slate-400">(optional)</span>
        </h3>
        <textarea
          value={constraints ?? ""}
          onChange={(e) => setConstraints(e.target.value)}
          rows={2}
          maxLength={2000}
          placeholder="e.g. must respond within 3 seconds, must support Mandarin, must not store data beyond the session"
          className="mt-2 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
        />
      </div>
      <div className="flex justify-end">
        <button
          type="submit"
          disabled={!valid || busy}
          className="rounded-lg bg-indigo-500 px-5 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
        >
          Continue →
        </button>
      </div>
    </form>
  );
}

// ------------------------------------------------------------------ clarify

function ClarifyStep({
  state, busy, post,
}: {
  state: GuidedState;
  busy: boolean;
  post: (path: string, body?: unknown) => Promise<ChatSession | null>;
}) {
  const clar = state.clarification;
  const open = useMemo(() => clar.items.filter((i) => i.answer === null && !i.skipped), [clar.items]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [skips, setSkips] = useState<Record<string, boolean>>({});

  // First entry: fetch round 1 automatically
  useEffect(() => {
    if (clar.rounds === 0 && !busy) post("clarify", { answers: [] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clar.rounds]);

  async function submitAnswers() {
    const answers = open.map((item) => ({
      question_id: item.id,
      ...(skips[item.id] ? { skip: true } : { answer: drafts[item.id]?.trim() ?? "" }),
    }));
    const incomplete = answers.find((a) => !("skip" in a && a.skip) && !(a as { answer?: string }).answer);
    if (incomplete) return;
    await post("clarify", { answers });
    setDrafts({});
    setSkips({});
  }

  if (clar.rounds === 0) {
    return (
      <div className="mx-auto max-w-xl py-10 text-center text-slate-400">
        <p className="animate-pulse">marshal is reviewing your answers for gaps…</p>
      </div>
    );
  }

  const answered = clar.items.filter((i) => i.answer !== null || i.skipped);

  return (
    <div className="mx-auto max-w-xl space-y-5">
      <div>
        <h2 className="text-lg font-medium">A few follow-up questions</h2>
        <p className="mt-1 text-sm text-slate-400">
          Round {clar.rounds} of 3 · {clar.questions_total} of 15 questions max. Skip anything you
          don&apos;t know — it becomes a stated assumption.
        </p>
      </div>

      {answered.length > 0 && (
        <details className="rounded-lg border border-slate-800 bg-slate-950/50 px-4 py-2 text-sm">
          <summary className="cursor-pointer text-slate-400">
            {answered.length} answered earlier
          </summary>
          <ul className="mt-2 space-y-2">
            {answered.map((item) => (
              <li key={item.id}>
                <p className="text-slate-400">{item.question}</p>
                <p className="text-slate-400">↳ {item.skipped ? "(skipped)" : item.answer}</p>
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="space-y-4">
        {open.map((item) => (
          <div key={item.id} className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
            <p className="text-sm text-slate-200">🤖 {item.question}</p>
            <textarea
              value={drafts[item.id] ?? ""}
              onChange={(e) => setDrafts({ ...drafts, [item.id]: e.target.value })}
              disabled={skips[item.id]}
              rows={2}
              className="mt-2 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500 disabled:opacity-40"
            />
            <label className="mt-1 flex items-center gap-2 text-xs text-slate-400">
              <input
                type="checkbox"
                checked={skips[item.id] ?? false}
                onChange={(e) => setSkips({ ...skips, [item.id]: e.target.checked })}
              />
              Skip — use a sensible default
            </label>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between">
        <button
          onClick={() => post("proceed")}
          disabled={busy}
          className="text-sm text-slate-400 hover:text-slate-300"
        >
          Proceed with assumptions →
        </button>
        <button
          onClick={submitAnswers}
          disabled={busy || open.some((i) => !skips[i.id] && !(drafts[i.id] ?? "").trim())}
          className="rounded-lg bg-indigo-500 px-5 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
        >
          {busy ? "Thinking…" : "Submit answers"}
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ generate

function GenerateStep({
  session, state, generation, spec, busy, onGenerate, onSave, studioHref,
}: {
  session: ChatSession;
  state: GuidedState;
  generation: Generation | null;
  spec: FullSpecResponse | null;
  busy: boolean;
  onGenerate: () => void;
  onSave: () => Promise<void>;
  studioHref?: string | null;
}) {
  const [risk, setRisk] = useState<ProjectRisk | null>(null);
  const [saving, setSaving] = useState(false);
  const [approvedNow, setApprovedNow] = useState(false);
  const running = generation?.status === "running";
  const done = generation?.status === "done";
  // The chat surface authors the SPEC; the build lives on the project
  // workbench (FSD S8-04). Without this handoff CTA, guided users finish
  // here with a saved spec and no visible next step (tester feedback, 13.5Y).
  const approved = approvedNow || session.status === "saved";

  useEffect(() => {
    if (done && session.project_id) {
      api<ProjectRisk>(`/v1/projects/${session.project_id}/risk`).then(setRisk).catch(() => {});
    }
  }, [done, session.project_id]);

  const features = useMemo(() => {
    const req = spec?.requirements?.latest?.content;
    if (!req) return [];
    return (req.match(/^- (US|FR)-\d+: .+$/gm) ?? [])
      .slice(0, 5)
      .map((line: string) => line.replace(/^- (US|FR)-\d+: /, ""));
  }, [spec]);

  async function approve() {
    setSaving(true);
    try {
      await onSave();
      setApprovedNow(true);
      toast.success("Specification saved — continue on the project's Build tab.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mx-auto max-w-xl space-y-5">
      {!generation && (
        <>
          <div>
            <h2 className="text-lg font-medium">Ready to generate your specification</h2>
            <p className="mt-1 text-sm text-slate-400">
              marshal turns your answers into three documents: requirements, technical design,
              and an implementation plan.
            </p>
          </div>
          {state.assumptions.length > 0 && (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4">
              <h3 className="text-sm font-medium text-amber-300">
                Stated assumptions ({state.assumptions.length})
              </h3>
              <ul className="mt-2 space-y-1 text-sm text-slate-400">
                {state.assumptions.map((a) => (
                  <li key={a}>• {a}</li>
                ))}
              </ul>
            </div>
          )}
          <button
            onClick={onGenerate}
            disabled={busy}
            className="w-full rounded-lg bg-indigo-500 px-5 py-3 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            ✨ Generate my specification
          </button>
        </>
      )}

      {running && (
        <div className="space-y-3">
          <h2 className="text-lg font-medium">Generating…</h2>
          {(generation?.docs ?? []).map((doc) => (
            <div key={doc.type} className="flex items-center gap-3 rounded-lg border border-slate-800 bg-slate-900/60 px-4 py-3 text-sm">
              <span className="w-28 font-mono text-xs">{doc.type}.md</span>
              <span
                className={
                  doc.status === "done"
                    ? "text-emerald-400"
                    : doc.status === "generating"
                      // Same class of defect as the projects "building" badge:
                      // pulsing the text dips its contrast below AA. Brighter
                      // amber, no opacity animation on the label itself.
                      ? "text-amber-300"
                      : doc.status === "failed"
                        ? "text-red-400"
                        : "text-slate-400"
                }
              >
                {doc.status === "generating" ? "writing…" : doc.status}
              </span>
            </div>
          ))}
          <p className="text-xs text-slate-400">
            The full documents appear in the panel on the right as they complete.
          </p>
        </div>
      )}

      {done && (
        <div className="space-y-4">
          <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-5">
            <h2 className="text-lg font-medium">Your specification is ready 🎉</h2>
            <p className="mt-1 text-sm text-slate-400">
              &ldquo;{session.title}&rdquo; — review the documents on the right, then approve.
            </p>
            {features.length > 0 && (
              <ul className="mt-3 space-y-1 text-sm text-slate-300">
                {features.map((f: string) => (
                  <li key={f}>✓ {f}</li>
                ))}
              </ul>
            )}
            <div className="mt-3 flex items-center gap-2">
              {risk?.assessed && <RiskBadge level={risk.level ?? null} score={risk.score} />}
              {risk?.assessed && risk.decision === "pending" && (
                <span className="text-xs text-amber-300">
                  Needs admin risk review before deployment
                </span>
              )}
            </div>
          </div>
          <div className="flex gap-2">
            <button
              onClick={onGenerate}
              className="flex-1 rounded-lg border border-slate-700 px-4 py-2.5 text-sm hover:border-slate-500"
            >
              🔄 Regenerate
            </button>
            <button
              onClick={approve}
              disabled={saving}
              className="flex-1 rounded-lg bg-emerald-700 px-4 py-2.5 text-sm font-medium text-white hover:bg-emerald-600 disabled:opacity-40"
            >
              {saving ? "Saving…" : "✅ Approve & Save"}
            </button>
          </div>
          {approved && session.project_id && (
            <a
              href={`/projects/${session.project_id}?tab=build`}
              data-testid="guided-start-build"
              className="block rounded-lg bg-indigo-500 px-4 py-3 text-center text-sm font-medium text-white hover:bg-indigo-400"
            >
              🚀 Next: start your build — marshal generates the agent from this spec
            </a>
          )}
          {studioHref && (
            <a
              href={studioHref}
              className="block rounded-lg border border-indigo-500/40 bg-indigo-500/10 px-4 py-2.5 text-center text-sm text-indigo-300 hover:bg-indigo-500/20"
            >
              🎛 Open in Studio — keep refining beside the spec
            </a>
          )}
        </div>
      )}

      {generation?.status === "failed" && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/5 p-5">
          <h2 className="font-medium text-red-300">Generation hit a problem</h2>
          <p className="mt-1 text-sm text-slate-400">{generation.error ?? "Unknown error"}</p>
          <button
            onClick={onGenerate}
            className="mt-3 rounded-lg border border-slate-700 px-4 py-2 text-sm hover:border-slate-500"
          >
            Try again
          </button>
        </div>
      )}
    </div>
  );
}
