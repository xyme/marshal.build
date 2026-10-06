"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { DOC_PAGES } from "@/lib/docs";
import type { SearchResults } from "@/lib/types";

/** Global search dialog (beta-usability R2 — FSD B12).
 *
 * One debounced backend call (projects/sessions/samples, access-filtered
 * server-side) + a synchronous client-side docs filter (DOC_PAGES is a
 * build-time registry — routing it through the backend would create drift).
 * Listbox keyboard model: arrows move, Enter opens, Escape closes. */

interface ResultRow {
  id: string;
  group: string;
  label: string;
  detail: string | null;
  href: string;
}

const MIN_CHARS = 2;

export default function SearchDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResults | null>(null);
  const [searching, setSearching] = useState(false);
  const [active, setActive] = useState(0);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const rows = useMemo<ResultRow[]>(() => {
    const q = query.trim().toLowerCase();
    if (q.length < MIN_CHARS) return [];
    const out: ResultRow[] = [];
    for (const p of results?.groups.projects ?? []) {
      out.push({
        id: `project-${p.id}`,
        group: "Projects",
        label: p.name,
        detail: p.status.replace(/_/g, " "),
        href: `/projects/${p.id}`,
      });
    }
    for (const s of results?.groups.sessions ?? []) {
      out.push({
        id: `session-${s.id}`,
        group: "Sessions",
        label: s.title,
        detail: new Date(s.updated_at).toLocaleDateString(),
        href: `/chat?session=${s.id}`,
      });
    }
    for (const s of results?.groups.samples ?? []) {
      out.push({
        id: `sample-${s.id}`,
        group: "Marketplace",
        label: s.title,
        detail: s.category.replace(/_/g, " "),
        href: `/marketplace/${s.id}`,
      });
    }
    for (const s of results?.groups.specs ?? []) {
      out.push({
        id: `spec-${s.project_id}-${s.type}`,
        group: "Specs",
        label: `${s.project_name ?? "Project"} — ${s.type}.md`,
        detail: `v${s.version}`,
        href: `/projects/${s.project_id}?tab=spec`,
      });
    }
    // Docs: client-side title filter over the static registry (R2.2)
    for (const doc of DOC_PAGES.filter((d) => d.title.toLowerCase().includes(q)).slice(0, 5)) {
      out.push({
        id: `doc-${doc.slug}`,
        group: "Docs",
        label: doc.title,
        detail: "documentation",
        href: `/docs/${doc.slug}`,
      });
    }
    return out;
  }, [results, query]);

  useEffect(() => {
    setActive(0);
  }, [rows.length, query]);

  const runSearch = useCallback((value: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (value.trim().length < MIN_CHARS) {
      setResults(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    debounceRef.current = setTimeout(() => {
      api<SearchResults>(`/v1/search?q=${encodeURIComponent(value.trim())}`)
        .then((r) => {
          setResults(r);
          setSearching(false);
        })
        .catch(() => {
          setResults(null);
          setSearching(false);
        });
    }, 300);
  }, []);

  const open = useCallback(
    (row: ResultRow) => {
      onClose();
      router.push(row.href);
    },
    [onClose, router]
  );

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((v) => Math.min(v + 1, rows.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((v) => Math.max(v - 1, 0));
    } else if (e.key === "Enter" && rows[active]) {
      e.preventDefault();
      open(rows[active]);
    }
  }

  let lastGroup = "";
  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 p-4 pt-[12vh]"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search"
        className="mx-auto w-full max-w-xl overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-2xl"
        onKeyDown={onKeyDown}
      >
        <div className="flex items-center gap-2 border-b border-slate-800 px-4">
          <span aria-hidden className="text-slate-400">🔍</span>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              runSearch(e.target.value);
            }}
            placeholder="Search projects, sessions, marketplace, docs…"
            aria-label="Search"
            role="combobox"
            aria-expanded={rows.length > 0}
            aria-controls="global-search-results"
            aria-activedescendant={rows[active] ? `search-opt-${rows[active].id}` : undefined}
            className="w-full bg-transparent py-3.5 text-sm outline-none placeholder:text-slate-500"
          />
          <kbd className="rounded border border-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400">esc</kbd>
        </div>
        <div className="max-h-[50vh] overflow-y-auto p-2">
          {query.trim().length < MIN_CHARS ? (
            <p className="px-3 py-6 text-center text-sm text-slate-400">
              Type at least {MIN_CHARS} characters to search.
            </p>
          ) : rows.length === 0 ? (
            <p className="px-3 py-6 text-center text-sm text-slate-400">
              {searching ? "Searching…" : `No matches for “${query.trim()}”.`}
            </p>
          ) : (
            <ul id="global-search-results" role="listbox" aria-label="Search results">
              {rows.map((row, index) => {
                const header = row.group !== lastGroup ? row.group : null;
                lastGroup = row.group;
                return (
                  <li key={row.id} role="presentation">
                    {header && (
                      <div className="px-3 pb-1 pt-3 text-[10px] uppercase tracking-widest text-slate-500">
                        {header}
                      </div>
                    )}
                    <div
                      id={`search-opt-${row.id}`}
                      role="option"
                      aria-selected={index === active}
                      onMouseEnter={() => setActive(index)}
                      onMouseDown={(e) => {
                        e.preventDefault();
                        open(row);
                      }}
                      className={`flex cursor-pointer items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm ${
                        index === active ? "bg-slate-800 text-white" : "text-slate-300"
                      }`}
                    >
                      <span className="truncate">{row.label}</span>
                      {row.detail && (
                        <span className="shrink-0 text-xs text-slate-500">{row.detail}</span>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
