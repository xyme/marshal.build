"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import type { Project } from "@/lib/types";
import HelpLink from "@/components/shell/HelpLink";

interface TeamOption {
  id: string;
  name: string;
  my_role: string | null;
}

/** Project → team workspace assignment (S15-02).
 *
 *  Owner-only: filing a project into a team changes WHO CAN SEE it, so it is an
 *  ownership decision rather than an editing one. Viewers/editors see the
 *  current team as read-only text.
 */
export default function TeamCard({
  project,
  isOwner,
  onChange,
}: {
  project: Project;
  isOwner: boolean;
  onChange: () => void;
}) {
  const [teams, setTeams] = useState<TeamOption[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!isOwner) return;
    api<TeamOption[]>("/v1/teams")
      .then(setTeams)
      .catch(() => setTeams([]));
  }, [isOwner]);

  async function assign(teamId: string) {
    setBusy(true);
    try {
      await api(`/v1/projects/${project.id}/team`, {
        method: "PUT",
        body: JSON.stringify({ team_id: teamId || null }),
      });
      toast.success(
        teamId
          ? "Project moved into the team — its members can now see it."
          : "Project is personal again — only you and people you share it with can see it."
      );
      onChange();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // Nothing useful to show a non-owner on a personal project
  if (!isOwner && !project.team_name) return null;

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
        <div>
          <span className="font-medium">Team workspace</span>
          <HelpLink doc="teams" label="What this changes" />
          <span className="ml-3 text-slate-400">
            {project.team_name
              ? `Shared with ${project.team_name}`
              : "Personal project — visible only to you and people you share it with"}
          </span>
        </div>
        {isOwner && (
          <label className="flex items-center gap-2">
            <span className="sr-only">Team workspace</span>
            <select
              value={project.team_id ?? ""}
              disabled={busy}
              onChange={(e) => assign(e.target.value)}
              className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm outline-none focus:border-indigo-500 disabled:opacity-40"
            >
              <option value="">Personal (no team)</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {isOwner && teams.length === 0 && (
        <p className="mt-2 text-xs text-slate-400">
          You are not in any team yet. An admin creates teams under Admin → Teams.
        </p>
      )}
    </section>
  );
}
