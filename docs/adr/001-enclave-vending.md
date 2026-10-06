# ADR 001 — Enclave account vending: keep Innovation Sandbox, extend platform-side

- **Status:** Accepted (24 Jul 2026, S12 — resolves FSD §11 OQ-7)
- **Owners:** Platform
- **Context:** FSD §4.5 (Deploy to Enclave), §13.3 (`SandboxProvider` seam), §11 OQ-7

## Question

Innovation Sandbox on AWS (ISB) powers Enclave account vending at Alpha/Beta.
Its semantics are lease-centric: TTL expiry, budget-driven termination, AWS
Nuke recycling. As Enclaves carry longer-lived production-posture workloads,
does ISB stretch (long/no-expiry leases, per-lease budget profiles), or should
the `SandboxProvider` seam gain a purpose-built vending provider?

## Findings (spike against the deployed July 2026 release)

**What ISB gives us for free (and would be expensive to rebuild):**

- Account pooling with recycling — AWS Nuke sweep + re-registration after every
  lease, which is the isolation property the Enclave promise rests on.
- Lease state machine (`Provisioning → Active → Expired/BudgetExceeded/…`) with
  budget accrual (`totalCostAccrued`) surfaced per lease — feeds the S11 lease
  strip without any platform-side metering.
- Blueprint StackSets — the deployment role (`AIFactoryDeploymentRole`) and
  S10 staging-bucket rights arrive in every vended account without a platform
  provisioning pipeline.
- Observed vending latency in live drills: lease Active (incl. blueprint) in
  low minutes; acceptable against deploy expectations (§4.5 timeline shows a
  "leasing" phase).

**Where ISB does not stretch (verified against the API and source):**

1. **Per-lease budget/duration overrides.** `POST /leases` accepts only
   `{leaseTemplateUuid, comments}`. Budget (`maxSpend`) and duration
   (`leaseDurationInHours`) live on the TEMPLATE. marshal auto-creates one
   template (`maxSpend` $1000, 720 h) and every lease inherits it. The S12
   policy `per_deployment_budget_usd` therefore CANNOT map onto the ISB lease.
   Workaround assessed: template-per-budget-profile (create/find a template
   keyed by budget×duration). Viable but produces template sprawl and couples
   policy edits to ISB writes; rejected for Beta.
2. **Long/no-expiry residency.** Templates require a finite
   `leaseDurationInHours`. Very long durations (e.g. 8760 h) are unvalidated
   against the release's bounds, and a lease that never recycles forfeits the
   recycling guarantee anyway — the account drifts from pool hygiene.
3. **Pool elasticity.** The pool is statically registered accounts
   (`POST /accounts`); there is no auto-grow. Capacity planning is manual:
   pool size ≥ `max_concurrent_platform` policy + recycling headroom.

## Decision

**Keep ISB as the vending engine through Beta and GA-candidate scale, and
enforce the deployment-policy layer platform-side** (as built in S12):

- `per_deployment_budget_usd` is recorded on the platform `leases.budget_usd`
  row at creation and enforced via platform surfaces (costs dashboard, future
  reaper); the ISB template `maxSpend` ($1000) stays as the hard backstop.
- TTL is enforced platform-side (S11 sweeper, default 72 h / max 168 h policy,
  extend endpoint) well inside the 720 h ISB lease; the lease−6 h clamp keeps
  platform teardown ahead of ISB reaping, so ISB expiry is the backstop, never
  the primary control.
- Platform concurrency policies size the demand side; pool registration sizes
  the supply side (runbook: keep registered accounts ≥ platform concurrency
  limit + 2 recycling headroom).

## Replacement triggers (revisit this ADR when any fires)

1. Production workloads need guaranteed residency beyond 30 days without
   account recycling (GA "keep my app running" tier).
2. Policy requires true per-lease budget/duration control that template
   sprawl cannot reasonably express.
3. Pool demand outgrows manual registration (elastic account vending needed).

**Replacement path when triggered:** a purpose-built provider behind the
existing `SandboxProvider` seam (`request_lease / get_lease / lease_state /
terminate_lease / deployment_session`) using Organizations account vending +
a platform-owned cleanup pipeline. The seam already isolates the deployment
engine from vending; this is a provider swap, not a rewrite. Estimated as a
sprint-scale effort dominated by the cleanup/recycling pipeline (the part ISB
currently gives us for free).

## Consequences

- Beta ships with two budget numbers per lease: the platform policy budget
  (authoritative for governance surfaces) and the ISB template backstop. The
  S11 lease strip labels the ISB figure; the costs dashboard labels the
  platform figure. Documented in §13 S12 as-built notes.
- Admin runbook gains a pool-capacity check tied to the
  `max_concurrent_platform` policy.
- OQ-7 closes; backlog carries the three triggers as watch items instead of
  an open question.
