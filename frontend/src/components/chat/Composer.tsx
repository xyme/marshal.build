"use client";

import { useRef, useState } from "react";

export default function Composer({
  disabled,
  streaming,
  onSend,
  onCancel,
  onGenerate,
  generating,
  canGenerate,
  seed,
  placeholder,
}: {
  disabled: boolean;
  streaming: boolean;
  onSend: (content: string) => void;
  onCancel: () => void;
  onGenerate: () => void;
  generating: boolean;
  canGenerate: boolean;
  /** Pre-filled editable text (template starter prompt, S2-05) */
  seed?: string | null;
  /** Contextual placeholder (guided request-change mode, S4-05) */
  placeholder?: string;
}) {
  const [value, setValue] = useState(seed ?? "");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  function submit() {
    const content = value.trim();
    if (!content || disabled) return;
    onSend(content);
    setValue("");
    textareaRef.current?.focus();
  }

  return (
    <div className="border-t border-slate-800 p-3">
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          aria-label="Message marshal"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            // Enter to send, Shift+Enter for newline (FSD §4.1.6)
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          rows={Math.min(6, Math.max(1, value.split("\n").length))}
          placeholder={placeholder ?? "Describe your AI agent idea… (Enter to send, Shift+Enter for a new line)"}
          className="max-h-40 flex-1 resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2.5 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500"
        />
        {streaming ? (
          <button
            onClick={onCancel}
            className="rounded-lg border border-red-500/50 px-4 py-2.5 text-sm text-red-300 hover:bg-red-500/10"
          >
            ■ Stop
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={disabled || !value.trim()}
            className="rounded-lg bg-indigo-500 px-4 py-2.5 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            Send
          </button>
        )}
        <button
          onClick={onGenerate}
          disabled={!canGenerate || generating || streaming}
          title="Generate the full spec set (requirements, design, tasks) from this conversation"
          className="rounded-lg border border-emerald-500/50 px-4 py-2.5 text-sm text-emerald-300 hover:bg-emerald-500/10 disabled:opacity-40"
        >
          {generating ? "Generating…" : "📝 Generate spec"}
        </button>
      </div>
    </div>
  );
}
