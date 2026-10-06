"use client";

import { useUser } from "@/components/user-context";

/**
 * View-only admin visibility (product decision, 4 Sep 2026).
 *
 * True when the signed-in account holds the `admin_readonly` capability but
 * not the real admin role: every admin surface renders, mutation controls are
 * disabled here, and the backend refuses non-GET admin requests with
 * `admin_read_only` regardless of what the UI shows.
 */
export function useAdminReadonly(): boolean {
  const { user } = useUser();
  return !!user?.admin_readonly && user.role !== "admin";
}
