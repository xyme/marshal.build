"use client";

import { createContext, useCallback, useContext, useRef, useState } from "react";

interface ConfirmOptions {
  title: string;
  body?: string;
  confirmLabel?: string;
  destructive?: boolean;
  /** Require the user to type this string (destructive ops, §4.7.7 pattern). */
  typedConfirmation?: string;
}

type Confirm = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<Confirm>(() => Promise.resolve(false));

export function useConfirm(): Confirm {
  return useContext(ConfirmContext);
}

/** App-wide confirm dialog replacing window.confirm (alpha-polish R2.2). */
export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const [typed, setTyped] = useState("");
  const resolveRef = useRef<(value: boolean) => void>(null);

  const confirm = useCallback<Confirm>((opts) => {
    setOptions(opts);
    setTyped("");
    return new Promise<boolean>((resolve) => {
      resolveRef.current = resolve;
    });
  }, []);

  function close(result: boolean) {
    resolveRef.current?.(result);
    resolveRef.current = null;
    setOptions(null);
  }

  const typedOk = !options?.typedConfirmation || typed === options.typedConfirmation;

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {options && (
        <div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4"
          role="dialog"
          aria-modal="true"
          aria-label={options.title}
          onKeyDown={(e) => {
            if (e.key === "Escape") close(false);
          }}
        >
          <div
            className={`w-full max-w-md rounded-xl border bg-slate-900 p-6 ${
              options.destructive ? "border-red-900/60" : "border-slate-700"
            }`}
          >
            <h2 className={`text-lg font-medium ${options.destructive ? "text-red-300" : ""}`}>
              {options.title}
            </h2>
            {options.body && <p className="mt-2 text-sm text-slate-400">{options.body}</p>}
            {options.typedConfirmation && (
              <input
                autoFocus
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                placeholder={options.typedConfirmation}
                aria-label={`Type ${options.typedConfirmation} to confirm`}
                className="mt-4 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-red-500"
              />
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                autoFocus={!options.typedConfirmation}
                onClick={() => close(false)}
                className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                onClick={() => close(true)}
                disabled={!typedOk}
                className={`rounded-lg px-4 py-2 text-sm font-medium text-white disabled:opacity-40 ${
                  options.destructive
                    ? "bg-red-600 hover:bg-red-500"
                    : "bg-indigo-500 hover:bg-indigo-400"
                }`}
              >
                {options.confirmLabel ?? "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}
