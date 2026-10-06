"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";
import HelpLink from "@/components/shell/HelpLink";

interface TeamSummary {
  id: string;
  name: string;
  description: string | null;
  status: string;
  budget_usd: number | null;
  member_count: number;
  project_count: number;
}

interface TeamMemberRow {
  user_id: string;
  email: string;
  name: string | null;
  platform_role: string;
  status: string;
  team_role: "lead" | "member";
}

interface DirectoryUser {
  id: string;
  email: string;
  name: string | null;
  role: string;
  status: string;
}

/** Admin > Teams (S15-02): team workspaces within the org.
 *  Teams are NOT tenants — org-wide surfaces (marketplace, templates) stay
 *  shared; a team scopes which projects its members can see. */
export default function AdminTeamsPage() {
  const readonly = useAdminReadonly();
  const [teams, setTeams] = useState<TeamSummary[] | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [members, setMembers] = useState<TeamMemberRow[] | null>(null);
  const [directory, setDirectory] = useState<DirectoryUser[]>([]);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [busy, setBusy] = useState(false);

  const loadTeams = useCallback(async () => {
    const data = await api<TeamSummary[]>(
      `/v1/admin/teams${showArchived ? "?include_archived=true" : ""}`
    );
    setTeams(data);
  }, [showArchived]);

  useEffect(() => {
    loadTeams().catch((e) => toast.error((e as Error).message));
  }, [loadTeams]);

  useEffect(() => {
    api<{ items: DirectoryUser[] }>("/v1/admin/users?page_size=200")
      .then((d) => setDirectory(d.items ?? []))
      .catch(() => {});
  }, []);

  const openTeam = async (id: string) => {
    setSelected(id);
    setMembers(null);
    try {
      const data = await api<{ members: TeamMemberRow[] }>(`/v1/teams/${id}/members`);
      setMembers(data.members);
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  async function createTeam() {
    if (!newName.trim()) return;
    setBusy(true);
    try {
      await api("/v1/admin/teams", {
        method: "POST",
        body: JSON.stringify({ name: newName.trim(), description: newDesc.trim() || null }),
      });
      toast.success(`Team "${newName.trim()}" created.`);
      setNewName("");
      setNewDesc("");
      await loadTeams();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function saveMembers(rows: TeamMemberRow[]) {
    if (!selected) return;
    setBusy(true);
    try {
      const data = await api<{ members: TeamMemberRow[] }>(
        `/v1/admin/teams/${selected}/members`,
        {
          method: "PUT",
          body: JSON.stringify({
            members: rows.map((r) => ({ user_id: r.user_id, role: r.team_role })),
          }),
        }
      );
      setMembers(data.members);
      await loadTeams();
      toast.success("Membership saved.");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function setStatus(team: TeamSummary, status: "active" | "archived") {
    setBusy(true);
    try {
      await api(`/v1/admin/teams/${team.id}`, {
        method: "PUT",
        body: JSON.stringify({ status }),
      });
      await loadTeams();
      toast.success(status === "archived" ? "Team archived." : "Team restored.");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const addMember = (userId: string) => {
    if (!members) return;
    if (members.some((m) => m.user_id === userId)) return;
    const user = directory.find((u) => u.id === userId);
    if (!user) return;
    saveMembers([
      ...members,
      {
        user_id: user.id,
        email: user.email,
        name: user.name,
        platform_role: user.role,
        status: user.status,
        team_role: "member",
      },
    ]);
  };

  if (!teams) return <p className="text-slate-400">Loading…</p>;

  const current = teams.find((t) => t.id === selected) ?? null;
  const candidates = directory.filter(
    (u) => !(members ?? []).some((m) => m.user_id === u.id)
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Teams</h1>
        <HelpLink doc="teams" label="How teams and sharing work" />
        <p className="mt-1 text-sm text-slate-400">
          A team is a shared workspace: members see the projects filed to it. Leads can
          edit those projects, members can read them. Templates and the marketplace stay
          shared across the whole organisation.
        </p>
      </div>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Create a team
        </h2>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <label className="text-sm text-slate-400">
            Name
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              maxLength={80}
              placeholder="Retail Banking"
              className="mt-1 block w-56 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
            />
          </label>
          <label className="flex-1 text-sm text-slate-400">
            Description (optional)
            <input
              value={newDesc}
              onChange={(e) => setNewDesc(e.target.value)}
              maxLength={500}
              placeholder="What this team builds"
              className="mt-1 block w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
            />
          </label>
          <button
            onClick={createTeam}
            disabled={busy || readonly || !newName.trim()}
            className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            Create team
          </button>
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
            Teams ({teams.length})
          </h2>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(e) => setShowArchived(e.target.checked)}
            />
            Show archived
          </label>
        </div>
        {teams.length === 0 ? (
          <p className="mt-3 text-sm text-slate-400">
            No teams yet. Create one above, then file projects into it from the project
            page.
          </p>
        ) : (
          <table className="mt-3 w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="pb-2">Team</th>
                <th className="pb-2">Members</th>
                <th className="pb-2">Projects</th>
                <th className="pb-2">Monthly budget</th>
                <th className="pb-2">Status</th>
                <th className="pb-2" />
              </tr>
            </thead>
            <tbody>
              {teams.map((team) => (
                <tr key={team.id} className="border-t border-slate-800/70">
                  <td className="py-2">
                    <div className="font-medium text-slate-200">{team.name}</div>
                    {team.description && (
                      <div className="text-xs text-slate-400">{team.description}</div>
                    )}
                  </td>
                  <td className="py-2 text-slate-400">{team.member_count}</td>
                  <td className="py-2 text-slate-400">{team.project_count}</td>
                  <td className="py-2">
                    <TeamBudgetCell team={team} readonly={readonly || busy} onSaved={loadTeams} />
                  </td>
                  <td className="py-2">
                    <span
                      className={`rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                        team.status === "active"
                          ? "bg-emerald-500/15 text-emerald-300"
                          : "bg-slate-700/60 text-slate-300"
                      }`}
                    >
                      {team.status}
                    </span>
                  </td>
                  <td className="py-2 text-right">
                    <button
                      onClick={() => openTeam(team.id)}
                      className="rounded-lg border border-slate-700 px-3 py-1 text-xs hover:border-slate-500"
                    >
                      Manage
                    </button>
                    <button
                      onClick={() =>
                        setStatus(team, team.status === "active" ? "archived" : "active")
                      }
                      disabled={busy || readonly}
                      className="ml-2 rounded-lg px-2 py-1 text-xs text-slate-400 hover:text-slate-200 disabled:opacity-40"
                    >
                      {team.status === "active" ? "Archive" : "Restore"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {current && (
        <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
              {current.name} — membership
            </h2>
            <button
              onClick={() => {
                setSelected(null);
                setMembers(null);
              }}
              className="text-xs text-slate-400 hover:text-slate-200"
            >
              Close
            </button>
          </div>

          {members === null ? (
            <p className="mt-3 text-sm text-slate-400">Loading members…</p>
          ) : (
            <>
              {members.length === 0 ? (
                <p className="mt-3 text-sm text-slate-400">
                  No members yet — add someone below. An empty team&apos;s projects are
                  visible only to their owners.
                </p>
              ) : (
                <ul className="mt-3 divide-y divide-slate-800/70">
                  {members.map((m) => (
                    <li key={m.user_id} className="flex items-center justify-between py-2">
                      <span className="text-sm">
                        <span className="text-slate-200">{m.name ?? m.email}</span>
                        <span className="ml-2 text-xs text-slate-400">{m.email}</span>
                        {m.status !== "active" && (
                          <span className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] uppercase text-amber-300">
                            {m.status}
                          </span>
                        )}
                      </span>
                      <span className="flex items-center gap-2">
                        <select
                          value={m.team_role}
                          aria-label={`Team role for ${m.email}`}
                          disabled={busy || readonly}
                          onChange={(e) =>
                            saveMembers(
                              members.map((row) =>
                                row.user_id === m.user_id
                                  ? { ...row, team_role: e.target.value as "lead" | "member" }
                                  : row
                              )
                            )
                          }
                          className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-xs disabled:opacity-50"
                        >
                          <option value="member">member (read)</option>
                          <option value="lead">lead (edit)</option>
                        </select>
                        <button
                          onClick={() =>
                            saveMembers(members.filter((row) => row.user_id !== m.user_id))
                          }
                          disabled={busy || readonly}
                          className="text-xs text-slate-400 hover:text-red-300 disabled:opacity-40"
                        >
                          Remove
                        </button>
                      </span>
                    </li>
                  ))}
                </ul>
              )}

              <label className="mt-4 block text-sm text-slate-400">
                Add member
                <select
                  defaultValue=""
                  disabled={busy || readonly || candidates.length === 0}
                  onChange={(e) => {
                    if (e.target.value) addMember(e.target.value);
                    e.currentTarget.value = "";
                  }}
                  className="mt-1 block w-full max-w-md rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
                >
                  <option value="">
                    {candidates.length ? "Select a user…" : "Everyone is already a member"}
                  </option>
                  {candidates.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.email} ({u.role})
                    </option>
                  ))}
                </select>
              </label>
            </>
          )}
        </section>
      )}
    </div>
  );
}

/** Inline per-team monthly budget editor (5 Sep 2026).
 *  Visibility only — shown against spend on the Cost Dashboard; never a
 *  model-seam cap (user/project/platform caps stay the enforcement tools). */
function TeamBudgetCell({
  team,
  readonly,
  onSaved,
}: {
  team: TeamSummary;
  readonly: boolean;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(
    team.budget_usd != null ? String(team.budget_usd) : ""
  );
  const [saving, setSaving] = useState(false);

  async function save() {
    const trimmed = value.trim();
    const payload =
      trimmed === ""
        ? { clear_budget: true }
        : { budget_usd: Number(trimmed) };
    if (trimmed !== "" && (!Number.isFinite(Number(trimmed)) || Number(trimmed) < 0)) {
      toast.error("Budget must be a non-negative number.");
      return;
    }
    setSaving(true);
    try {
      await api(`/v1/admin/teams/${team.id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      toast.success(
        trimmed === ""
          ? `Budget cleared for "${team.name}".`
          : `Budget set: $${Number(trimmed)} / month for "${team.name}".`
      );
      setEditing(false);
      onSaved();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  if (!editing) {
    return (
      <span className="flex items-center gap-1.5 text-slate-300">
        {team.budget_usd != null ? `$${team.budget_usd}` : "—"}
        <button
          onClick={() => {
            setValue(team.budget_usd != null ? String(team.budget_usd) : "");
            setEditing(true);
          }}
          disabled={readonly}
          aria-label={`Edit budget for ${team.name}`}
          className="rounded px-1 text-xs text-slate-400 hover:bg-slate-800 hover:text-white disabled:opacity-40"
        >
          ✏️
        </button>
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1.5">
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            save();
          }
          if (e.key === "Escape") setEditing(false);
        }}
        type="number"
        min={0}
        max={100000}
        placeholder="empty = clear"
        aria-label={`Monthly budget for ${team.name}`}
        className="w-24 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-xs"
      />
      <button
        onClick={save}
        disabled={saving}
        className="rounded border border-slate-700 px-1.5 py-1 text-xs hover:border-indigo-400 disabled:opacity-40"
      >
        Save
      </button>
      <button
        onClick={() => setEditing(false)}
        className="px-1 text-xs text-slate-400 hover:text-slate-200"
      >
        ✕
      </button>
    </span>
  );
}
