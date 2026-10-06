"use client";

import { useEffect, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { useRouter } from "next/navigation";
import BuildInfoBadge from "@/components/shell/BuildInfoBadge";
import { useUser } from "@/components/user-context";
import { api } from "@/lib/api";
import { toast } from "sonner";
import type {
  DemoExperience,
  NotificationPrefs,
  Persona,
  UsageSummary,
  User,
  UserStats,
} from "@/lib/types";

// Installation support contact — build-time public env, inlined by Next.js.
// Both are optional: the Support block renders only when an e-mail is set and
// the hours row only when hours are set (no installation-specific fallback).
const SUPPORT_EMAIL = process.env.NEXT_PUBLIC_SUPPORT_EMAIL || "";
const SUPPORT_HOURS = process.env.NEXT_PUBLIC_SUPPORT_HOURS || "";

/** Profile dashboard (S1-06, FSD §4.4.3 subset) + persona switching (§4.4.4). */
export default function ProfilePage() {
  const { user, setUser, refresh } = useUser();
  const router = useRouter();
  const [stats, setStats] = useState<UserStats | null>(null);
  const [name, setName] = useState("");
  const [editingName, setEditingName] = useState(false);
  const [pendingNote, setPendingNote] = useState(false);
  const [saving, setSaving] = useState(false);
  const [previewSaving, setPreviewSaving] = useState(false);

  useEffect(() => {
    api<UserStats>("/v1/users/me/stats").then(setStats).catch(() => {});
  }, []);

  useEffect(() => {
    if (user) {
      setName(user.name ?? "");
      setPendingNote(user.persona_upgrade_requested);
    }
  }, [user]);

  if (!user) return <div className="text-slate-400">Loading profile…</div>;

  async function update(payload: Record<string, unknown>) {
    setSaving(true);
    try {
      const result = await api<{ user: User; pending_approval: boolean }>("/v1/users/me", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      setUser(result.user);
      if (result.pending_approval) setPendingNote(true);
    } finally {
      setSaving(false);
    }
  }

  async function switchPersona(persona: Persona) {
    if (persona === user!.persona) return;
    await update({ persona });
  }

  async function switchDemoExperience(experience: DemoExperience) {
    if (!user?.can_demo_switch || experience === user.experience_view) return;
    setPreviewSaving(true);
    try {
      await api<User>("/v1/users/me/demo-experience", {
        method: "PUT",
        body: JSON.stringify({ experience_view: experience }),
      });
      await refresh();
      const label = `${experience[0].toUpperCase()}${experience.slice(1)}`;
      toast.success(`${label} navigation selected.`);
    } catch (error) {
      toast.error((error as Error).message);
    } finally {
      setPreviewSaving(false);
    }
  }

  const joined = new Date(user.created_at).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">My Profile</h1>

      {/* Release-phase indicator (§7 taxonomy): S1–S16 are the Alpha track.
          S16-06: the build identity sits beside the badge — "Alpha" plus
          exactly which build, with a path to what changed. */}
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-200/90">
        <span className="rounded-full border border-amber-400/50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-amber-300">
          Alpha
        </span>
        <span>
          This is an Alpha Build — the platform is under active development; features may
          evolve between releases.
        </span>
        <BuildInfoBadge />
      </div>

      <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-6">
        <div className="flex items-start gap-4">
          <span className="flex h-14 w-14 items-center justify-center rounded-full bg-indigo-600 text-xl font-bold uppercase">
            {(user.name ?? user.email).slice(0, 1)}
          </span>
          <div className="flex-1">
            {editingName ? (
              <div className="flex items-center gap-2">
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-lg"
                  maxLength={120}
                />
                <button
                  disabled={saving}
                  onClick={async () => {
                    await update({ name });
                    setEditingName(false);
                  }}
                  className="rounded bg-indigo-500 px-3 py-1 text-sm hover:bg-indigo-400"
                >
                  Save
                </button>
                <button
                  onClick={() => setEditingName(false)}
                  className="px-2 py-1 text-sm text-slate-400"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <div className="flex items-center gap-3">
                <h2 className="text-lg font-medium">{user.name ?? "Unnamed user"}</h2>
                <button
                  onClick={() => setEditingName(true)}
                  className="text-xs text-indigo-400 hover:text-indigo-300"
                >
                  Edit
                </button>
              </div>
            )}
            <p className="text-sm text-slate-400">{user.email}</p>
            <p className="mt-1 text-xs text-slate-400">
              {user.persona === "power" ? "Power User" : "Business User"} · Role:{" "}
              <span className="uppercase">{user.role}</span> · Joined {joined}
            </p>
          </div>
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-6">
        <h2 className="mb-1 font-medium">Quick stats</h2>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Projects" value={stats?.projects} icon="🗂️" />
          <Stat label="Specs" value={stats?.specs} icon="📝" />
          <Stat label="Chat sessions" value={stats?.sessions} icon="💬" />
          <Stat label="Deployments" value={stats?.deployments} icon="🚀" />
        </div>
      </section>

      {!user.can_demo_switch ? (
        <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-6">
          <h2 className="font-medium">Experience mode</h2>
          <p className="mt-1 text-sm text-slate-400">
            Business mode keeps things simple; Power mode unlocks technical controls.
          </p>
          <div className="mt-4 flex gap-3">
            <PersonaButton
              label="💼 Business"
              active={user.persona === "business"}
              onClick={() => switchPersona("business")}
              disabled={saving}
            />
            <PersonaButton
              label="⚡ Power"
              active={user.persona === "power"}
              onClick={() => switchPersona("power")}
              disabled={saving}
            />
          </div>
          {pendingNote && user.role === "business" && (
            <p className="mt-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
              Power User access requested — pending admin approval. You&apos;ll keep the
              Business experience until an admin approves the upgrade.
            </p>
          )}
        </section>
      ) : (
        <section className="rounded-xl border border-indigo-500/40 bg-indigo-500/5 p-6">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-medium">Navigation preview</h2>
            <span className="rounded-full border border-indigo-400/50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-indigo-300">
              Presentation only
            </span>
          </div>
          <p className="mt-2 text-sm text-slate-300">
            Business and Power change navigation presentation only; they do not simulate
            those personas. Your real role, persona, permissions, Cognito groups, and
            API access never change.
          </p>
          <div className="mt-4 flex flex-wrap gap-3">
            {user.allowed_demo_experiences.map((experience) => {
              const label =
                experience === "business"
                  ? "💼 Business navigation"
                  : "⚡ Power navigation";
              const selected = user.experience_view === experience;
              return (
                <button
                  key={experience}
                  type="button"
                  onClick={() => switchDemoExperience(experience)}
                  disabled={previewSaving || selected}
                  className={`rounded-lg border px-4 py-2 text-sm transition ${
                    selected
                      ? "border-indigo-500 bg-indigo-500/15 text-indigo-200"
                      : "border-slate-700 text-slate-300 hover:border-slate-500"
                  } disabled:cursor-default disabled:opacity-70`}
                >
                  {label}
                  {selected && <span className="ml-2 text-xs">✓ selected</span>}
                </button>
              );
            })}
          </div>
          {user.experience_view === null && (
            <p className="mt-3 text-xs text-slate-400">
              No navigation preview is selected; navigation follows your real persona.
            </p>
          )}
        </section>
      )}

      {SUPPORT_EMAIL && (
        <section
          className="rounded-xl border border-slate-800 bg-slate-900/60 p-6"
          aria-labelledby="support-heading"
        >
          <h2 id="support-heading" className="font-medium">Support</h2>
          <dl className="mt-4 grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-xs uppercase tracking-wide text-slate-400">Contact</dt>
              <dd className="mt-1">
                <a className="text-indigo-300 hover:text-indigo-200" href={`mailto:${SUPPORT_EMAIL}`}>
                  {SUPPORT_EMAIL}
                </a>
              </dd>
            </div>
            {SUPPORT_HOURS && (
              <div>
                <dt className="text-xs uppercase tracking-wide text-slate-400">Support hours</dt>
                <dd className="mt-1 text-slate-300">{SUPPORT_HOURS}</dd>
              </div>
            )}
          </dl>
        </section>
      )}

      <UsageSection />
      <MfaSection isAdmin={user.role === "admin"} />
      {(user.persona === "power" || user.role === "admin") && <MySubmissionsSection />}
      <NotificationPrefsSection />

      <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-6">
        <h2 className="font-medium">Walkthrough</h2>
        <p className="mt-1 text-sm text-slate-400">
          Replay the guided tour of marshal&apos;s main features.
        </p>
        <button
          onClick={() => router.push("/home?tour=replay")}
          className="mt-3 rounded-lg border border-slate-600 px-4 py-2 text-sm hover:border-indigo-400 hover:text-indigo-300"
        >
          ▶ Replay tour
        </button>
      </section>
    </div>
  );
}

const PREF_LABELS: Record<string, string> = {
  generation_complete: "Spec generation complete",
  deploy_succeeded: "Deployment succeeded",
  deploy_failed: "Deployment failed",
  cost_threshold: "Cost threshold reached",
  cost_cap_reached: "Cost cap reached",
  risk_review_requested: "Risk review requested",
  risk_decided: "Risk review decided",
  risk_changes_requested: "Risk changes requested",
  risk_escalated: "Risk review escalated",
  sample_published: "Marketplace sample published",
  project_shared: "Project shared with me",
  project_role_changed: "My project role changed",
  project_unshared: "Removed from a project",
  comment_reply: "Reply to my spec comment",
  ownership_transferred: "Project ownership transferred",
  submission_received: "Marketplace submission received (admins)",
  submission_decided: "My marketplace submission decided",
  build_ready: "Application build ready",
  build_failed: "Application build failed",
};

const SUBMISSION_STATUS_STYLE: Record<string, string> = {
  submitted: "bg-amber-500/15 text-amber-300",
  rejected: "bg-red-500/15 text-red-300",
  withdrawn: "bg-slate-700/40 text-slate-400",
  draft: "bg-indigo-500/15 text-indigo-300",
  published: "bg-emerald-500/15 text-emerald-300",
  archived: "bg-slate-800/80 text-slate-400",
};

function MySubmissionsSection() {
  const [items, setItems] = useState<import("@/lib/types").Submission[] | null>(null);

  const load = () =>
    api<import("@/lib/types").Submission[]>("/v1/marketplace/my-submissions")
      .then(setItems)
      .catch(() => setItems([]));

  useEffect(() => {
    load();
  }, []);

  async function withdraw(id: string) {
    try {
      await api(`/v1/marketplace/submissions/${id}/withdraw`, { method: "POST" });
      load();
    } catch {
      /* surfaced on reload */
    }
  }

  if (items === null || items.length === 0) return null;
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-6">
      <h2 className="font-medium">My marketplace submissions</h2>
      <p className="mt-1 text-sm text-slate-400">
        Projects you&apos;ve shared for the sample catalog. Approved submissions are curated and
        published by an admin, with credit to you.
      </p>
      <div className="mt-4 space-y-2">
        {items.map((s) => (
          <div
            key={s.id}
            className="rounded-lg border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm"
          >
            <div className="flex items-center justify-between gap-3">
              <p className="min-w-0 truncate font-medium">{s.title}</p>
              <span
                className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                  SUBMISSION_STATUS_STYLE[s.status] ?? "bg-slate-700/40 text-slate-400"
                }`}
              >
                {s.status === "draft" ? "approved · in curation" : s.status}
              </span>
            </div>
            <p className="mt-0.5 text-xs text-slate-400">
              {s.project_name && <>From &ldquo;{s.project_name}&rdquo; · </>}
              {s.submitted_at && new Date(s.submitted_at).toLocaleString()}
            </p>
            {s.status === "rejected" && s.review_feedback && (
              <p className="mt-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
                Feedback: {s.review_feedback} — you can revise the project and resubmit.
              </p>
            )}
            {s.status === "submitted" && (
              <button
                onClick={() => withdraw(s.id)}
                className="mt-2 text-xs text-slate-400 underline hover:text-slate-300"
              >
                Withdraw
              </button>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function NotificationPrefsSection() {
  const [data, setData] = useState<NotificationPrefs | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api<NotificationPrefs>("/v1/users/me/notification-prefs").then(setData).catch(() => {});
  }, []);

  async function toggle(event: string, channel: "in_app" | "email") {
    if (!data) return;
    const next = {
      ...data.prefs,
      [event]: { ...data.prefs[event], [channel]: !data.prefs[event][channel] },
    };
    setData({ ...data, prefs: next });
    setSaving(true);
    try {
      const saved = await api<NotificationPrefs>("/v1/users/me/notifications", {
        method: "PUT",
        body: JSON.stringify(next),
      });
      setData(saved);
    } catch {
      toast.error("Could not save notification preferences.");
    } finally {
      setSaving(false);
    }
  }

  if (!data) return null;

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-6">
      <h2 className="font-medium">Notifications</h2>
      <p className="mt-1 text-sm text-slate-400">
        Which events reach you, and where.
        {!data.email_enabled && " Email delivery is not yet enabled on this platform."}
      </p>
      <table className="mt-4 w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
          <tr>
            <th className="py-2">Event</th>
            <th className="w-20 py-2 text-center">In-app</th>
            <th className="w-20 py-2 text-center">Email</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">
          {Object.entries(data.prefs).map(([event, channels]) => (
            <tr key={event}>
              <td className="py-2 text-slate-300">{PREF_LABELS[event] ?? event}</td>
              <td className="py-2 text-center">
                <input
                  type="checkbox"
                  aria-label={`${PREF_LABELS[event] ?? event} in-app`}
                  checked={channels.in_app}
                  disabled={saving}
                  onChange={() => toggle(event, "in_app")}
                />
              </td>
              <td className="py-2 text-center">
                <input
                  type="checkbox"
                  aria-label={`${PREF_LABELS[event] ?? event} email`}
                  checked={channels.email}
                  disabled={saving || !data.email_enabled}
                  title={data.email_enabled ? undefined : "Email delivery not yet enabled"}
                  onChange={() => toggle(event, "email")}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function UsageSection() {
  const [usage, setUsage] = useState<UsageSummary | null>(null);

  useEffect(() => {
    api<UsageSummary>("/v1/users/me/usage").then(setUsage).catch(() => {});
  }, []);

  if (!usage) return null;
  const pct = usage.cap_usd ? Math.min((usage.mtd_usd / usage.cap_usd) * 100, 100) : null;

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-6">
      <h2 className="font-medium">My usage — {usage.month}</h2>
      <p className="mt-1 text-sm text-slate-400">
        ${usage.mtd_usd.toFixed(2)} model spend this month
        {usage.cap_usd != null && ` of $${usage.cap_usd.toFixed(0)} cap`}
        {usage.at_cap === "block" && usage.cap_usd != null && " (hard cap)"}
        {usage.enclave_usd != null && ` · $${usage.enclave_usd.toFixed(2)} Enclave infra`}
      </p>
      {pct != null && (
        <div
          className="mt-3 h-2 overflow-hidden rounded bg-slate-800"
          role="progressbar"
          aria-valuenow={Math.round(pct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Monthly usage"
        >
          <div
            className={`h-full ${pct > 90 ? "bg-red-500" : pct > 70 ? "bg-amber-500" : "bg-emerald-500"}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      {usage.projects.length > 0 && (
        <ul className="mt-4 space-y-1.5 text-sm">
          {usage.projects.slice(0, 5).map((p) => (
            <li key={p.project_id} className="flex items-center justify-between">
              <span className="truncate text-slate-300">{p.name}</span>
              <span className="text-slate-400">
                ${p.usd.toFixed(2)}
                {p.enclave_usd != null && ` (+$${p.enclave_usd.toFixed(2)} infra)`}
                {p.budget_usd != null && ` / $${p.budget_usd.toFixed(0)}`}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Stat({ label, value, icon }: { label: string; value?: number; icon: string }) {
  return (
    <div>
      <div className="text-xl">{icon}</div>
      <div className="mt-1 text-2xl font-semibold">{value ?? "…"}</div>
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
    </div>
  );
}

function PersonaButton({
  label,
  active,
  onClick,
  disabled,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  disabled: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled || active}
      className={`rounded-lg border px-4 py-2 text-sm transition ${
        active
          ? "border-indigo-500 bg-indigo-500/15 text-indigo-200"
          : "border-slate-700 text-slate-300 hover:border-slate-500"
      } disabled:cursor-default`}
    >
      {label}
      {active && <span className="ml-2 text-xs">✓ current</span>}
    </button>
  );
}


interface MfaState {
  enrolled: boolean;
  preferred?: string | null;
  session_mfa: boolean;
  required_for_admins: boolean;
  required_for_me: boolean;
}

/** Multi-factor authentication (S14-02). TOTP enrollment runs through the
 *  backend because Cognito's hosted UI cannot self-enrol while the pool's MFA
 *  setting is optional. */
function MfaSection({ isAdmin }: { isAdmin: boolean }) {
  const [state, setState] = useState<MfaState | null>(null);
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () =>
    api<MfaState>("/v1/users/me/mfa")
      .then(setState)
      .catch(() => setState(null));

  useEffect(() => {
    load();
  }, []);

  async function start() {
    setBusy(true);
    try {
      setSetup(await api("/v1/users/me/mfa/start", { method: "POST" }));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    setBusy(true);
    try {
      await api("/v1/users/me/mfa/confirm", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      toast.success("Authenticator app enabled — it applies at your next sign-in.");
      setSetup(null);
      setCode("");
      load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setBusy(true);
    try {
      await api("/v1/users/me/mfa", { method: "DELETE" });
      toast.success("Authenticator app removed.");
      load();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!state) return null;
  const mustEnrol = state.required_for_me && !state.enrolled;

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="font-medium">
            Two-factor authentication
            <span
              className={`ml-3 rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                state.enrolled
                  ? "bg-emerald-500/15 text-emerald-300"
                  : "bg-slate-700/60 text-slate-300"
              }`}
            >
              {state.enrolled ? "On" : "Off"}
            </span>
          </h2>
          <p className="mt-1 text-sm text-slate-400">
            {state.enrolled
              ? "An authenticator app is required at sign-in."
              : "Add an authenticator app (TOTP) for a second factor at sign-in."}
            {isAdmin && state.required_for_admins && (
              <span className="ml-1 text-amber-300">Required for administrators.</span>
            )}
          </p>
        </div>
        {!setup &&
          (state.enrolled ? (
            <button
              onClick={disable}
              disabled={busy || state.required_for_me}
              title={
                state.required_for_me
                  ? "Required for administrators — cannot be removed"
                  : undefined
              }
              className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs hover:border-slate-500 disabled:opacity-40"
            >
              Remove
            </button>
          ) : (
            <button
              onClick={start}
              disabled={busy}
              className="rounded-lg bg-indigo-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
            >
              Set up
            </button>
          ))}
      </div>

      {mustEnrol && !setup && (
        <p className="mt-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
          Administrator actions are blocked until you set up an authenticator app and
          sign in again.
        </p>
      )}

      {state.enrolled && !state.session_mfa && (
        <p className="mt-3 rounded-md border border-slate-700 bg-slate-950/60 px-3 py-2 text-xs text-slate-400">
          This session signed in before two-factor was enabled. Sign out and back in to
          use it.
        </p>
      )}

      {setup && (
        <div className="mt-4 space-y-3 rounded-lg border border-slate-700 bg-slate-950/60 p-4">
          <p className="text-sm text-slate-300">
            1. Scan this QR code with your authenticator app (Microsoft Authenticator,
            Google Authenticator, 1Password):
          </p>
          {/* White tile: authenticator cameras need dark modules on a light
              background — the QR must not inherit the dark theme. */}
          <div className="inline-block rounded-lg bg-white p-3">
            <QRCodeSVG
              value={setup.otpauth_uri}
              size={168}
              title="QR code for authenticator app enrollment"
            />
          </div>
          <details className="text-xs text-slate-400">
            <summary className="cursor-pointer select-none text-slate-300 hover:text-slate-100">
              Can&apos;t scan? Enter the key manually
            </summary>
            <code className="mt-2 block break-all rounded bg-slate-900 px-3 py-2 font-mono text-xs text-indigo-300">
              {setup.secret}
            </code>
            <p className="mt-1">
              Manual entry: account <span className="text-slate-300">marshal</span>, type
              time-based, 6 digits, 30 seconds.
            </p>
          </details>
          <label className="block text-sm text-slate-300">
            2. Enter the 6-digit code it shows
            <input
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
              inputMode="numeric"
              placeholder="000000"
              aria-label="Authenticator code"
              className="mt-1 block w-32 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono tracking-widest outline-none focus:border-indigo-500"
            />
          </label>
          <div className="flex gap-2">
            <button
              onClick={confirm}
              disabled={busy || code.length !== 6}
              className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
            >
              {busy ? "Verifying…" : "Enable"}
            </button>
            <button
              onClick={() => {
                setSetup(null);
                setCode("");
              }}
              className="rounded-lg px-3 py-2 text-sm text-slate-400 hover:text-slate-200"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
