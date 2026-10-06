/**
 * Installation sizing tier (install drill G18).
 *
 *   production (default)  today's exact sizing: RDS Multi-AZ, two-task floors,
 *                         Container Insights on. Synth output is unchanged.
 *   evaluate              single-AZ RDS, one-task floors (desiredCount 1,
 *                         autoscaling minCapacity 1; ceilings unchanged),
 *                         Container Insights off. Roughly $120–125/month idle
 *                         instead of $190–195 (README "What it costs").
 *
 * Selected by the deploy-time MARSHAL_TIER environment variable (overlay key,
 * exported by scripts/deploy-env.sh) or the CDK context `-c marshalTier=…`;
 * context wins when both are set. Nothing else — NAT count, task size,
 * ALB/WAF posture — moves with the tier.
 */
export type MarshalTier = "production" | "evaluate";

export const MARSHAL_TIERS: readonly MarshalTier[] = ["production", "evaluate"];

export function resolveMarshalTier(raw: unknown): MarshalTier {
  const value = typeof raw === "string" ? raw.trim().toLowerCase() : "";
  if (value === "") return "production";
  if ((MARSHAL_TIERS as readonly string[]).includes(value)) return value as MarshalTier;
  throw new Error(
    `MARSHAL_TIER must be one of ${MARSHAL_TIERS.join(", ")} (got "${String(raw)}")`
  );
}
