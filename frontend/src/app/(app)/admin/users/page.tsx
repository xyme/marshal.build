"use client";

import { useCallback, useEffect, useState } from "react";
import { useUser } from "@/components/user-context";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import type {
  AdminUser,
  AdminUserList,
  AdminUserStats,
  DemoExperience,
} from "@/lib/types";

/** Admin > Users (S3-08, FSD §4.4.5): roles, personas, suspension, approvals. */
export default function AdminUsersPage() {
  const readonly = useAdminReadonly();
  const { user: me } = useUser();
  const [stats, setStats] = useState<AdminUserStats | null>(null);
  const [data, setData] = useState<AdminUserList | null>(null);
  const [q, setQ] = useState("");
  const [role, setRole] = useState("");
  const [status, setStatus] = useState("");
  const [editing, setEditing] = useState<AdminUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const params = new URLSearchParams({ page_size: "100" });
      if (q.trim()) params.set("q", q.trim());
      if (role) params.set("role", role);
      if (status) params.set("status", status);
      setData(await api<AdminUserList>(`/v1/admin/users?${params}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [q, role, status]);

  useEffect(() => {
    const t = setTimeout(load, 250);
    return () => clearTimeout(t);
  }, [load]);

  useEffect(() => {
    api<AdminUserStats>("/v1/admin/users/stats").then(setStats).catch(() => {});
  }, []);

  async function decidePersona(target: AdminUser, approve: boolean) {
    try {
      await api(`/v1/admin/users/${target.id}/persona-request/decide`, {
        method: "POST",
        body: JSON.stringify({ approve }),
      });
      setNotice(
        approve
          ? `${target.email} upgraded to Power persona.`
          : `Persona request from ${target.email} declined.`
      );
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Users</h1>
        <p className="mt-1 text-sm text-slate-400">
          Roles, personas, suspension and persona-upgrade approvals.
        </p>
      </div>

      {stats && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <StatCard label="Total users" value={stats.total} />
          <StatCard label="Active (30d)" value={stats.active_30d} />
          <StatCard label="Business" value={stats.business} />
          <StatCard label="Power" value={stats.power} />
          <StatCard label="Admins" value={stats.admins} />
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-800 bg-slate-900/40 p-3 text-sm">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search name or email…"
          className="w-full max-w-xs rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 outline-none focus:border-indigo-500"
        />
        <select
          value={role}
          onChange={(e) => setRole(e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5"
        >
          <option value="">All roles</option>
          <option value="business">Business</option>
          <option value="power">Power</option>
          <option value="admin">Admin</option>
        </select>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5"
        >
          <option value="">Any status</option>
          <option value="active">Active</option>
          <option value="suspended">Suspended</option>
        </select>
        {/* S15-03: the artifact a reviewer signs off — role, teams, last active.
            Blob download (same pattern as the audit export) so the request
            carries the session and the filename comes from us. */}
        <button
          type="button"
          onClick={async () => {
            try {
              const response = await fetch(
                "/api/backend/v1/admin/users/access-review?format=csv"
              );
              if (!response.ok) throw new Error(`Export failed (${response.status})`);
              const url = URL.createObjectURL(await response.blob());
              const link = document.createElement("a");
              link.href = url;
              link.download = `marshal-access-review-${new Date()
                .toISOString()
                .slice(0, 10)}.csv`;
              link.click();
              URL.revokeObjectURL(url);
            } catch (e) {
              setError((e as Error).message);
            }
          }}
          className="ml-auto rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500"
          title="Download who has what access, with last-active dates and dormant flags"
        >
          ⬇ Access review (CSV)
        </button>
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

      {data === null ? (
        <p className="text-slate-400">Loading…</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/80 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-4 py-3">User</th>
                <th className="px-4 py-3">Role</th>
                <th className="px-4 py-3">Persona</th>
                <th className="px-4 py-3">Account</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Projects</th>
                <th className="px-4 py-3">Last active</th>
                <th className="px-4 py-3" aria-label="Actions" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {data.items.map((u) => (
                <tr key={u.id} className="bg-slate-900/40 hover:bg-slate-900/70">
                  <td className="px-4 py-3">
                    <div className="font-medium">{u.name ?? "—"}</div>
                    <div className="text-xs text-slate-400">{u.email}</div>
                  </td>
                  <td className="px-4 py-3">
                    <span className="capitalize text-slate-300">{u.role}</span>
                    {u.role_source === "admin" && (
                      <span className="ml-1 text-[10px] uppercase text-slate-400" title="Set by an admin">
                        pinned
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span className="capitalize text-slate-300">{u.persona ?? "—"}</span>
                    {u.persona_upgrade_requested && (
                      <span className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-400">
                        upgrade requested
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                        u.account_class === "demo"
                          ? "bg-indigo-500/15 text-indigo-300"
                          : "bg-slate-800 text-slate-400"
                      }`}
                    >
                      {u.account_class}
                    </span>
                    {u.experience_view && (
                      <div className="mt-1 text-xs capitalize text-slate-400">
                        {`${u.experience_view} navigation`}
                      </div>
                    )}
                    {u.admin_readonly && (
                      <span className="mt-1 inline-block rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-300">
                        admin view-only
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                        u.status === "active"
                          ? "bg-emerald-500/15 text-emerald-400"
                          : "bg-red-500/15 text-red-400"
                      }`}
                    >
                      {u.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-400">{u.project_count}</td>
                  <td className="px-4 py-3 text-slate-400">
                    {u.last_active_at ? new Date(u.last_active_at).toLocaleDateString() : "—"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex items-center justify-end gap-1">
                      {u.persona_upgrade_requested && !readonly && (
                        <>
                          <button
                            onClick={() => decidePersona(u, true)}
                            className="rounded-md bg-emerald-600/80 px-2 py-1 text-xs text-white hover:bg-emerald-500"
                          >
                            Approve
                          </button>
                          <button
                            onClick={() => decidePersona(u, false)}
                            className="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:border-slate-500"
                          >
                            Decline
                          </button>
                        </>
                      )}
                      <button
                        onClick={() => setEditing(u)}
                        disabled={readonly}
                        aria-label={`Edit ${u.email}`}
                        className="rounded-md px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-white disabled:opacity-40"
                      >
                        ✏️
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <EditUserModal
          target={editing}
          isSelf={editing.id === me?.id}
          onClose={() => setEditing(null)}
          onSaved={(msg) => {
            setEditing(null);
            setNotice(msg);
            load();
          }}
          onError={(msg) => setError(msg)}
        />
      )}
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <div className="text-2xl font-semibold">{value}</div>
      <div className="mt-0.5 text-xs uppercase tracking-wide text-slate-400">{label}</div>
    </div>
  );
}

function EditUserModal({
  target,
  isSelf,
  onClose,
  onSaved,
  onError,
}: {
  target: AdminUser;
  isSelf: boolean;
  onClose: () => void;
  onSaved: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [role, setRole] = useState(target.role);
  const [persona, setPersona] = useState(target.persona ?? "business");
  const [status, setStatus] = useState(target.status);
  const [accountClass, setAccountClass] = useState(target.account_class);
  const [experienceView, setExperienceView] = useState<DemoExperience | "">(
    target.experience_view ?? ""
  );
  const [budget, setBudget] = useState(
    target.budget_override_usd != null ? String(target.budget_override_usd) : ""
  );
  const [adminReadonly, setAdminReadonly] = useState(target.admin_readonly);
  const [busy, setBusy] = useState(false);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const payload: Record<string, unknown> = {};
      if (role !== target.role) payload.role = role;
      if (persona !== (target.persona ?? "business")) payload.persona = persona;
      if (status !== target.status) payload.status = status;
      if (accountClass !== target.account_class) payload.account_class = accountClass;
      if (experienceView === "" && target.experience_view !== null) {
        payload.clear_experience_view = true;
      } else if (experienceView !== "" && experienceView !== target.experience_view) {
        payload.experience_view = experienceView;
      }
      const budgetValue = budget.trim() === "" ? null : Number(budget);
      if (budgetValue === null && target.budget_override_usd != null) payload.clear_budget = true;
      else if (budgetValue != null && budgetValue !== target.budget_override_usd)
        payload.budget_override_usd = budgetValue;
      if (adminReadonly !== target.admin_readonly) payload.admin_readonly = adminReadonly;
      if (Object.keys(payload).length === 0) {
        onClose();
        return;
      }
      await api(`/v1/admin/users/${target.id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      onSaved(
        `${target.email} updated.` +
          (payload.role
            ? " Role change is effective on their next request; sign-in refreshes their session claim."
            : "")
      );
    } catch (err) {
      onError((err as Error).message);
      onClose();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <form
        onSubmit={save}
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border border-slate-700 bg-slate-900 p-6"
      >
        <h2 className="text-lg font-medium">Edit user</h2>
        <p className="mt-0.5 text-sm text-slate-400">{target.email}</p>

        <fieldset className="mt-4 text-sm text-slate-400">
          <legend>Role</legend>
          <div className="mt-1 flex gap-4">
            {(["business", "power", "admin"] as const).map((r) => (
              <label key={r} className="flex items-center gap-1.5 capitalize">
                <input
                  type="radio"
                  name="role"
                  checked={role === r}
                  onChange={() => setRole(r)}
                  disabled={isSelf && r !== "admin"}
                />
                {r}
              </label>
            ))}
          </div>
          {isSelf && <p className="mt-1 text-xs text-slate-400">You cannot demote your own account.</p>}
        </fieldset>

        <fieldset className="mt-4 text-sm text-slate-400">
          <legend>Persona</legend>
          <div className="mt-1 flex gap-4">
            {(["business", "power"] as const).map((p) => (
              <label key={p} className="flex items-center gap-1.5 capitalize">
                <input
                  type="radio"
                  name="persona"
                  checked={persona === p}
                  onChange={() => setPersona(p)}
                />
                {p}
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className="mt-4 rounded-lg border border-indigo-500/30 bg-indigo-500/5 p-3 text-sm text-slate-400">
          <legend className="px-1 text-slate-300">Navigation preview</legend>
          <p className="text-xs text-slate-400">
            Business and Power change navigation presentation only. Neither choice
            changes the user&apos;s role, persona, permissions, or Cognito groups.
          </p>
          <div className="mt-3 flex gap-4">
            {(["standard", "demo"] as const).map((value) => (
              <label key={value} className="flex items-center gap-1.5 capitalize">
                <input
                  type="radio"
                  name="account-class"
                  checked={accountClass === value}
                  onChange={() => {
                    setAccountClass(value);
                    if (value === "standard") setExperienceView("");
                  }}
                />
                {value}
              </label>
            ))}
          </div>
          <label className="mt-3 block text-xs uppercase tracking-wide text-slate-400">
            Initial navigation selection
            <select
              value={experienceView}
              onChange={(e) =>
                setExperienceView(e.target.value as DemoExperience | "")
              }
              disabled={accountClass !== "demo"}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm normal-case tracking-normal text-slate-200 disabled:opacity-50"
            >
              <option value="">No navigation selection</option>
              <option value="business">Business navigation</option>
              <option value="power">Power navigation</option>
            </select>
          </label>
          <p className="mt-2 text-xs text-slate-400">
            Returning the account to Standard clears its navigation selection.
          </p>
        </fieldset>

        <fieldset className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-slate-400">
          <legend className="px-1 text-slate-300">Admin console visibility</legend>
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={adminReadonly}
              onChange={(e) => setAdminReadonly(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              View-only admin access (beta viewer)
              <span className="block text-xs text-slate-400">
                Grants read access to every admin surface, including audit logs and
                users. The platform refuses all changes from this account, reads are
                audited, and the admin-MFA policy applies to it. No role change.
              </span>
            </span>
          </label>
        </fieldset>

        <fieldset className="mt-4 text-sm text-slate-400">
          <legend>Status</legend>
          <div className="mt-1 flex gap-4">
            {(["active", "suspended"] as const).map((s) => (
              <label key={s} className="flex items-center gap-1.5 capitalize">
                <input
                  type="radio"
                  name="status"
                  checked={status === s}
                  onChange={() => setStatus(s)}
                  disabled={isSelf && s === "suspended"}
                />
                {s}
              </label>
            ))}
          </div>
          {isSelf && <p className="mt-1 text-xs text-slate-400">You cannot suspend your own account.</p>}
        </fieldset>

        <label className="mt-4 block text-sm text-slate-400">
          Monthly budget override (USD){" "}
          <span className="text-slate-400">— stored now, enforced with S5 cost caps</span>
          <input
            type="number"
            min="0"
            step="10"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
            placeholder="none"
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
          />
        </label>

        {/* S15-03: offboarding is the irreversible counterpart to suspension —
            it strips team and project access. Owned projects are reported, not
            deleted, so nothing is destroyed on someone's last day. */}
        {!isSelf && (
          <div className="mt-5 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
            <p className="text-xs text-amber-200/90">
              <strong>Offboard</strong> suspends the account and removes every team and
              project share. Projects they own are listed for you to transfer — nothing is
              deleted. Suspension alone is reversible and keeps their access intact.
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                if (
                  !window.confirm(
                    `Offboard ${target.email}? This suspends the account and removes all team and project access.`
                  )
                )
                  return;
                setBusy(true);
                try {
                  const result = await api<{
                    teams_removed: number;
                    project_shares_removed: number;
                    owned_projects_needing_transfer: { id: string; name: string }[];
                  }>(`/v1/admin/users/${target.id}/offboard`, { method: "POST" });
                  const owned = result.owned_projects_needing_transfer;
                  onSaved(
                    `Offboarded ${target.email}: ${result.teams_removed} team(s) and ` +
                      `${result.project_shares_removed} project share(s) removed.` +
                      (owned.length
                        ? ` ${owned.length} owned project(s) still need transfer: ${owned
                            .map((p) => p.name)
                            .join(", ")}`
                        : "")
                  );
                } catch (e) {
                  onError((e as Error).message);
                } finally {
                  setBusy(false);
                }
              }}
              className="mt-2 rounded-lg border border-amber-500/50 px-3 py-1.5 text-xs text-amber-200 hover:bg-amber-500/10 disabled:opacity-40"
            >
              Offboard user
            </button>
          </div>
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
            disabled={busy}
            className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}
