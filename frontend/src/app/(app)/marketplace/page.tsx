"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { CATEGORY_ART, COMPLEXITY_STYLE, modelLabel } from "@/lib/marketplace-ui";
import type { CategoryCount, SampleCard, SampleList } from "@/lib/types";

const PAGE_SIZE = 12;

function MarketplaceCatalog() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [q, setQ] = useState(searchParams.get("q") ?? "");
  const category = searchParams.get("category") ?? "";
  const complexity = searchParams.get("complexity") ?? "";
  const sort = searchParams.get("sort") ?? "popular";

  const [items, setItems] = useState<SampleCard[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [categories, setCategories] = useState<CategoryCount[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const setParam = useCallback(
    (key: string, value: string) => {
      const params = new URLSearchParams(searchParams.toString());
      if (value) params.set(key, value);
      else params.delete(key);
      router.replace(`/marketplace?${params.toString()}`);
    },
    [router, searchParams]
  );

  const load = useCallback(
    async (pageToLoad: number, append: boolean) => {
      setLoading(true);
      try {
        const params = new URLSearchParams({
          sort,
          page: String(pageToLoad),
          page_size: String(PAGE_SIZE),
        });
        const qParam = searchParams.get("q");
        if (qParam) params.set("q", qParam);
        if (category) params.set("category", category);
        if (complexity) params.set("complexity", complexity);
        const body = await api<SampleList>(`/v1/marketplace/samples?${params}`);
        setItems((prev) => (append ? [...prev, ...body.items] : body.items));
        setTotal(body.total);
        setPage(pageToLoad);
        setError(null);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setLoading(false);
      }
    },
    [searchParams, category, complexity, sort]
  );

  useEffect(() => {
    load(1, false);
  }, [load]);

  useEffect(() => {
    api<CategoryCount[]>("/v1/marketplace/categories").then(setCategories).catch(() => {});
  }, []);

  function onSearchChange(value: string) {
    setQ(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setParam("q", value.trim()), 300);
  }

  function resetFilters() {
    setQ("");
    router.replace("/marketplace");
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Marketplace</h1>
        <p className="mt-1 text-sm text-slate-400">
          Curated, vetted sample apps — preview the full spec, then fork one as your starting point.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-800 bg-slate-900/40 p-4">
        <input
          value={q}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Search samples…"
          className="w-full max-w-xs rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
        />
        <select
          value={category}
          aria-label="Filter samples by category"
          onChange={(e) => setParam("category", e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
        >
          <option value="">All categories</option>
          {categories.map((c) => (
            <option key={c.category} value={c.category}>
              {c.category.replace(/_/g, " ")} ({c.count})
            </option>
          ))}
        </select>
        <select
          value={complexity}
          aria-label="Filter samples by complexity"
          onChange={(e) => setParam("complexity", e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
        >
          <option value="">Any complexity</option>
          <option value="beginner">Beginner</option>
          <option value="intermediate">Intermediate</option>
          <option value="advanced">Advanced</option>
        </select>
        <select
          value={sort}
          aria-label="Sort samples"
          onChange={(e) => setParam("sort", e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
        >
          <option value="popular">Most popular</option>
          <option value="newest">Newest</option>
          <option value="viewed">Most viewed</option>
        </select>
      </div>

      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {loading && items.length === 0 ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-48 animate-pulse rounded-xl border border-slate-800 bg-slate-900/40" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 p-10 text-center">
          <p className="text-slate-400">No samples match your search.</p>
          <button
            onClick={resetFilters}
            className="mt-2 text-sm text-indigo-400 hover:text-indigo-300"
          >
            Try broader keywords or browse all categories →
          </button>
        </div>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {items.map((sample) => (
              <Link
                key={sample.id}
                href={`/marketplace/${sample.id}`}
                className="flex flex-col rounded-xl border border-slate-800 bg-slate-900/60 p-5 transition hover:border-indigo-500/50"
              >
                <div className="text-2xl">{CATEGORY_ART[sample.category] ?? "🧩"}</div>
                <h2 className="mt-3 font-medium">{sample.title}</h2>
                <p className="mt-1 line-clamp-3 flex-1 text-sm text-slate-400">
                  {sample.description}
                </p>
                <div className="mt-4 flex flex-wrap items-center gap-2 text-[11px]">
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 capitalize text-slate-300">
                    {sample.category.replace(/_/g, " ")}
                  </span>
                  <span
                    className={`rounded px-1.5 py-0.5 capitalize ${COMPLEXITY_STYLE[sample.complexity]}`}
                  >
                    {sample.complexity}
                  </span>
                  {sample.models_used[0] && (
                    <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">
                      {modelLabel(sample.models_used[0])}
                    </span>
                  )}
                  <span className="ml-auto text-slate-400">
                    {sample.fork_count} fork{sample.fork_count === 1 ? "" : "s"}
                  </span>
                </div>
                {sample.contributed_by && (
                  <p className="mt-2 text-[11px] text-slate-400">
                    🤝 Contributed by {sample.contributed_by}
                  </p>
                )}
              </Link>
            ))}
          </div>
          <div className="flex items-center justify-between text-sm text-slate-400">
            <span>
              Showing {items.length} of {total} sample{total === 1 ? "" : "s"}
            </span>
            {items.length < total && (
              <button
                onClick={() => load(page + 1, true)}
                disabled={loading}
                className="rounded-lg border border-slate-700 px-4 py-2 hover:border-slate-500 disabled:opacity-40"
              >
                {loading ? "Loading…" : "Load more"}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export default function MarketplacePage() {
  return (
    <Suspense fallback={<p className="text-slate-400">Loading…</p>}>
      <MarketplaceCatalog />
    </Suspense>
  );
}
