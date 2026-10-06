"use client";

import { useState } from "react";
import { signIn } from "next-auth/react";

/**
 * Client-driven sign-in: next-auth/react fetches the CSRF token itself and
 * POSTs to the provider signin endpoint — robust behind CloudFront/ALB where
 * server-action-based sign-in proved fragile.
 */
export default function SignInButton({ callbackUrl }: { callbackUrl?: string }) {
  const [busy, setBusy] = useState(false);

  return (
    <button
      type="button"
      disabled={busy}
      onClick={() => {
        setBusy(true);
        signIn("cognito", { callbackUrl: callbackUrl ?? "/home" });
      }}
      className="mt-8 w-full rounded-lg bg-indigo-500 px-4 py-2.5 font-medium text-white transition hover:bg-indigo-400 disabled:opacity-60"
    >
      {busy ? "Redirecting…" : "Sign in with your organization"}
    </button>
  );
}
