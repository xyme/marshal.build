"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import HelpLink from "@/components/shell/HelpLink";
import { api } from "@/lib/api";
import { fetchSSE } from "@/lib/sse";
import type { BuildArtifactInfo, CodegenBuild, ConformanceReport, ProjectRole } from "@/lib/types";

const Monaco = dynamic(() => import("@/lib/monaco-setup").then(() => import("@monaco-editor/react")), {
  ssr: false,
  loading: () => <p className="p-4 text-sm text-slate-400">Loading viewer…</p>,
});

const IN_FLIGHT = new Set(["queued", "dispatched", "generating", "validating"]);
const STATUS_STYLE: Record<string, string> = {
  ready: "bg-emerald-500/15 text-emerald-400 border-emerald-500/40",
  failed: "bg-red-500/15 text-red-400 border-red-500/40",
  cancelled: "bg-slate-700/40 text-slate-400 border-slate-600",
};

interface LogLine {
  id: number;
  at: string;
  text: string;
  kind: "phase" | "file" | "error" | "info";
}

const LANG_MAP: Record<string, string> = {
  python: "python",
  json: "json",
  markdown: "markdown",
};

/** Build tab (codegen-handoff spec R4): start, live console, validation
 * findings, artifact browser, history, deploy handoff. */
export default function BuildTab({
  projectId,
  myRole,
  onDeployBuild,
}: {
  projectId: string;
  myRole: ProjectRole;
  onDeployBuild: (buildId: string) => void;
}) {
  // Role gate is project-role-based (R4.4); persona does not restrict builds
  const canBuild = myRole === "owner" || myRole === "editor";
  const [builds, setBuilds] = useState<CodegenBuild[] | null>(null);

  const [profile, setProfile] = useState<"inline-cfn" | "cdk-app">("inline-cfn");
  const [selected, setSelected] = useState<CodegenBuild | null>(null);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const logIdRef = useRef(0);
  const streamingRef = useRef(false);
  const logEndRef = useRef<HTMLDivElement>(null);

  const pushLog = useCallback((kind: LogLine["kind"], text: string) => {
    setLogs((prev) => [
      ...prev.slice(-400),
      { id: logIdRef.current++, at: new Date().toISOString(), text, kind },
    ]);
  }, []);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  const load = useCallback(async () => {
    try {
      const r = await api<{ items: CodegenBuild[] }>(`/v1/projects/${projectId}/builds`);
      setBuilds(r.items);
      setSelected((prev) => {
        if (prev) {
          const updated = r.items.find((b) => b.id === prev.id);
          if (updated) return updated;
        }
        return r.items[0] ?? null;
      });
      return r.items;
    } catch (e) {
      setError((e as Error).message);
      return [];
    }
  }, [projectId]);

  useEffect(() => {
    load();
  }, [load]);

  const streamBuild = useCallback(
    async (buildId: string) => {
      if (streamingRef.current) return;
      streamingRef.current = true;
      try {
        await fetchSSE(
          `/api/backend/v1/builds/${buildId}/events`,
          { method: "GET" },
          {
            onEvent: (event, data) => {
              const payload = data as Record<string, unknown> | null;
              if (event === "phase" && payload) {
                pushLog("phase", `▶ ${payload.phase}${payload.detail ? ` — ${payload.detail}` : ""}`);
                load();
              } else if (event === "plan" && payload) {
                pushLog("info", `Plan: ${payload.app_name} — ${(payload.files as string[])?.join(", ")}`);
              } else if (event === "file" && payload) {
                pushLog("file", `✓ ${payload.path} (${payload.index}/${payload.total})`);
              } else if (event === "done") {
                pushLog("phase", `■ Build ${(payload?.status as string) ?? "finished"}`);
                load();
              }
            },
          }
        );
      } catch (e) {
        pushLog("error", `Stream error: ${(e as Error).message}`);
      } finally {
        streamingRef.current = false;
      }
    },
    [pushLog, load]
  );

  // Attach to in-flight builds (incl. after page refresh)
  useEffect(() => {
    if (selected && IN_FLIGHT.has(selected.status)) {
      streamBuild(selected.id);
      const interval = setInterval(load, 6000);
      return () => clearInterval(interval);
    }
  }, [selected?.id, selected?.status, selected, streamBuild, load]);

  async function startBuild() {
    setBusy(true);
    setError(null);
    setLogs([]);
    try {
      const build = await api<CodegenBuild>(`/v1/projects/${projectId}/builds`, {
        method: "POST",
        body: JSON.stringify({ artifact_profile: profile }),
      });
      pushLog("info", "Build queued");
      await load();
      setSelected(build);
      streamBuild(build.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function cancelBuild(build: CodegenBuild) {
    try {
      await api(`/v1/builds/${build.id}/cancel`, { method: "POST" });
      toast.info("Cancel requested — the build stops at the next file boundary.");
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  const hasInFlight = builds?.some((b) => IN_FLIGHT.has(b.status)) ?? false;

  return (
    <div className="space-y-4">
      {/* start card */}
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-4">
        <div>
          <h2 className="flex items-center gap-3 font-medium">
            Generate agent code
            <HelpLink doc="quickstart" label="How builds work" />
          </h2>
          <p className="mt-0.5 text-sm text-slate-400">
            Turns the latest saved spec into a small deployable serverless app
            (CloudFormation + Lambda). Costs model tokens against your monthly cap; the
            template&apos;s guardrails apply. Rebuilds may differ — hashes make that visible.
          </p>
        </div>
        {canBuild ? (
          <div className="flex items-center gap-3">
            <select
              value={profile}
              onChange={(e) => setProfile(e.target.value as "inline-cfn" | "cdk-app")}
              className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-xs"
              aria-label="Artifact profile"
              title="What shape of artifact the build produces"
            >
              <option value="inline-cfn">Inline CloudFormation (single template)</option>
              <option value="cdk-app">
                cdk-app (experimental — needs the workspace-runner provider)
              </option>
            </select>
            <button
              onClick={startBuild}
              disabled={busy || hasInFlight}
              className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
              title={hasInFlight ? "A build is already running" : undefined}
            >
              🛠 {builds && builds.length > 0 ? "Rebuild" : "Build app"}
            </button>
          </div>
        ) : (
          <span className="text-sm text-slate-400">Building requires editor access.</span>
        )}
      </section>

      {error && (
        <p className="flex items-center justify-between gap-3 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          <span>{error}</span>
          <HelpLink doc="troubleshooting" label="Troubleshooting" />
        </p>
      )}

      {builds === null ? (
        <p className="text-slate-400">Loading…</p>
      ) : builds.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 p-10 text-center text-sm text-slate-400">
          No builds yet. The generated artifact set (template + source + README) appears
          here with a validation report before anything can be deployed.
        </div>
      ) : (
        <>
          {/* history strip */}
          <div className="flex gap-2 overflow-x-auto pb-1">
            {builds.map((b) => (
              <button
                key={b.id}
                onClick={() => setSelected(b)}
                className={`shrink-0 rounded-lg border px-3 py-2 text-left text-xs ${
                  selected?.id === b.id
                    ? "border-indigo-500 bg-indigo-500/10"
                    : "border-slate-800 bg-slate-950/60 hover:border-slate-600"
                }`}
              >
                <span
                  className={`mr-2 rounded-full border px-1.5 py-0.5 text-[10px] uppercase ${
                    STATUS_STYLE[b.status] ?? "border-amber-500/40 bg-amber-500/15 text-amber-400"
                  }`}
                >
                  {b.status}
                </span>
                {b.manifest?.app_name ?? "build"}
                <span className="ml-2 font-mono text-slate-400">
                  {b.content_hash ? b.content_hash.slice(0, 8) : "…"}
                </span>
                <div className="mt-0.5 text-slate-400">
                  {b.created_by_name ?? ""} · {new Date(b.created_at).toLocaleString()}
                </div>
              </button>
            ))}
          </div>

          {selected && (
            <SelectedBuild
              build={selected}
              logs={logs}
              logEndRef={logEndRef}
              canOperate={canBuild}
              onCancel={() => cancelBuild(selected)}
              onDeploy={() => onDeployBuild(selected.id)}
            />
          )}
        </>
      )}
    </div>
  );
}

function SelectedBuild({
  build,
  logs,
  logEndRef,
  canOperate,
  onCancel,
  onDeploy,
}: {
  build: CodegenBuild;
  logs: LogLine[];
  logEndRef: React.RefObject<HTMLDivElement | null>;
  canOperate: boolean;
  onCancel: () => void;
  onDeploy: () => void;
}) {
  const inFlight = IN_FLIGHT.has(build.status);
  const findings =
    build.error?.findings ?? build.manifest?.validation?.findings ?? [];

  return (
    <div className="space-y-4">
      <section className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-4">
        <div className="flex flex-wrap items-center gap-3">
          <span
            className={`rounded-full border px-2.5 py-0.5 text-xs uppercase tracking-wide ${
              STATUS_STYLE[build.status] ?? "border-amber-500/40 bg-amber-500/15 text-amber-400"
            }`}
          >
            {inFlight && <span className="mr-1.5 inline-block h-1.5 w-1.5 animate-ping rounded-full bg-current" />}
            {build.status}
          </span>
          {build.phase_detail && <span className="text-sm text-slate-400">{build.phase_detail}</span>}
          {build.content_hash && (
            <span className="font-mono text-xs text-slate-400" title="Output content hash — identical hashes mean identical output">
              #{build.content_hash.slice(0, 12)}
            </span>
          )}
          {build.provider !== "internal" && (
            <span
              className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-400"
              title={
                build.external_job_id
                  ? `Runner job ${build.external_job_id}${build.retried ? " (retried once)" : ""}`
                  : "External workspace runner"
              }
            >
              runner{build.retried ? " · r2" : ""}
            </span>
          )}
          {build.artifact_profile === "cdk-app" && (
            <span className="rounded bg-indigo-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300"
              title="Full CDK application — the bundle is a working repository">
              cdk app
            </span>
          )}
          {build.manifest?.endpoint_auth && (
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                build.manifest.endpoint_auth.mode === "key_required"
                  ? "bg-emerald-500/15 text-emerald-300"
                  : "bg-amber-500/15 text-amber-300"
              }`}
              title={
                build.manifest.endpoint_auth.source === "default"
                  ? "API-key protection applied by the platform safe default"
                  : build.manifest.endpoint_auth.source === "explicit_public"
                    ? "Requirements explicitly opt out of endpoint authentication"
                    : "Endpoint authentication intent declared in requirements"
              }
            >
              {build.manifest.endpoint_auth.mode === "key_required"
                ? `🔑 keyed · ${build.manifest.endpoint_auth.source === "default" ? "default" : "spec"}`
                : "🌐 public · explicit"}
            </span>
          )}
          {build.manifest?.inline_retry?.attempted && (
            <span
              className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-300"
              title={`Oversized inline code was regenerated once with compaction (${build.manifest.inline_retry.files.join(", ")}) — ${
                build.manifest.inline_retry.resolved ? "retry fit under the ceiling" : "still over the ceiling after retry"
              }`}
            >
              compacted{build.manifest.inline_retry.resolved ? "" : " · still over"}
            </span>
          )}
          <span className="ml-auto flex gap-2">
            {inFlight && canOperate && (
              <button
                onClick={onCancel}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs hover:border-slate-500"
              >
                Cancel
              </button>
            )}
            {build.status === "ready" && (
              <>
                <a
                  href={`/api/backend/v1/builds/${build.id}/bundle`}
                  download
                  className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs hover:border-slate-500"
                  title="Spec snapshot + generated code + manifest"
                >
                  📦 Handoff bundle
                </a>
                {canOperate && (
                  <button
                    onClick={onDeploy}
                    className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500"
                  >
                    🚀 Deploy this build
                  </button>
                )}
              </>
            )}
          </span>
        </div>
        {build.manifest?.architecture_notes && (
          <p className="mt-2 text-sm text-slate-400">{build.manifest.architecture_notes}</p>
        )}
        {build.error && build.error.code !== "validation_failed" && (
          <p className="mt-2 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
            {build.error.code}: {build.error.message}
          </p>
        )}
      </section>

      {findings.length > 0 && (
        <section className="rounded-xl border border-red-900/50 bg-red-950/20 px-5 py-4">
          <h3 className="text-sm font-medium text-red-300">
            Validation findings ({findings.length}) — the build cannot deploy until these pass
          </h3>
          <ul className="mt-2 space-y-1.5 text-sm">
            {findings.map((f, i) => (
              <li key={i} className="flex gap-2">
                <span className="shrink-0 rounded bg-red-500/15 px-1.5 py-0.5 font-mono text-[10px] text-red-300">
                  {f.check}
                </span>
                <span className="font-mono text-xs text-slate-400">{f.path}</span>
                <span className="text-slate-300">{f.message}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-slate-400">
            Fix by refining the spec (or template guardrails) and rebuilding — generated
            artifacts are never hand-edited on the platform.
          </p>
        </section>
      )}

      {build.manifest?.conformance && <ConformanceSection report={build.manifest.conformance} />}

      {inFlight || logs.length > 0 ? (
        <section className="flex min-h-[180px] flex-col rounded-xl border border-slate-800 bg-black/50">
          <div className="border-b border-slate-800 px-3 py-2 text-xs uppercase tracking-wide text-slate-400">
            Build console
          </div>
          <div className="max-h-64 flex-1 space-y-1 overflow-y-auto p-3 font-mono text-[11px] leading-relaxed">
            {logs.length === 0 ? (
              <p className="text-slate-400">Waiting for events…</p>
            ) : (
              logs.map((line) => (
                <div
                  key={line.id}
                  className={
                    line.kind === "phase"
                      ? "text-indigo-300"
                      : line.kind === "error"
                        ? "text-red-300"
                        : line.kind === "file"
                          ? "text-emerald-300"
                          : "text-slate-400"
                  }
                >
                  <span className="text-slate-400">{new Date(line.at).toLocaleTimeString()} </span>
                  {line.text}
                </div>
              ))
            )}
            <div ref={logEndRef} />
          </div>
        </section>
      ) : null}

      {(build.status === "ready" || findings.length > 0) && (
        <ArtifactBrowser buildId={build.id} />
      )}
    </div>
  );
}

const VERDICT_STYLE: Record<string, { badge: string; icon: string }> = {
  met: { badge: "bg-emerald-500/15 text-emerald-300", icon: "✓" },
  violated: { badge: "bg-amber-500/15 text-amber-300", icon: "⚠" },
  unverifiable: { badge: "bg-slate-700/60 text-slate-300", icon: "?" },
};

/** Spec-conformance report (B7/B23). Deterministic field/literal
 * violations are build gates; model-review verdicts remain advisory. */
function ConformanceSection({ report }: { report: ConformanceReport }) {
  if (report.status !== "ok") {
    return (
      <p className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-3 text-xs text-slate-400">
        Spec conformance: unavailable for this build{report.error ? ` (${report.error})` : ""}.
      </p>
    );
  }
  const verdicts = report.verdicts ?? [];
  const s = report.summary ?? { met: 0, violated: 0, unverifiable: 0 };
  const hasViolations = s.violated > 0;
  return (
    <section
      className={`rounded-xl border px-5 py-4 ${
        hasViolations ? "border-amber-900/60 bg-amber-950/15" : "border-slate-800 bg-slate-900/40"
      }`}
    >
      <details open={hasViolations}>
        <summary className="cursor-pointer select-none text-sm font-medium text-slate-200">
          Spec conformance{" "}
          <span className="ml-1 text-xs font-normal text-slate-400">
            <span className="text-emerald-300">{s.met} met</span>
            {" · "}
            <span className={hasViolations ? "text-amber-300" : ""}>{s.violated} violated</span>
            {" · "}
            {s.unverifiable} unverifiable
          </span>
        </summary>
        <ul className="mt-3 space-y-2 text-sm">
          {verdicts.map((v, i) => {
            const style = VERDICT_STYLE[v.verdict] ?? VERDICT_STYLE.unverifiable;
            return (
              <li key={i} className="flex gap-2">
                <span
                  className={`mt-0.5 h-fit shrink-0 rounded px-1.5 py-0.5 font-mono text-[10px] ${style.badge}`}
                  title={v.source === "deterministic" ? "Deterministic textual check" : "Model review (temperature 0)"}
                >
                  {style.icon} {v.verdict}
                </span>
                <span className="min-w-0">
                  <span className="text-slate-300">{v.criterion}</span>
                  {v.evidence && (
                    <span className="mt-0.5 block text-xs text-slate-400">{v.evidence}</span>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
        <p className="mt-3 text-xs text-slate-400">
          Deterministic field/literal violations block build readiness and appear in
          the red findings panel above. Model-review verdicts are advisory — they can
          be wrong; use their evidence to refine the spec and rebuild.
        </p>
      </details>
    </section>
  );
}

function ArtifactBrowser({ buildId }: { buildId: string }) {
  const [items, setItems] = useState<BuildArtifactInfo[]>([]);
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [content, setContent] = useState<string>("");
  const [language, setLanguage] = useState<string>("plaintext");
  const [isBinary, setIsBinary] = useState(false);

  useEffect(() => {
    api<{ items: BuildArtifactInfo[] }>(`/v1/builds/${buildId}/artifacts`)
      .then((r) => {
        setItems(r.items);
        const first = r.items.find((a) => a.path === "template.json") ?? r.items[0];
        if (first) setOpenPath(first.path);
      })
      .catch(() => {});
  }, [buildId]);

  useEffect(() => {
    if (!openPath) return;
    setIsBinary(false);
    api<{ content: string | null; language: string | null; binary?: boolean; too_large?: boolean }>(
      `/v1/builds/${buildId}/artifacts/${openPath}`
    )
      .then((r) => {
        if (r.binary || r.too_large) {
          setIsBinary(true);
          setContent("");
          return;
        }
        setContent(r.content ?? "");
        setLanguage(LANG_MAP[r.language ?? ""] ?? (openPath.endsWith(".ts") ? "typescript" : "plaintext"));
      })
      .catch(() => setContent("// failed to load"));
  }, [buildId, openPath]);

  return (
    <section className="flex h-[420px] overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
      <div className="w-64 shrink-0 space-y-0.5 overflow-y-auto border-r border-slate-800 p-2">
        <p className="px-2 py-1 text-xs uppercase tracking-wide text-slate-400">
          Artifacts ({items.length})
        </p>
        {items.map((a) => (
          <button
            key={a.path}
            onClick={() => setOpenPath(a.path)}
            className={`block w-full truncate rounded px-2 py-1.5 text-left font-mono text-xs ${
              openPath === a.path ? "bg-slate-800 text-white" : "text-slate-400 hover:bg-slate-800/60"
            }`}
            title={`${a.path} · ${a.size_bytes} bytes · ${a.content_hash.slice(0, 8)}`}
          >
            {a.path}
          </button>
        ))}
      </div>
      <div className="min-w-0 flex-1">
        {openPath && isBinary ? (
          <div className="p-6 text-sm text-slate-400">
            <p>Binary/large artifact — no inline preview.</p>
            <a
              href={`/api/backend/v1/builds/${buildId}/artifacts/${openPath}?download=true`}
              download
              className="mt-3 inline-block rounded-lg border border-slate-700 px-3 py-1.5 text-xs hover:border-slate-500"
            >
              ⬇ Download {openPath.split("/").pop()}
            </a>
          </div>
        ) : openPath ? (
          <Monaco
            language={language}
            theme="vs-dark"
            value={content}
            options={{
              readOnly: true,
              minimap: { enabled: false },
              wordWrap: "on",
              fontSize: 12,
            }}
          />
        ) : (
          <p className="p-6 text-sm text-slate-400">Select a file.</p>
        )}
      </div>
    </section>
  );
}
