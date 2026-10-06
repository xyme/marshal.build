import Link from "next/link";

/**
 * Contextual help affordance (S15-05): a small labelled link from a surface to
 * the documentation page that explains it.
 *
 * Deliberately a real link rather than a tooltip — tooltips are invisible to
 * keyboard and touch users, and the explanation belongs somewhere durable.
 */
export default function HelpLink({
  doc,
  label = "How this works",
}: {
  /** Slug from src/lib/docs.ts, optionally with a #heading anchor */
  doc: string;
  label?: string;
}) {
  return (
    <Link
      href={`/docs/${doc}`}
      className="inline-flex items-center gap-1 text-xs text-slate-400 underline decoration-dotted underline-offset-2 hover:text-indigo-300"
    >
      <span aria-hidden="true">?</span>
      {label}
    </Link>
  );
}
