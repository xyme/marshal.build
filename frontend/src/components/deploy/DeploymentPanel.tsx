"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import HelpLink from "@/components/shell/HelpLink";
import { useConfirm } from "@/components/ui/ConfirmDialog";
import { ModalShell } from "@/components/ui/primitives";
import { api, ApiError } from "@/lib/api";
import { RISK_STYLE } from "@/lib/governance-ui";
import { fetchSSE } from "@/lib/sse";
import type {
  CodegenBuild,
  Deployment,
  Project,
  ProjectRisk,
  TestbedCredentials,
} from "@/lib/types";

const PHASES = ["pre_flight", "leasing", "deploying", "active"] as const;
const PHASE_LABELS: Record<string, string> = {
  pending: "Queued",
  pre_flight: "Pre-flight checks",
  leasing: "Provisioning Enclave account",
  deploying: "Deploying stack",
  updating: "Updating stack in place",
  active: "Live",
  failed: "Failed",
  superseded: "Superseded",
  tearing_down: "Tearing down",
  torn_down: "Torn down",
  expired: "Expired",
  health_degraded: "Health degraded",
  health_recovered: "Health recovered",
  health_probe_failed: "Health probe failed",
  health_probe_ok: "Health probe OK",
};
const IN_FLIGHT = new Set(["pending", "pre_flight", "leasing", "deploying", "updating", "tearing_down"]);

interface LogLine {
  id: number;
  at: string;
  text: string;
  kind: "phase" | "cfn" | "error";
}

/** Deployment tab (S1-08..S1-10): status, timeline, live SSE console, actions.
 * S8: payload selector — deploy a ready codegen build instead of the sample app. */
