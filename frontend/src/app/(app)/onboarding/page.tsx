"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useUser } from "@/components/user-context";
import { api } from "@/lib/api";
import type { Persona, User } from "@/lib/types";

const USE_CASES = [
  "Customer support",
  "Internal tools",
  "Data analysis",
  "Document processing",
  "Workflow automation",
  "Not sure yet",
];

/** 3-step onboarding per FSD §4.4.1 (user-persona-profile spec R1). */
export default function OnboardingPage() {
  const { user, setUser } = useUser();
  const router = useRouter();
  const [step, setStep] = useState(1);
  const [persona, setPersona] = useState<Persona | null>(null);
  const [useCase, setUseCase] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const businessRole = user?.role === "business";

  async function finish() {
    if (!persona) return;
    setSaving(true);
    setError(null);
    try {
      const result = await api<{ user: User }>("/v1/users/me", {
        method: "PUT",
        body: JSON.stringify({
          persona,
          use_case: useCase ?? undefined,
          onboarding_completed: true,
        }),
      });
      setUser(result.user);
      router.replace("/home");
    } catch (e) {
      setError((e as Error).message);
      setSaving(false);
    }
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-slate-950 px-4 text-slate-100">
      <div className="w-full max-w-2xl">
        <h1 className="text-center text-2xl font-semibold">Welcome to marshal 🏭</h1>
        <p className="mt-1 text-center text-sm text-slate-400">
          Let&apos;s set up your experience in {step === 3 ? "one last" : "3 quick"} step{step === 3 ? "" : "s"}.
        </p>

        {step === 1 && (
          <div className="mt-8">
            <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-slate-400">
              Step 1 of 3 — How would you describe yourself?
            </h2>
            <div className="grid gap-4 sm:grid-cols-2">
              <PersonaCard
                emoji="💼"
                title="Business User"
                quote="I have ideas for AI agents but I'm not technical. Guide me step by step."
                bullets={["Guided wizard experience", "Pre-built templates", "Simple language"]}
                selected={persona === "business"}
                onSelect={() => setPersona("business")}
              />
              <PersonaCard
                emoji="⚡"
                title="Power User"
                quote="I know AI/ML and want full control over models and code."
                bullets={["Full chat + editor", "Model parameter tuning", "Advanced deployment"]}
                selected={persona === "power"}
                disabled={businessRole}
                disabledNote="Requires admin approval for your account"
                onSelect={() => setPersona("power")}
              />
            </div>
            <p className="mt-3 text-center text-xs text-slate-400">
              ℹ️ You can change this anytime in Settings
            </p>
            <NavButtons
              nextDisabled={!persona}
              onNext={() => setStep(2)}
            />
          </div>
        )}

        {step === 2 && (
          <div className="mt-8">
            <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-slate-400">
              Step 2 of 3 — What&apos;s your primary use case? <span className="normal-case text-slate-400">(optional)</span>
            </h2>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {USE_CASES.map((uc) => (
                <button
                  key={uc}
                  onClick={() => setUseCase(uc === useCase ? null : uc)}
                  className={`rounded-lg border px-3 py-3 text-sm transition ${
                    useCase === uc
                      ? "border-indigo-500 bg-indigo-500/10 text-indigo-200"
                      : "border-slate-700 bg-slate-900/60 text-slate-300 hover:border-slate-500"
                  }`}
                >
                  {uc}
                </button>
              ))}
            </div>
            <NavButtons onBack={() => setStep(1)} onNext={() => setStep(3)} nextLabel={useCase ? "Next" : "Skip"} />
          </div>
        )}

        {step === 3 && (
          <div className="mt-8 text-center">
            <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-slate-400">
              Step 3 of 3 — Here&apos;s your workspace!
            </h2>
            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-8">
              <p className="text-slate-300">
                You&apos;re set up as a{" "}
                <span className="font-semibold text-indigo-300">
                  {persona === "power" ? "Power User" : "Business User"}
                </span>
                {useCase ? ` focused on ${useCase.toLowerCase()}` : ""}.
              </p>
              <p className="mt-2 text-sm text-slate-400">
                Next you&apos;ll get a quick interactive tour of the main features:
                chat, spec generation, projects, and Enclave deployment.
              </p>
              {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
              <button
                onClick={finish}
                disabled={saving}
                className="mt-6 rounded-lg bg-indigo-500 px-6 py-2.5 font-medium text-white transition hover:bg-indigo-400 disabled:opacity-50"
              >
                {saving ? "Setting up…" : "Start the tour →"}
              </button>
            </div>
            <NavButtons onBack={() => setStep(2)} />
          </div>
        )}

        <div className="mt-8 flex justify-center gap-2">
          {[1, 2, 3].map((s) => (
            <span
              key={s}
              className={`h-2 w-2 rounded-full ${s <= step ? "bg-indigo-400" : "bg-slate-700"}`}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function PersonaCard({
  emoji,
  title,
  quote,
  bullets,
  selected,
  disabled,
  disabledNote,
  onSelect,
}: {
  emoji: string;
  title: string;
  quote: string;
  bullets: string[];
  selected: boolean;
  disabled?: boolean;
  disabledNote?: string;
  onSelect: () => void;
}) {
  return (
    <button
      onClick={onSelect}
      disabled={disabled}
      className={`rounded-xl border p-5 text-left transition ${
        selected
          ? "border-indigo-500 bg-indigo-500/10"
          : disabled
            ? "cursor-not-allowed border-slate-800 bg-slate-900/40 opacity-50"
            : "border-slate-700 bg-slate-900/60 hover:border-slate-500"
      }`}
    >
      <div className="text-2xl">{emoji}</div>
      <h3 className="mt-2 font-semibold">{title}</h3>
      <p className="mt-1 text-sm italic text-slate-400">&ldquo;{quote}&rdquo;</p>
      <ul className="mt-3 space-y-1 text-sm text-slate-300">
        {bullets.map((b) => (
          <li key={b}>• {b}</li>
        ))}
      </ul>
      {disabled && disabledNote && (
        <p className="mt-3 text-xs text-amber-400">{disabledNote}</p>
      )}
    </button>
  );
}

function NavButtons({
  onBack,
  onNext,
  nextDisabled,
  nextLabel = "Next",
}: {
  onBack?: () => void;
  onNext?: () => void;
  nextDisabled?: boolean;
  nextLabel?: string;
}) {
  return (
    <div className="mt-6 flex justify-between">
      {onBack ? (
        <button onClick={onBack} className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200">
          ← Back
        </button>
      ) : (
        <span />
      )}
      {onNext && (
        <button
          onClick={onNext}
          disabled={nextDisabled}
          className="rounded-lg bg-indigo-500 px-5 py-2 text-sm font-medium text-white transition hover:bg-indigo-400 disabled:opacity-40"
        >
          {nextLabel} →
        </button>
      )}
    </div>
  );
}
