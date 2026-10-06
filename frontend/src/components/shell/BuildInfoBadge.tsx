"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export interface BuildInfo {
  git_sha: string | null;
  build_time: string | null;
  environment: string;
  migration_head_code: string | null;
  migration_head_db: string | null;
  migrations_in_sync: boolean;
}

/**
 * Version & release surface (S16-06): exactly what is running, next to the
 * Alpha badge on the profile page and in the admin footer. A code/db
 * migration mismatch is highlighted — it is the first thing to check in any
 * incident (docs/runbook.md#task-health).
 */
export default function BuildInfoBadge({ detailed = false }: { detailed?: boolean }) {
  const [info, setInfo] = useState<BuildInfo | null>(null);

  useEffect(() => {
    api<BuildInfo>("/v1/meta/build-info").then(setInfo).catch(() => setInfo(null));
  }, []);

  if (!info) return null;
  const sha = info.git_sha ? info.git_sha.slice(0, 12) : "unknown";

  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-400">
      <span>
        build <code className="rounded bg-slate-950 px-1.5 py-0.5 text-slate-300">{sha}</code>
      </span>
      {info.build_time && <span>({new Date(info.build_time).toLocaleString()})</span>}
      {detailed && (
        <>
          <span>
            migrations{" "}
            <code className="rounded bg-slate-950 px-1.5 py-0.5 text-slate-300">
              {info.migration_head_code?.slice(0, 12) ?? "?"}
            </code>
          </span>
          {!info.migrations_in_sync && info.migration_head_db && (
            <span className="text-amber-300" title={`database is at ${info.migration_head_db}`}>
              ⚠ db at {info.migration_head_db.slice(0, 12)}
            </span>
          )}
          <span>env {info.environment}</span>
        </>
      )}
      {/* The release-notes link was removed with the user-facing page
          (4 Sep 2026): the digest is internal-only now. */}
    </span>
  );
}
