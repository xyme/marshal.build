# Reliability: SLOs, capacity planning & business continuity

Sprint 13 (Alpha 4) deliverable. Every number here was measured on production,
not estimated. Re-measure with `scripts/loadtest.mjs` after any change to the
request path, and update this document in the same commit.

## 1. Service level objectives

Sized for the Alpha 4 assumption of **≈50 concurrent users** (owner decision D4).

| Metric | Target | Measured (1× ≈50 users) | Measured (5× ≈250 users) |
|---|---|---|---|
| API reads p95 (`/projects`, `/notifications/unread-count`, `/marketplace/samples`, `/users/me/stats`, `/templates`) | < 500 ms | **481 ms** ✓ | **469 ms** ✓ |
| Admin dashboards p95 (costs, breakdowns, risk queue, audit) | < 1500 ms | **520 ms** ✓ | **569 ms** ✓ |
| Chat SSE first event p95 (send → first streamed token) | < 3000 ms | **2927 ms** ✓ | **2843 ms** ✓ |
| Server 5xx at 5× load | zero | — | **0 / 16,900 ALB requests** ✓ |

Run conditions: 29 Jul 2026, single-region us-east-1, fleet at 4 backend + 2
frontend tasks, driven from one workstation through CloudFront.

**Measurement caveats (read before trusting these numbers):**
- ~255 ms of every reading is transport floor (workstation → CloudFront edge →
  ALB → frontend SSR proxy → Service Connect → backend). `pg_stat_statements`
  after 21k requests shows **no application query above 1 ms mean**, so latency
  is dominated by network hops, not compute or the database.
- The load driver is one machine; at 5× the client itself contributes queueing.
  Treat 5× as "does it stay correct and error-free", not as a precise latency
  figure.
- SSE first-event timing starts at the message send. Session creation is
  excluded deliberately — in real use the session already exists when the user
  presses send.

### Running the harness

```bash
DEMO_USER_PASSWORD='...' node scripts/loadtest.mjs                    # 1× baseline
DEMO_USER_PASSWORD='...' LOAD_MULTIPLIER=5 DURATION_S=240 node scripts/loadtest.mjs
DEMO_USER_PASSWORD='...' SLO_CHECK=1 node scripts/loadtest.mjs        # non-zero exit on breach
```

Run on a **quiescent fleet**. A run overlapping a deployment or a scaling event
records 502s from task draining and misattributes them to the application (this
happened during the S13 drills and cost an hour of false-positive chasing).

## 2. Capacity planning

Bind capacity to the demand policy, not to guesswork. To size for N concurrent users:

| Knob | Where | Rule |
|---|---|---|
| Backend tasks | ECS autoscaling (`MarshalAppStack`) | floor 2, ceiling 4; CPU target 60% + ALB request-count steps. 4 tasks absorbed 250 simulated users at 60 rps with zero errors |
| Concurrent deployments | Admin → Model Controls → Deployment policies (`max_concurrent_platform`) | Set to the number of Enclaves you are willing to run at once; the platform refuses beyond it with `policy_concurrency_platform` |
| Enclave account pool (`isb` only) | ISB console (manual registration) | General target: **pool ≥ `max_concurrent_platform` + 2** recycling headroom. A smaller installation can run a one-buffer posture instead (for example three healthy accounts, platform cap 2 and `provider_capacity_buffer=1`): admission reads `/accounts` under the transaction lock and fails closed if the buffer cannot remain, so the pool is never driven to zero. ISB does not auto-grow the pool. In `direct` mode there is no pool; the concurrency limits above are the capacity |
| Per-user deployments | `max_concurrent_per_user` | Default 3. Raise deliberately: each is a real AWS account lease |
| Bedrock throughput | Service Quotas (us-east-1) | Check on-demand TPM/RPM for the Sonnet/Haiku inference profiles before an event; spec generation is the burst source |
| Platform model spend | Model Controls → Cost (`platform_budget_usd`) | Set before opening access; `at_cap=block` makes it a hard stop |
| Database | `MarshalDataStack` | db.t4g.micro is adequate at these numbers (no query >1 ms). Scale up only against measured evidence |

Scaling posture: **the platform refuses work it cannot govern** rather than
degrading — concurrency policies, cost caps, and rate limits all fail closed
with actionable messages, which is the intended enterprise behavior.

## 3. Business continuity

### Targets

| | Target | Evidence (29 Jul 2026 drill) |
|---|---|---|
| RPO (max data loss) | 24 h | **~1 second** — restored instance's newest audit row was 04:35:37Z against a latest-restorable time of 04:35:38Z |
| RTO (database recovery) | 4 h | **~32 min** to `available` (04:39:08Z → 05:10:41Z), plus verification |

