"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { signOut } from "next-auth/react";
import { useEffect, useRef, useState } from "react";
import NotificationBell from "@/components/shell/NotificationBell";
import SearchDialog from "@/components/shell/SearchDialog";
import { api } from "@/lib/api";
import { useUser } from "@/components/user-context";

/** Persona-adaptive navigation (user-persona-profile spec R3). */
export default function AppShell({ children }: { children: React.ReactNode }) {
  const { user } = useUser();
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);

  const previewExperience = user?.can_demo_switch ? user.experience_view : null;
  const presentationExperience = previewExperience ?? user?.persona;
  const isPower = presentationExperience === "power";
  const [reviewerMeta, setReviewerMeta] = useState<{ is_reviewer: boolean; pending: number } | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const searchButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    api<{ is_reviewer: boolean; pending: number }>("/v1/reviews/meta")
      .then(setReviewerMeta)
      .catch(() => {});
  }, []);
  // Global search shortcut (B12 R2.4) — the app's first window-level keybinding
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen((v) => !v);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const closeSearch = () => {
    setSearchOpen(false);
    searchButtonRef.current?.focus(); // focus restore (R2.5)
  };

  const navItems = [
    { href: "/home", label: "Home", tour: undefined },
    { href: "/chat", label: "Chat", tour: "chat" },
    { href: "/projects", label: "My Projects", tour: "projects" },
    { href: "/marketplace", label: "Marketplace", tour: undefined },
    ...(reviewerMeta?.is_reviewer
      ? [{
          href: "/reviews",
          label: reviewerMeta.pending > 0 ? `Reviews (${reviewerMeta.pending})` : "Reviews",
          tour: undefined,
        }]
      : []),
    // 17 Sep 2026 fix: this item had pointed at /projects since S15 — a
    // dead label. It now targets the real cross-project deployments page.
    ...(isPower ? [{ href: "/deployments", label: "Deployments", tour: "deploy" }] : []),
    { href: "/docs", label: "Docs", tour: undefined }, // S15-04
    // The real /admin link is gated by the persisted role, or by the
    // administrator-managed view-only capability (product decision, 4 Sep
    // 2026) — never by presentation state.
    ...(user?.role === "admin" || user?.admin_readonly
      ? [{ href: "/admin/templates", label: "Admin", tour: undefined }]
      : []),
  ];

  async function handleSignOut() {
    await signOut({ redirect: false });
    const domain = process.env.NEXT_PUBLIC_COGNITO_DOMAIN;
    const clientId = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID;
    if (domain && clientId) {
      window.location.href = `${domain}/logout?client_id=${clientId}&logout_uri=${encodeURIComponent(
        window.location.origin
      )}`;
    } else {
      window.location.href = "/";
    }
  }

  if (pathname === "/onboarding") return <>{children}</>;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      {/* S15-06: first tab stop jumps past the nav for keyboard users */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-indigo-600 focus:px-4 focus:py-2 focus:text-sm focus:text-white"
      >
        Skip to main content
      </a>
      <header className="sticky top-0 z-40 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center justify-between px-4">
          <div className="flex items-center gap-8">
            <Link href="/home" className="flex items-center gap-2 font-semibold tracking-tight">
              <Image
                src="/marshal-logo.png"
                alt=""
                width={28}
                height={28}
                className="rounded-md"
              />
              marshal
            </Link>
            <nav className="flex items-center gap-1 text-sm">
              {navItems.map((item, i) => (
                <Link
                  key={`${item.href}-${i}`}
                  href={item.href}
                  data-tour={item.tour}
                  className={`rounded-md px-3 py-1.5 transition-colors ${
                    (item.label === "Admin"
                      ? pathname.startsWith("/admin")
                      : pathname.startsWith(item.href))
                      ? "bg-slate-800 text-white"
                      : "text-slate-400 hover:bg-slate-900 hover:text-slate-200"
                  }`}
                >
                  {item.label}
                </Link>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-3">
            <button
              ref={searchButtonRef}
              onClick={() => setSearchOpen(true)}
              aria-label="Search (Cmd+K)"
              data-testid="global-search-button"
              className="flex items-center gap-2 rounded-lg border border-slate-700 px-2.5 py-1.5 text-sm text-slate-400 hover:border-slate-500 hover:text-slate-200"
            >
              <span aria-hidden>🔍</span>
              <span className="hidden sm:inline">Search</span>
              <kbd className="hidden rounded border border-slate-700 px-1 py-0.5 text-[10px] sm:inline">⌘K</kbd>
            </button>
            <NotificationBell />
          <div className="relative">
            <button
              data-tour="profile"
              onClick={() => setMenuOpen((v) => !v)}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label="Account menu"
              className="flex items-center gap-2 rounded-full border border-slate-700 py-1 pl-1 pr-3 text-sm hover:border-slate-500"
            >
              <span className="flex h-7 w-7 items-center justify-center rounded-full bg-indigo-600 text-xs font-bold uppercase">
                {(user?.name ?? user?.email ?? "?").slice(0, 1)}
              </span>
              <span className="max-w-[140px] truncate text-slate-300">
                {user?.name ?? user?.email ?? "…"}
              </span>
              <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300">
                {previewExperience
                  ? `${previewExperience} navigation`
                  : (user?.persona ?? "…")}
              </span>
            </button>
            {menuOpen && (
              <div
                role="menu"
                className="absolute right-0 mt-2 w-44 overflow-hidden rounded-lg border border-slate-700 bg-slate-900 text-sm shadow-xl"
                onMouseLeave={() => setMenuOpen(false)}
                onKeyDown={(e) => {
                  // S15-06: keyboard users need a way OUT that mouseleave gives mice
                  if (e.key === "Escape") setMenuOpen(false);
                }}
              >
                <Link
                  href="/profile"
                  role="menuitem"
                  className="block px-4 py-2.5 hover:bg-slate-800"
                  onClick={() => setMenuOpen(false)}
                >
                  Profile & settings
                </Link>
                <button
                  onClick={handleSignOut}
                  role="menuitem"
                  className="block w-full px-4 py-2.5 text-left text-red-400 hover:bg-slate-800"
                >
                  Sign out
                </button>
              </div>
            )}
          </div>
          </div>
        </div>
      </header>
      <main id="main-content" className="mx-auto max-w-7xl px-4 py-6">{children}</main>
      {searchOpen && <SearchDialog onClose={closeSearch} />}
    </div>
  );
}
