# On-call runbook (S16-01)

Operating notes for the marshal control plane. Written for the person paged
by `marshal-ops-alerts`. Dashboard: CloudWatch → **marshal-ops** (us-east-1).

**The stack in one line:** CloudFront (+WAF) → ALB (origin-verify header) →
frontend (Next SSR, ECS marshal-frontend) → Service Connect envoy →
backend (FastAPI, ECS marshal-backend ×2-4) → RDS Postgres (multi-AZ) +
DynamoDB (chat, runtime-state) + Bedrock; deployed apps live in Enclave
accounts vended by ISB, polled by the S11 lifecycle ticks.

**Tools:** AWS CLI on the ops role; ECS exec for a shell in a task
(`aws ecs execute-command --cluster marshal --task <id> --interactive
--command bash` — needs the local session-manager-plugin, an S12 lesson);
`GET /api/v1/meta/build-info` for what is actually running (git SHA +
migration heads); `scripts/ui-smoke.mjs` for a full browser-path check.

---

## Alarms → what to do

### <a id="alb-5xx"></a>marshal-alb-target-5xx
Targets returned 5xx. First: `marshal-ops` dashboard → which side (ELB vs
target counts) and when it started; correlate with the last deploy
(`build-info` SHA vs the repo).
1. Backend stack traces: CloudWatch Logs `AppLogs`, streams `backend/*`.
2. If it started at a deploy → the circuit breaker should have rolled back;
   if it did not, redeploy the previous image tag (ECR keeps `IMAGE_TAG`
   history) **with the full env context** (see deploy cadence below).
3. ELB-generated 502 with zero target errors = the S13-02 keep-alive class:
   verify `KEEP_ALIVE_TIMEOUT=65000` on the frontend task and ALB idle 60s
   (invariant: target keep-alive > ALB idle; envoy SC idle 240s < uvicorn 300s).

### <a id="latency"></a>marshal-alb-p95-latency
p95 > 2s for 15 min (S13 envelope: 5x load ≈ 59 rps stayed well under this).
1. Dashboard: CPU rows — if backend CPU ≥ 60% sustained, autoscaling should be
   adding tasks (floor 2, max 4). If pinned at max, that is the capacity story.
2. CPU flat but latency high = I/O wait: RDS (connections/locks) or Bedrock
   streams. Check the Bedrock widget for throttles first — model waits
   masquerade as API latency.
3. Long-running: check the relay widget; a flapping relay forces SSE
   reconnect storms that show up as latency.

### <a id="task-health"></a>marshal-*-unhealthy-hosts
1. `aws ecs describe-services --cluster marshal --services marshal-backend
   marshal-frontend` → events tail tells you why tasks are cycling
   (OOM, failed health checks, image pull).
2. Crash-looping after a deploy → placeholder-env failure mode (below) or a
   migration mismatch: `build-info.migrations_in_sync=false` on a task that
   booted before its migration ran means the entrypoint migration step failed —
   check the migration stream in `AppLogs`.
3. OOM: frontend task is 1024MiB — mermaid/monaco SSR spikes have hit this;
   restart is self-healing, recurrence = raise memory, note it in §13.

### <a id="ddb-throttles"></a>marshal-ddb-*-throttles
Both tables are PAY_PER_REQUEST — throttles mean a hot partition, not
capacity. Chat table: one conversation being hammered (check rate-limit
metrics: the 429 seam should be absorbing this — if 429s are zero AND
throttles are high, the limiter is being bypassed somewhere). Runtime-state:
spend counters/lease sweeps — the S12 shared-state seam retries; sustained
throttling here breaks cap enforcement accuracy → treat as high priority.

### <a id="bedrock"></a>marshal-bedrock-throttles
Platform-side backoff already retries (3 attempts). Sustained throttles:
1. Check the costs dashboard for a runaway consumer (top spenders).
2. Per-user rate limits are the intended brake — verify 429s are being served
   (`rate_limited` security audit events).
3. Regional capacity issues: nothing to do but wait; the UI degrades with
   explicit errors. Do NOT raise platform caps to "fix" throttling.

### <a id="relay"></a>marshal-relay-flapping
Event relay = Postgres LISTEN/NOTIFY. Reconnects mean the DB connection is
dying repeatedly: check RDS failover events / connection counts. During a
flap the platform is NOT broken — buses fall back to local-only mode, SSE
just loses cross-task fan-out (a viewer on task A may miss events produced on
task B; persisted timelines cover the replay). Steady reconnects with healthy
RDS = look for network policy changes.

### <a id="audit"></a>marshal-audit-write-failures
Audit writes are fire-and-forget so the product keeps working — but every
failure is compliance evidence lost. The stdout JSON mirror in `AppLogs`
(`"audit "` lines) still has the entries: capture them for backfill. Cause is
almost always DB availability or a schema drift; fix, then verify with any
mutating request.

### Scheduler ticks (dashboard only, no alarm)
`ran` + `skipped` per interval across N tasks is HEALTHY (election working:
exactly one runs). Alarm-worthy anomaly you might see on the widget: zero
`ran` across two intervals = both tasks failing the advisory lock — check DB
connectivity; the lifecycle/escalation/spend sweeps are stalled.

---

## Incident learnings (S10–S13, paid for in drills)

**Deploy cadence / placeholder-env failure mode (S12, sev-high).** A bare
`cdk deploy MarshalAppStack` bakes synth-time placeholder values into task
definitions and **breaks auth platform-wide**. Deploys MUST go through
`scripts/deploy-cloud.sh` (or carry the same env: `COGNITO_*`,
`ORIGIN_VERIFY_TOKEN`, `ISB_*`) and image builds MUST pass the
`NEXT_PUBLIC_*` build args. Caught by smoke, fixed by re-deploying with full
env. If auth suddenly 401s for everyone right after an infra change, this is
the first suspect. Always run `scripts/ui-smoke.mjs` after any deploy touching
frontend, proxy, or networking.

**Keep-alive 502s (S13-02).** Sporadic ELB 502s with no target errors were
socket reuse against closing targets. The invariant is encoded in
app-stack.ts: frontend `KEEP_ALIVE_TIMEOUT` (65s) > ALB idle (60s), and
Service Connect envoy idle (240s) < uvicorn keep-alive (300s). Do not "tune"
one side alone.

**In-place update rollback states (S11).** `UPDATE_ROLLBACK_COMPLETE` on a
generated-app stack = the previous build still serves (by design).
`UPDATE_ROLLBACK_FAILED` = stuck stack: a critical alert fires; teardown and
redeploy is the only path (never hand-edit Enclave stacks).

**Post-deploy smoke (S16-02).** After CREATE/UPDATE_COMPLETE the deployer
probes the routes the template declares; failures mark the deployment
`degraded` and write the per-route report to the build card. A degraded-only
state (stack green, smoke red) is a codegen QUALITY issue, not an infra one —
route it to the build owner, not on-call.

**Restore (S13-03).** PITR is enabled on RDS (7 days) — restore to a new
instance, repoint `DATABASE_URL` secret, roll tasks.

---

## Escalation

1. On-call (this doc) — 30 min box.
2. Platform owner (repo owner) — auth, tenancy, spend-cap and Enclave-vending
   incidents escalate immediately: these have compliance surface.
3. Evidence discipline: timeline in the incident doc, audit trail + `AppLogs`
   exports attached, learnings PR'd into this runbook and §13.