Both targets are met with wide margin. RPO is bounded by continuous PITR
(5-minute transaction-log granularity), not by the nightly snapshot.

### Posture by data store

| Store | Protection | Loss impact |
|---|---|---|
| RDS Postgres (system of record) | Multi-AZ, deletion protection, 7-day automated backups + PITR. **Storage encryption is off in the current `data-stack.ts`; enabling it on an existing instance is a snapshot-copy cutover (ROADMAP.md)** | Recoverable to seconds. Contains users, projects, specs, builds, deployments, audit |
| DynamoDB `marshal-runtime-state` | None by design | Rebuildable cache tier: rate windows self-expire, spend counters re-seed from `model_invocations` truth on next boot (`SpendTracker.hydrate`) |
| DynamoDB `marshal-chat-messages` | Point-in-time recovery enabled | Chat transcripts |
| S3 codegen workspace | Versioning off; `builds/` 30-day, `artifacts/` 180-day lifecycle | Generated artifacts are reproducible from specs by rebuilding |
| Secrets Manager | AWS-managed durability + recovery window | Credentials |
| Enclave (ISB) accounts | Not backed up — deliberately ephemeral | Deployed workloads are redeployable from a ready build; teardown is the normal end state |

### Restore drill (repeat quarterly and after any DataStack change)

```bash
# 1. Restore to a new instance (never over the live one)
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier <prod-instance-id> \
  --target-db-instance-identifier marshal-restore-drill \
  --use-latest-restorable-time --db-instance-class db.t4g.micro \
  --db-subnet-group-name <prod-subnet-group> \
  --vpc-security-group-ids <prod-db-sg> --no-multi-az --no-publicly-accessible

# 2. Wait, then verify FROM INSIDE the VPC (ECS exec on a backend task):
#    - alembic_version matches the production chain tip
#    - row counts on users/projects/specs/deployments are plausible
#    - max(audit_logs.created_at) is close to the restore point
# 3. Delete the drill instance
aws rds delete-db-instance --db-instance-identifier marshal-restore-drill \
  --skip-final-snapshot --delete-automated-backups
```

Recovery runbook for a real incident: promote by pointing `DB_HOST` at the
restored endpoint (AppStack env) and redeploying, rather than renaming
instances — the app composes its DSN from `DB_SECRET_JSON` + `DB_HOST` at
container start, so a restored instance is adopted with one deploy.

### Single-region posture (accepted risk)

Everything runs in us-east-1: control plane, database, Bedrock inference
profiles, and the Enclave account pool. A regional outage is an accepted
outage — there is no warm standby, and Bedrock model availability differs
per region.

**Multi-region trigger:** revisit when an enterprise customer contractually
requires regional failover, or when Preview/GA introduces an availability
commitment stronger than best-effort. The work would be: cross-region RDS read
replica with promotion, an S3 artifact replication rule, a second ECS/ALB
stack behind the existing CloudFront distribution, and a decision on Bedrock
region parity.

## 4. Findings from the S13 drills

Recorded because they are the kind of thing that silently returns:

1. **ELB 502s under load (fixed).** Node's default 5-second keep-alive was
   shorter than the ALB's 60-second idle timeout, so the ALB reused sockets the
   frontend had already closed — 3 ELB-generated 502s in 17k requests with
   `TargetConnectionErrorCount` at 0 and no target error. Fixed by setting
   `KEEP_ALIVE_TIMEOUT=65000` on the frontend container and pinning the ALB
   idle timeout to 60s so the ordering is explicit. Zero 5xx afterwards.
2. **Autoscaling looked broken but wasn't.** The first 5× run held 84% CPU for
   ~3 minutes and never scaled, because target-tracking alarms need ~3 minutes
   of breach before acting. A longer run scaled 2→3→4 backend and 1→2 frontend
   tasks. Scale-out cooldown reduced to 60s, and ALB request-count added as a
   second signal (I/O-bound saturation raises latency long before CPU).
3. **No database work was warranted.** `pg_stat_statements` showed no app query
   above 1 ms mean, so the planned index pass was closed as unnecessary rather
   than performed on speculation. The only app-level wins were removing an N+1
   in the admin risk queue (p95 726 → 577 ms) and collapsing three sequential
   COUNT queries into one grouped pass.
4. **Enclave spend attribution was broken when the flag went live (fixed).**
   Cost-day attribution tested whether a day's midnight fell *inside* the lease
   window, so any lease shorter than a day — every drill lease and most real
   ones — was never attributed. Now attributes each cost day to the lease with
   the greatest overlap, with regression tests.
5. **An S12 observability regression (fixed).** Moving the sandbox-spend poll
   under the scheduler election dropped the log line that reported its result,
   so the poll ran invisibly. The result is now logged inside `poll_once`,
   independent of the caller.
