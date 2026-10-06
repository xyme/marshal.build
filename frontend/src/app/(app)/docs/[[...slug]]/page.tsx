import Link from "next/link";
import { notFound } from "next/navigation";
import SpecMarkdown from "@/components/markdown/SpecMarkdown";
import { DOC_PAGES, getDoc } from "@/lib/docs";

/**
 * In-app documentation (S15-04, product decision D7).
 *
 * Content is markdown in the repository (`src/content/docs`), imported at build
 * time: no new infrastructure, no separate deploy, and the docs are versioned
 * with the code they describe. Rendered through the same sanitising markdown
 * component the specification viewer uses.
 */
export default async function DocsPage({
  params,
}: {
  params: Promise<{ slug?: string[] }>;
}) {
  const { slug } = await params;
  const key = slug?.[0] ?? DOC_PAGES[0].slug;
  const doc = getDoc(key);
  if (!doc) notFound();

  return (
    <div className="flex flex-col gap-6 lg:flex-row">
      <nav aria-label="Documentation" className="lg:w-56 lg:shrink-0">
        <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
          Documentation
        </p>
        <ul className="space-y-1 text-sm">
          {DOC_PAGES.map((page) => (
            <li key={page.slug}>
              <Link
                href={`/docs/${page.slug}`}
                aria-current={page.slug === key ? "page" : undefined}
                className={`block rounded-lg px-3 py-1.5 ${
                  page.slug === key
                    ? "bg-slate-800/70 text-white"
                    : "text-slate-400 hover:bg-slate-900/60 hover:text-slate-200"
                }`}
              >
                {page.title}
              </Link>
            </li>
          ))}
        </ul>
      </nav>

      <article className="min-w-0 flex-1 rounded-xl border border-slate-800 bg-slate-900/40 p-6">
        <SpecMarkdown content={doc.content} />
      </article>
    </div>
  );
}