export default function DeploymentPanel({
  project,
  onChange,
  preselectedBuildId,
}: {
  project: Project;
  onChange: () => void;
  preselectedBuildId?: string | null;
}) {
  const [deployment, setDeployment] = useState<Deployment | null>(project.deployment);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [risk, setRisk] = useState<ProjectRisk | null>(null);
  const [readyBuilds, setReadyBuilds] = useState<CodegenBuild[]>([]);
  const [payloadBuildId, setPayloadBuildId] = useState<string | null>(
    preselectedBuildId ?? null
  );
  const [showExtend, setShowExtend] = useState(false);
  const [extendHours, setExtendHours] = useState(24);
  const [history, setHistory] = useState<Deployment[]>([]);
  // B20 R1.1: deployment mode picker (create only; updates inherit)
  const [mode, setMode] = useState<"full_governance" | "testbed" | null>(null);
  // B20 R2.5: shown-once vended credentials (never persisted client-side)
  const [testbedCreds, setTestbedCreds] = useState<TestbedCredentials | null>(null);
  const [minting, setMinting] = useState(false);

  async function mintTestbedCreds() {
    setMinting(true);
    setError(null);
    try {
      const creds = await api<TestbedCredentials>(
        `/v1/projects/${project.id}/deployment/testbed-credentials`,
        { method: "POST" }
      );
      setTestbedCreds(creds);
    } catch (e) {
      if (e instanceof ApiError) {
        try {
          const detail = JSON.parse(e.message);
          setError(detail.detail ?? e.message);
        } catch {
          setError(e.message);
        }
      } else {
        setError((e as Error).message);
      }
    } finally {
      setMinting(false);
    }
  }

  const loadHistory = useCallback(() => {
    api<Deployment[]>(`/v1/projects/${project.id}/deployments`)
      .then(setHistory)
      .catch(() => {});
  }, [project.id]);
  useEffect(() => {
    loadHistory();
  }, [loadHistory, deployment?.status]);

  // B13: owner-only, audit-logged live key reveal (nothing stored client-side
  // beyond this component's state; the row exists only while mounted)
  const [revealedKey, setRevealedKey] = useState<{ api_key_id: string; value: string } | null>(null);
  const [revealing, setRevealing] = useState(false);
  const hasApiKey = (deployment?.resources ?? []).some(
    (r) => r.type === "AWS::ApiGateway::ApiKey"
  );
  // B19: the fixed logical id is the gate-enforced detection contract
  const hasConsole = (deployment?.resources ?? []).some(
    (r) => r.logical_id === "WebConsoleFunction"
  );
  async function revealKey() {
    setRevealing(true);
    try {
      const result = await api<{ api_key_id: string; value: string }>(
        `/v1/projects/${project.id}/deployment/api-key/reveal`,
        { method: "POST" }
      );
      setRevealedKey(result);
      toast.success("Key revealed — this action is audit-logged.");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setRevealing(false);
    }
  }

  async function extend() {
    try {
      const dep = await api<Deployment>(`/v1/projects/${project.id}/deployment/extend`, {
        method: "POST",
        body: JSON.stringify({ hours: extendHours }),
      });
      setDeployment(dep);
      setShowExtend(false);
      toast.success(`Extended — now expires ${new Date(dep.expires_at!).toLocaleString()}.`);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }
  const confirm = useConfirm();
  const logIdRef = useRef(0);
  const streamingRef = useRef(false);
  const logEndRef = useRef<HTMLDivElement>(null);

  const refreshRisk = useCallback(() => {
    api<ProjectRisk>(`/v1/projects/${project.id}/risk`).then(setRisk).catch(() => {});
  }, [project.id]);

  useEffect(() => setDeployment(project.deployment), [project.deployment]);

  useEffect(() => {
    refreshRisk();
  }, [refreshRisk]);
  useEffect(() => {
    // Ready builds populate the payload selector (S8 R5); latest preselected
    api<{ items: CodegenBuild[] }>(`/v1/projects/${project.id}/builds`)
      .then((r) => {
        const ready = r.items.filter((b) => b.status === "ready");
        setReadyBuilds(ready);
        setPayloadBuildId((prev) => prev ?? (ready[0]?.id ?? null));
      })
      .catch(() => {});
  }, [project.id]);
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  const pushLog = useCallback((kind: LogLine["kind"], at: string, text: string) => {
    setLogs((prev) => [...prev.slice(-400), { id: logIdRef.current++, at, text, kind }]);
  }, []);

  const refreshDeployment = useCallback(async () => {
    try {
      const dep = await api<Deployment>(`/v1/projects/${project.id}/deployment`);
      setDeployment(dep);
      return dep;
    } catch {
      return null;
    }
  }, [project.id]);

  const streamLogs = useCallback(async () => {
    if (streamingRef.current) return;
    streamingRef.current = true;
    try {
      await fetchSSE(
        `/api/backend/v1/projects/${project.id}/deployment/logs`,
        { method: "GET" },
        {
          onEvent: (event, data) => {
            const payload = data as Record<string, string> | null;
            if (event === "phase" && payload) {
              pushLog(
                "phase",
                payload.at ?? new Date().toISOString(),
                `▶ ${PHASE_LABELS[payload.phase] ?? payload.phase}${payload.detail ? ` — ${payload.detail}` : ""}`
              );
              refreshDeployment();
            } else if (event === "cfn" && payload) {
              pushLog(
                "cfn",
                payload.at,
                `${payload.resource_type}  ${payload.logical_id}  ${payload.status}${payload.reason ? `  (${payload.reason})` : ""}`
              );
            } else if (event === "done") {
              pushLog("phase", new Date().toISOString(), "■ Stream complete");
              refreshDeployment().then(() => onChange());
            }
          },
        }
      );
    } catch (e) {
      pushLog("error", new Date().toISOString(), `Stream error: ${(e as Error).message}`);
    } finally {
      streamingRef.current = false;
    }
  }, [project.id, pushLog, refreshDeployment, onChange]);

  // Auto-attach to in-flight deployments (also on page refresh — spec R3.5)
  useEffect(() => {
    if (deployment && IN_FLIGHT.has(deployment.status)) {
      streamLogs();
      const interval = setInterval(refreshDeployment, 5000);
      return () => clearInterval(interval);
    }
  }, [deployment?.status, deployment, streamLogs, refreshDeployment]);

  async function deploy() {
    setBusy(true);
    setError(null);
    setLogs([]);
    try {
      // B20 R1.1: mode rides along on CREATE only (updates inherit server-side)
      const updating = deployment?.status === "active";
      const requestedMode = updating
        ? undefined
        : (mode ?? risk?.default_mode ?? undefined);
      const dep = await api<Deployment>(`/v1/projects/${project.id}/deploy`, {
        method: "POST",
        body: JSON.stringify({
          ...(payloadBuildId ? { build_id: payloadBuildId } : {}),
          ...(requestedMode ? { mode: requestedMode } : {}),
        }),
      });
      setDeployment(dep);
      streamLogs();
    } catch (e) {
      // Risk gate 403s carry structured detail (S4-03 R3.3)
      if (e instanceof ApiError && e.status === 403) {
        try {
          const detail = JSON.parse(e.message);
          if (detail.code === "risk_pending") {
            toast.info("Deployment is pending risk review — an admin has been queued.");
          } else if (detail.code === "risk_rejected") {
            toast.error("Deployment rejected by risk review. See the reviewer notes.");
          }
          setError(detail.detail ?? e.message);
        } catch {
          setError(e.message);
        }
        refreshRisk();
      } else if (e instanceof ApiError && (e.status === 409 || e.status === 422)) {
        // Structured refusals: platform policies (S12 R1) or risk scoring race
        try {
          const detail = JSON.parse(e.message);
          if (detail.code === "risk_scoring") {
            setError("Risk scoring is in progress — try again in a few seconds.");
            refreshRisk();
          } else if (typeof detail.code === "string" && detail.code.startsWith("policy_")) {
            setError(detail.detail ?? "Blocked by platform deployment policy.");
          } else {
            setError(detail.detail ?? e.message);
          }
        } catch {
          setError(e.message);
          if (e.status === 409) refreshRisk();
        }
      } else {
        setError((e as Error).message);
      }
    } finally {
      setBusy(false);
    }
  }

  async function teardown() {
    const ok = await confirm({
      title: "Tear down deployment",
      body: `All ${deployment?.mode === "testbed" ? "Testbed" : "Enclave"} resources for this project will be deleted. The account lease is terminated and recycled.`,
      confirmLabel: "Tear down",
      destructive: true,
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      const dep = await api<Deployment>(`/v1/projects/${project.id}/deployment/teardown`, {
        method: "POST",
      });
      setDeployment(dep);
      streamLogs();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const status = deployment?.status;
  const isUpdate = status === "active"; // S11 R3: in-place update, no teardown
  const selectedBuild = readyBuilds.find((b) => b.id === payloadBuildId) ?? null;
  const selectedEndpointAuth =
    selectedBuild?.manifest?.endpoint_auth ??
    (risk?.endpoint_auth
      ? { mode: risk.endpoint_auth, source: risk.endpoint_auth_source ?? "default" }
      : null);
  // B20 R1: the SELECTED mode decides whether a blocking verdict blocks
  const testbedAvailable =
    !!risk?.gate_modes?.testbed && risk.gate_modes.testbed !== "disabled";
  const selectedMode: "full_governance" | "testbed" = isUpdate
    ? (deployment?.mode ?? "full_governance")
    : (mode ?? risk?.default_mode ?? "full_governance");
  const selectedGate =
    selectedMode === "testbed" ? (risk?.gate_modes?.testbed ?? "advisory") : "enforce";
  const gateVerdictBlocks =
    risk?.assessed === true &&
    risk.current === true &&
    (risk.decision === "pending" ||
      risk.decision === "rejected" ||
      risk.decision === "changes_requested");
  const gateBlocked = gateVerdictBlocks && selectedGate === "enforce";
  const canDeploy =
    (!deployment || status === "torn_down" || status === "failed" || status === "active") &&
    !gateBlocked;

  interface PreviewChange {
    action: string;
    logical_id: string;
    resource_type: string;
    replacement?: string | null;
  }
  const [preview, setPreview] = useState<
    { no_changes: boolean; changes: PreviewChange[] } | null
  >(null);
  const [previewing, setPreviewing] = useState(false);

  async function previewChanges() {
    setPreviewing(true);
    try {
      const result = await api<{ no_changes: boolean; changes: PreviewChange[] }>(
        `/v1/projects/${project.id}/deployment/preview`,
        {
          method: "POST",
          body: JSON.stringify(payloadBuildId ? { build_id: payloadBuildId } : {}),
        }
      );
      setPreview(result);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setPreviewing(false);
    }
  }
  const canTeardown = status === "active" || status === "failed";

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40">
      <header className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
        <div className="flex items-center gap-3">
          <h2 className="font-medium">Deployment</h2>
          <HelpLink doc="quickstart" label="How Enclaves work" />
          {status && <StatusPill status={status} />}
          {status === "active" && deployment?.health === "degraded" && (
            <span
              className="rounded-full border border-amber-500/40 bg-amber-500/15 px-2.5 py-0.5 text-xs uppercase tracking-wide text-amber-300"
              title={`Failing health probes since ${deployment.last_health_at ? new Date(deployment.last_health_at).toLocaleTimeString() : "recently"}`}
            >
              ⚠ degraded
            </span>
          )}
          {status === "active" && deployment?.expires_at && (
            <ExpiryChip
              expiresAt={deployment.expires_at}
              onExtend={() => setShowExtend(true)}
            />
          )}
        </div>
        <div className="flex items-center gap-2">
          {risk?.assessed && risk.level && (
            <span
              className={`rounded px-2 py-0.5 text-xs uppercase tracking-wide ${RISK_STYLE[risk.level] ?? "bg-slate-700/40 text-slate-400"}`}
              title={`Risk ${risk.score}/100${risk.current ? "" : " (from an earlier version — deploying re-scores)"}`}
            >
              risk: {risk.level}
              {risk.current && risk.decision === "pending" && " · pending review"}
            </span>
          )}
          {selectedEndpointAuth && (
            <span
              className={`rounded px-2 py-0.5 text-xs uppercase tracking-wide ${
                selectedEndpointAuth.mode === "key_required"
                  ? "bg-emerald-500/15 text-emerald-300"
                  : "bg-amber-500/15 text-amber-300"
              }`}
              title={
                selectedEndpointAuth.mode === "open"
                  ? "The selected build explicitly opts out of endpoint authentication"
                  : selectedEndpointAuth.source === "default"
                    ? "The selected build is API-key protected by the platform safe default"
                    : "The selected build explicitly requires API-key protection"
              }
            >
              {selectedEndpointAuth.mode === "key_required"
                ? `🔑 keyed${selectedEndpointAuth.source === "default" ? " · default" : " · spec"}`
                : "🌐 public · explicit"}
            </span>
          )}
          {canDeploy && (
            <>
              {!isUpdate && testbedAvailable && (
                <select
                  value={selectedMode}
                  onChange={(e) =>
                    setMode(e.target.value as "full_governance" | "testbed")
                  }
                  className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-xs"
                  aria-label="Deployment mode"
                  title={
                    selectedMode === "testbed"
                      ? "Testbed: you can mint limited AWS credentials for self-configuration (EULAs, quotas, extra services). Risk gate is " +
                        (risk?.gate_modes?.testbed ?? "advisory") +
                        ". Budget + TTL still enforce. Mode is fixed once deployed."
                      : "Full Governance: the platform holds sole custody of the Enclave; the risk gate enforces. Mode is fixed once deployed."
                  }
                >
                  <option value="full_governance">🛡 Full Governance</option>
                  <option value="testbed">🧪 Testbed</option>
                </select>
              )}
              {readyBuilds.length > 0 && (
                <select
                  value={payloadBuildId ?? ""}
                  onChange={(e) => setPayloadBuildId(e.target.value || null)}
                  className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-xs"
                  aria-label="Deployment payload"
                  title="What gets deployed to the Enclave"
                >
                  {readyBuilds.map((b, i) => (
                    <option key={b.id} value={b.id}>
                      🛠 {b.manifest?.app_name ?? "build"} #{b.content_hash?.slice(0, 8)}
                      {i === 0 ? " (latest)" : ""}
                    </option>
                  ))}
                  <option value="">Sample app (hello world)</option>
                </select>
              )}
              {isUpdate && (
                <button
                  onClick={previewChanges}
                  disabled={busy || previewing}
                  title="Evaluate a CloudFormation change set for this payload — the running stack is not modified"
                  className="rounded-lg border border-slate-700 px-4 py-2 text-sm hover:border-indigo-400 disabled:opacity-40"
                >
                  {previewing ? "Evaluating…" : "🔍 Preview changes"}
                </button>
              )}
              <button
                onClick={deploy}
                disabled={busy}
                data-tour="deploy"
                title={
                  isUpdate
                    ? "Updates the running stack directly; a failed update rolls back to the current build"
                    : undefined
                }
                className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
              >
                {isUpdate
                  ? "⬆ Update in place"
                  : selectedMode === "testbed"
                    ? "🚀 Deploy to Testbed"
                    : "🚀 Deploy to Enclave"}
              </button>
            </>
          )}
          {gateBlocked && (
            <span className="rounded-lg border border-amber-500/50 bg-amber-500/10 px-4 py-2 text-sm text-amber-300">
              {risk?.decision === "rejected"
                ? "⛔ Rejected by review"
                : risk?.decision === "changes_requested"
                  ? "✏️ Changes requested"
                  : "⏸ Pending Review"}
            </span>
          )}
          {gateVerdictBlocks && !gateBlocked && selectedGate === "advisory" && (
            <span
              className="rounded-lg border border-slate-600 bg-slate-800/60 px-4 py-2 text-sm text-slate-300"
              title="Testbed gate stance is advisory — the verdict stays on the assessment and the bypass is audited, but the deploy is not blocked"
            >
              ⚠ Review verdict advisory in Testbed mode
            </span>
          )}
          {canTeardown && (
            <button
              onClick={teardown}
              disabled={busy}
              className="rounded-lg border border-red-500/50 px-4 py-2 text-sm text-red-300 hover:bg-red-500/10 disabled:opacity-40"
            >
              🗑 Tear Down
            </button>
          )}
        </div>
      </header>

      {error && (
        <p className="mx-5 mt-3 flex items-center justify-between gap-3 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          <span>{error}</span>
          <HelpLink doc="troubleshooting" label="Troubleshooting" />
        </p>
      )}

      {risk?.assessed && risk.current && risk.decision === "rejected" && risk.notes && (
        <p className="mx-5 mt-3 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          Reviewer notes: {risk.notes} — edit the spec to address these and it will be re-scored.
        </p>
      )}
      {risk?.assessed && risk.current && risk.decision === "pending" && (
        <p className="mx-5 mt-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
          This spec scored {risk.score}/100 ({risk.level}) and is waiting on reviewer approval
          before deployment (FSD §4.6.3). Reviewers see it in their queue.
        </p>
      )}
      {risk?.assessed && risk.current && risk.decision === "changes_requested" && (
        <p className="mx-5 mt-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
          Reviewers requested changes: {risk.notes} — revise the spec in the workspace; the
          resubmission is re-scored and returns to the review queue automatically.
        </p>
      )}

      {!deployment ? (
        <div className="px-5 py-10 text-center text-sm text-slate-400">
          Not deployed yet. Deploy the sample app to prove Enclave connectivity — a
          hello-world Lambda + API Gateway stack in an isolated account.
        </div>
      ) : (
        <div className="grid gap-5 p-5 lg:grid-cols-2">
          <div className="space-y-4">
            <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-sm">
              {deployment.build_id && (
                <Row
                  label="Payload"
                  value={
                    <span className="rounded bg-indigo-500/15 px-1.5 py-0.5 text-xs text-indigo-300">
                      🛠 generated build {deployment.build_id.slice(0, 8)}
                    </span>
                  }
                />
              )}
              <Row
                label={deployment.mode === "testbed" ? "Testbed account" : "Enclave account"}
                value={deployment.lease_account_id ?? "—"}
                mono
              />
              <Row label="Lease" value={deployment.lease_external_id ?? "—"} mono />
              {deployment.lease_state && (
                <Row
                  label="Enclave lease"
                  value={
                    <span className="text-xs text-slate-400">
                      {deployment.lease_state.budget_used_usd != null &&
                      deployment.lease_state.budget_cap_usd != null
                        ? `$${Number(deployment.lease_state.budget_used_usd).toFixed(2)} / $${Number(deployment.lease_state.budget_cap_usd).toFixed(0)} budget`
                        : "budget n/a"}
                      {deployment.lease_state.expires_at &&
                        ` · lease ends ${new Date(deployment.lease_state.expires_at).toLocaleDateString()}`}
                    </span>
                  }
                />
              )}
              <Row label="Stack" value={deployment.stack_name ?? "—"} mono />
              {deployment.app_url && (
                <Row
                  label="App URL"
                  value={
                    <a
                      href={deployment.app_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-indigo-400 underline hover:text-indigo-300"
                    >
                      {deployment.app_url} ↗
                    </a>
                  }
                />
              )}
              {deployment.status === "active" && deployment.app_url && hasConsole && (
                <Row
                  label="Test page"
                  value={
                    <a
                      href={`${deployment.app_url.replace(/\/$/, "")}/app`}
                      target="_blank"
                      rel="noreferrer"
                      className="text-indigo-400 underline hover:text-indigo-300"
                      title="Interactive test console for this agent (B19) — same origin as the API"
                    >
                      🧪 Open test console ↗
                    </a>
                  }
                />
              )}
              {deployment.status === "active" && hasApiKey && (
                <Row
                  label="API key"
                  value={
                    revealedKey ? (
                      <span className="flex items-center gap-2">
                        <code className="rounded bg-slate-900 px-1.5 py-0.5 text-xs text-slate-200">
                          {revealedKey.value}
                        </code>
                        <button
                          onClick={() => navigator.clipboard.writeText(revealedKey.value)}
                          className="text-xs text-indigo-400 hover:text-indigo-300"
                          title="Copy key"
                        >
                          copy
                        </button>
                      </span>
                    ) : (
                      <button
                        onClick={revealKey}
                        disabled={revealing}
                        title="Fetched live from the Enclave; the platform stores no copy. Reveals are audit-logged."
                        className="rounded-lg border border-slate-700 px-2.5 py-1 text-xs hover:border-indigo-400 disabled:opacity-40"
                      >
                        {revealing ? "Fetching…" : "🔑 Reveal key"}
                      </button>
                    )
                  }
                />
              )}
              {deployment.error && (
                <Row label="Error" value={<span className="text-red-300">{deployment.error}</span>} />
              )}
            </div>

            {deployment.status === "active" && deployment.mode === "testbed" && (
              <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4">
                <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
                  🧪 Testbed access
                </h3>
                <p className="mb-3 text-xs text-slate-400">
                  Mint limited, time-boxed AWS credentials for this Testbed account —
                  accept EULAs and marketplace agreements, raise service quotas,
                  configure extra services. Identity, billing and marshal&apos;s own
                  stack stay off-limits; budget and lease TTL still enforce. Every
                  issuance is audit-logged.
                </p>
                {testbedCreds ? (
                  <div className="space-y-2 text-xs">
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-slate-400">
                        Session{" "}
                        <code className="text-slate-300">{testbedCreds.session_name}</code>
                        {" · expires "}
                        {new Date(testbedCreds.expires_at).toLocaleString()}
                      </span>
                      {testbedCreds.console_url && (
                        <a
                          href={testbedCreds.console_url}
                          target="_blank"
                          rel="noreferrer"
                          className="shrink-0 text-indigo-400 underline hover:text-indigo-300"
                          title="Federated sign-in to the Testbed account console under the limited role"
                        >
                          🖥 Open AWS console ↗
                        </a>
                      )}
                    </div>
                    <pre className="overflow-x-auto rounded bg-slate-900 p-2 font-mono text-[11px] leading-relaxed text-slate-300">
                      {`export AWS_ACCESS_KEY_ID=${testbedCreds.access_key_id}
export AWS_SECRET_ACCESS_KEY=${testbedCreds.secret_access_key}
export AWS_SESSION_TOKEN=${testbedCreds.session_token}
export AWS_DEFAULT_REGION=${testbedCreds.region}`}
                    </pre>
                    <div className="flex items-center gap-3">
                      <button
                        onClick={() =>
                          navigator.clipboard.writeText(
                            `export AWS_ACCESS_KEY_ID=${testbedCreds.access_key_id}\nexport AWS_SECRET_ACCESS_KEY=${testbedCreds.secret_access_key}\nexport AWS_SESSION_TOKEN=${testbedCreds.session_token}\nexport AWS_DEFAULT_REGION=${testbedCreds.region}`
                          )
                        }
                        className="text-indigo-400 hover:text-indigo-300"
                      >
                        copy exports
                      </button>
                      <button
                        onClick={mintTestbedCreds}
                        disabled={minting}
                        className="text-indigo-400 hover:text-indigo-300 disabled:opacity-40"
                        title="Mint a fresh session (the old one keeps its own expiry)"
                      >
                        {minting ? "minting…" : "re-mint"}
                      </button>
                    </div>
                    <p className="text-[11px] text-slate-500">
                      Shown once — the platform stores no copy. Sessions expire on
                      their own; teardown reclaims the whole account.
                    </p>
                  </div>
                ) : (
                  <button
                    onClick={mintTestbedCreds}
                    disabled={minting}
                    className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs hover:border-indigo-400 disabled:opacity-40"
                    title="Issues STS credentials on the limited MarshalTestbedUserRole in this account. Audit-logged."
                  >
                    {minting ? "Minting…" : "🎫 Mint credentials"}
                  </button>
                )}
              </div>
            )}

            <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4">
              <h3 className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-400">
                Timeline
              </h3>
              <ol className="space-y-2 text-sm">
                {(deployment.timeline ?? []).map((entry, i) => (
                  <li key={i} className="flex items-baseline gap-3">
                    <span className="shrink-0 font-mono text-[11px] text-slate-400">
                      {new Date(entry.at).toLocaleTimeString()}
                    </span>
                    <span
                      className={
                        entry.phase === "failed"
                          ? "text-red-300"
                          : PHASES.includes(entry.phase as (typeof PHASES)[number]) ||
                              entry.phase === "torn_down"
                            ? "text-slate-200"
                            : "text-slate-400"
                      }
                    >
                      {PHASE_LABELS[entry.phase] ?? entry.phase}
                      {entry.detail ? (
                        <span className="text-slate-400"> — {entry.detail}</span>
                      ) : null}
                    </span>
                  </li>
                ))}
              </ol>
            </div>

            {deployment.resources?.length > 0 && (
              <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4">
                <h3 className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-400">
                  Resources ({deployment.resources.length})
                </h3>
                <ul className="space-y-1.5 text-xs">
                  {deployment.resources.map((r) => (
                    <li key={r.logical_id} className="flex items-center justify-between gap-2">
                      <span className="truncate font-mono text-slate-300">{r.logical_id}</span>
                      <span className="truncate text-slate-400">{r.type}</span>
                      <span
                        className={`shrink-0 ${
                          r.status.includes("COMPLETE")
                            ? "text-emerald-400"
                            : r.status.includes("FAILED")
                              ? "text-red-400"
                              : "text-amber-400"
                        }`}
                      >
                        {r.status}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          {showExtend && (
            <ModalShell label="Extend deployment" onClose={() => setShowExtend(false)} maxWidth="max-w-sm">
                <h3 className="font-medium">Extend deployment</h3>
                <p className="mt-1 text-xs text-slate-400">
                  Bounded by the platform max TTL and the Enclave lease expiry.
                </p>
                <label className="mt-4 block text-sm text-slate-400">
                  Additional hours
                  <input
                    type="number" min={1} max={168} value={extendHours}
                    onChange={(e) => setExtendHours(Number(e.target.value))}
                    className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
                  />
                </label>
                <div className="mt-4 flex justify-end gap-2">
                  <button
                    onClick={() => setShowExtend(false)}
                    className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={extend}
                    className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400"
                  >
                    Extend
                  </button>
                </div>
            </ModalShell>
          )}

          {preview && (
            <ModalShell label="Planned changes" onClose={() => setPreview(null)} maxWidth="max-w-2xl">
              <h3 className="font-medium">Planned changes</h3>
              <p className="mt-1 text-xs text-slate-400">
                CloudFormation change set for the selected payload against the running
                stack — evaluated and discarded, nothing was modified.
              </p>
              {preview.no_changes ? (
                <p className="mt-4 rounded-lg border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm text-slate-300">
                  No changes — the running stack already matches this payload.
                </p>
              ) : (
                <div className="mt-4 max-h-80 overflow-y-auto rounded-lg border border-slate-800">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-slate-950/80 uppercase tracking-wide text-slate-400">
                      <tr>
                        <th className="px-3 py-2">Action</th>
                        <th className="px-3 py-2">Resource</th>
                        <th className="px-3 py-2">Type</th>
                        <th className="px-3 py-2">Replacement</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800">
                      {preview.changes.map((c) => (
                        <tr key={`${c.action}-${c.logical_id}`}>
                          <td className={`px-3 py-2 font-medium ${
                            c.action === "Remove" ? "text-red-300"
                            : c.action === "Add" ? "text-emerald-300" : "text-amber-300"
                          }`}>
                            {c.action}
                          </td>
                          <td className="px-3 py-2 text-slate-200">{c.logical_id}</td>
                          <td className="px-3 py-2 text-slate-400">{c.resource_type}</td>
                          <td className="px-3 py-2 text-slate-400">
                            {c.replacement === "True" ? "⚠ replaced" : c.replacement === "Conditional" ? "conditional" : "in-place"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="mt-4 flex justify-end gap-2">
                <button
                  onClick={() => setPreview(null)}
                  className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
                >
                  Close
                </button>
                {!preview.no_changes && (
                  <button
                    onClick={() => {
                      setPreview(null);
                      deploy();
                    }}
                    className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400"
                  >
                    ⬆ Apply update
                  </button>
                )}
              </div>
            </ModalShell>
          )}

          <div className="flex min-h-[300px] flex-col rounded-lg border border-slate-800 bg-black/50">
            <div className="border-b border-slate-800 px-3 py-2 text-xs uppercase tracking-wide text-slate-400">
              Live events
            </div>
            <div className="flex-1 space-y-1 overflow-y-auto p-3 font-mono text-[11px] leading-relaxed">
              {logs.length === 0 ? (
                <p className="text-slate-400">
                  {IN_FLIGHT.has(status ?? "") ? "Waiting for events…" : "No live stream — showing persisted state."}
                </p>
              ) : (
                logs.map((line) => (
                  <div
                    key={line.id}
                    className={
                      line.kind === "phase"
                        ? "text-indigo-300"
                        : line.kind === "error"
                          ? "text-red-300"
                          : "text-slate-400"
                    }
                  >
                    <span className="text-slate-400">
                      {new Date(line.at).toLocaleTimeString()}{" "}
                    </span>
                    {line.text}
                  </div>
                ))
              )}
              <div ref={logEndRef} />
            </div>
          </div>
        </div>
      )}

      {history.length > 1 && (
        <div className="border-t border-slate-800 px-5 py-4">
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
            Deployment history
          </h3>
          <ul className="space-y-1.5 text-xs">
            {history.map((row) => (
              <li key={row.id} className="flex items-center gap-2 text-slate-400">
                <span className="w-4 text-center">{HISTORY_ICONS[row.status] ?? "·"}</span>
                <span className="uppercase tracking-wide">{row.status.replace(/_/g, " ")}</span>
                {row.health === "degraded" && <span className="text-amber-400">degraded</span>}
                {row.build_id && (
                  <span className="rounded bg-indigo-500/15 px-1 py-px font-mono text-[10px] text-indigo-300">
                    🛠 {row.build_id.slice(0, 8)}
                  </span>
                )}
                <span className="text-slate-400">
                  {row.created_by_name ? `${row.created_by_name} · ` : ""}
                  {new Date(row.created_at).toLocaleString()}
                </span>
                {row.app_url && row.status === "active" && (
                  <a href={row.app_url} target="_blank" rel="noreferrer"
                     className="text-indigo-400 hover:underline">↗</a>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function ExpiryChip({ expiresAt, onExtend }: { expiresAt: string; onExtend: () => void }) {
  const hoursLeft = Math.max(0, (new Date(expiresAt).getTime() - Date.now()) / 3_600_000);
  if (hoursLeft > 72) return null; // only surface when it starts to matter (R5.3)
  const urgent = hoursLeft < 24;
  return (
    <button
      onClick={onExtend}
      className={`rounded-full border px-2.5 py-0.5 text-xs ${
        urgent
          ? "border-red-500/40 bg-red-500/10 text-red-300"
          : "border-slate-600 bg-slate-800/60 text-slate-300"
      }`}
      title={`Auto-teardown at ${new Date(expiresAt).toLocaleString()} — click to extend`}
    >
      ⏳ expires in {hoursLeft < 1 ? "<1h" : `${Math.round(hoursLeft)}h`}
    </button>
  );
}

const HISTORY_ICONS: Record<string, string> = {
  active: "🟢",
  superseded: "↷",
  failed: "✕",
  torn_down: "▫",
};

function StatusPill({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: "bg-emerald-500/15 text-emerald-400 border-emerald-500/40",
    failed: "bg-red-500/15 text-red-400 border-red-500/40",
    torn_down: "bg-slate-700/40 text-slate-400 border-slate-600",
  };
  const inFlight = IN_FLIGHT.has(status);
  return (
    <span
      className={`flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs uppercase tracking-wide ${
        styles[status] ?? "border-amber-500/40 bg-amber-500/15 text-amber-400"
      }`}
    >
      {inFlight && (
        <span className="h-1.5 w-1.5 animate-ping rounded-full bg-current" />
      )}
      {status.replace(/_/g, " ")}
    </span>
  );
}

function Row({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1">
      <span className="shrink-0 text-slate-400">{label}</span>
      <span className={`truncate text-right ${mono ? "font-mono text-xs" : ""}`}>{value}</span>
    </div>
  );
}
