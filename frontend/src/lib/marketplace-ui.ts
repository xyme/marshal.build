/** Shared marketplace presentation helpers (catalog + detail + admin). */

export const CATEGORY_ART: Record<string, string> = {
  chatbot: "🤖",
  document_processing: "📄",
  data_analysis: "📊",
  workflow_automation: "🔄",
  content_generation: "✍️",
  custom: "🧩",
};

export const COMPLEXITY_STYLE: Record<string, string> = {
  beginner: "bg-emerald-500/15 text-emerald-400",
  intermediate: "bg-amber-500/15 text-amber-400",
  advanced: "bg-red-500/15 text-red-400",
};

export function modelLabel(id: string): string {
  if (id.startsWith("ext/")) return id.slice(4); // custom endpoint slug (S17)
  const withoutScope = id.replace(/^(us|global)\./, "");
  const firstDot = withoutScope.indexOf(".");
  const rawName = firstDot >= 0 ? withoutScope.slice(firstDot + 1) : withoutScope;
  return rawName
    .replace(/:0$/, "")
    .replace(/[._-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

/** Fork warnings hand-off: fork API → project page banner (survives redirect). */
export function stashForkWarnings(projectId: string, warnings: string[], sampleTitle: string) {
  try {
    sessionStorage.setItem(
      `fork-info-${projectId}`,
      JSON.stringify({ warnings, sampleTitle })
    );
  } catch {
    /* storage unavailable — banner just won't show */
  }
}

export function popForkWarnings(
  projectId: string
): { warnings: string[]; sampleTitle: string } | null {
  try {
    const raw = sessionStorage.getItem(`fork-info-${projectId}`);
    if (!raw) return null;
    sessionStorage.removeItem(`fork-info-${projectId}`);
    return JSON.parse(raw);
  } catch {
    return null;
  }
}
