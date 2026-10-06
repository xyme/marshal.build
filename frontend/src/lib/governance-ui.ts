/** Shared governance presentation helpers (risk badges, cost formatting). */

export const RISK_STYLE: Record<string, string> = {
  low: "bg-emerald-500/15 text-emerald-400",
  medium: "bg-amber-500/15 text-amber-400",
  high: "bg-red-500/15 text-red-400",
};

export const RISK_DOT: Record<string, string> = {
  low: "🟢",
  medium: "🟡",
  high: "🔴",
};

export function formatUsd(value: number | null | undefined, digits = 2): string {
  if (value == null) return "—";
  return `$${value.toFixed(digits)}`;
}
