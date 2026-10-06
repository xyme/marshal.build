/** Status line under the composer while a message is in flight with no first
 * token yet (see useSlowModelNotice). The `role="status"` live region stays
 * mounted permanently and only its TEXT toggles: assistive technology
 * announces changes inside an existing live region, but frequently misses a
 * region that is inserted together with its content. Empty when idle, so it
 * takes no space and reads as nothing. */
export default function SlowModelNotice({ show }: { show: boolean }) {
  return (
    <p
      role="status"
      aria-live="polite"
      className={show ? "mx-4 mb-2 text-xs text-amber-300" : "sr-only"}
    >
      {show
        ? "The model is slow to respond — still waiting (the platform retries automatically)…"
        : ""}
    </p>
  );
}
