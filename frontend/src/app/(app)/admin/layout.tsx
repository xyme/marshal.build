"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import BuildInfoBadge from "@/components/shell/BuildInfoBadge";
import { useUser } from "@/components/user-context";

const SECTIONS = [
  { href: "/admin/templates", label: "Templates" },
  { href: "/admin/marketplace", label: "Marketplace" },
  { href: "/admin/users", label: "Users" },
  { href: "/admin/teams", label: "Teams" },
  { href: "/admin/risk", label: "Risk" },
  { href: "/admin/costs", label: "Costs" },
  { href: "/admin/analytics", label: "Analytics" },
  { href: "/admin/models", label: "Model Controls" },
  { href: "/admin/integrations", label: "Integrations" },
  { href: "/admin/audit", label: "Audit Logs" },
];

/** Admin console frame: role guard + section tabs (S3-04/05/08). */
export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { user } = useUser();
  const pathname = usePathname();
  // View-only admin visibility (owner decision, 4 Sep 2026): flagged beta
  // viewers see every surface; the backend refuses their non-GET requests.
  const readonly = !!user?.admin_readonly && user.role !== "admin";

  if (user && user.role !== "admin" && !user.admin_readonly) {
    return (
      <p className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-red-300">
        Admin role required.
      </p>
    );
  }

  return (
    <div className="space-y-6">
      {readonly && (
        <p className="rounded-xl border border-amber-400/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-100/90">
          <strong className="text-amber-300">View-only admin access.</strong> You can
          inspect every surface, but actions are disabled and the platform refuses
          changes from this account. Reads of the audit log are themselves audited.
        </p>
      )}
      <nav className="flex gap-1 border-b border-slate-800 text-sm">
        {SECTIONS.map((s) => (
          <Link
            key={s.href}
            href={s.href}
            className={`rounded-t-lg px-4 py-2 ${
              pathname.startsWith(s.href)
                ? "border border-b-0 border-slate-800 bg-slate-900/40 text-white"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {s.label}
          </Link>
        ))}
      </nav>
      {children}
      {/* S16-06: the admin footer always answers "what exactly is running?" */}
      <footer className="border-t border-slate-800 pt-3">
        <BuildInfoBadge detailed />
      </footer>
    </div>
  );
}
