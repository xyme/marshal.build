"use client";

/** Route-group error boundary with retry (alpha-polish R2.4). */
export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="mx-auto max-w-md py-20 text-center">
      <p className="text-4xl" aria-hidden>
        ⚠️
      </p>
      <h1 className="mt-4 text-lg font-medium">Something went wrong</h1>
      <p className="mt-2 text-sm text-slate-400">
        {error.message || "An unexpected error occurred rendering this page."}
      </p>
      <button
        onClick={reset}
        className="mt-5 rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400"
      >
        Try again
      </button>
    </div>
  );
}
