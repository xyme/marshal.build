import { NextResponse } from "next/server";
import { auth } from "@/auth";

/**
 * Route protection with EXPLICIT path gating inside the handler.
 *
 * Why not rely on `config.matcher` alone: the matcher export was silently
 * dropped in the production (turbopack) build, so middleware ran on every
 * path — including /api/auth/* — which broke the OAuth callback and looped
 * users back to the landing page. Gating in code is bundler-proof; the
 * matcher below remains as an optimization where it is honored.
 */

// Pre-Beta hardening: EVERY authenticated surface belongs here — this list
// drifted as surfaces shipped (/studio, /admin, /marketplace, /docs, /reviews,
// /notifications rendered shells unauthenticated; backend RBAC held, but the
// shell should not render). Update this list when adding a routed surface.
const PROTECTED_PREFIXES = [
  "/home",
  "/chat",
  "/projects",
  "/profile",
  "/onboarding",
  "/studio",
  "/admin",
  "/marketplace",
  "/docs",
  "/reviews",
  "/notifications",
];

export default auth((request) => {
  const { pathname } = request.nextUrl;
  const isProtected = PROTECTED_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`)
  );
  if (isProtected && !request.auth?.user) {
    const signInUrl = new URL("/", request.nextUrl.origin);
    signInUrl.searchParams.set("callbackUrl", request.nextUrl.href);
    return NextResponse.redirect(signInUrl);
  }
  return NextResponse.next();
});

export const config = {
  matcher: [
    "/home/:path*",
    "/chat/:path*",
    "/projects/:path*",
    "/profile/:path*",
    "/onboarding/:path*",
    "/studio/:path*",
    "/admin/:path*",
    "/marketplace/:path*",
    "/docs/:path*",
    "/reviews/:path*",
    "/notifications/:path*",
  ],
};
