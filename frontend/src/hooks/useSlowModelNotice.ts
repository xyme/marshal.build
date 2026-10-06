"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** How long a message may be in flight with no first token before the
 * composer shows the "slow model" status line. The backend retries a stalled
 * stream itself (FIRST_TOKEN_TIMEOUT_S = 45 s); this only tells the user why
 * nothing is happening yet. */
export const SLOW_MODEL_NOTICE_MS = 20_000;

/** Watchdog for the chat streaming path, shared by the chat page and the
 * studio so the two surfaces cannot drift.
 *
 * `arm()` when a message is sent; `disarm()` on the first delta, on
 * done/error, on cancel, and in the send() finally block. `slow` flips true
 * only if the timer runs out while armed.
 */
export function useSlowModelNotice(delayMs: number = SLOW_MODEL_NOTICE_MS): {
  slow: boolean;
  arm: () => void;
  disarm: () => void;
} {
  const [slow, setSlow] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clear = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const arm = useCallback(() => {
    clear();
    setSlow(false);
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      setSlow(true);
    }, delayMs);
  }, [clear, delayMs]);

  const disarm = useCallback(() => {
    clear();
    setSlow(false);
  }, [clear]);

  useEffect(() => clear, [clear]);

  return { slow, arm, disarm };
}
