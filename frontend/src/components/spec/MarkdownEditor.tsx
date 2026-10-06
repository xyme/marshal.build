"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef } from "react";

// Monaco is ~2MB — load only when the editor is actually opened (power users)
const MonacoEditor = dynamic(() => import("@/lib/monaco-setup").then(() => import("@monaco-editor/react")), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-sm text-slate-400">
      Loading editor…
    </div>
  ),
});

export default function MarkdownEditor({
  value,
  onChange,
  onSave,
  onAutosave,
}: {
  value: string;
  onChange: (value: string) => void;
  onSave: () => void;
  /** Called at most every 30s while dirty (draft autosave, S2-02 R1.4) */
  onAutosave: () => void;
}) {
  const saveRef = useRef(onSave);
  const autosaveRef = useRef(onAutosave);
  const dirtyRef = useRef(false);
  saveRef.current = onSave;
  autosaveRef.current = onAutosave;

  useEffect(() => {
    const interval = setInterval(() => {
      if (dirtyRef.current) {
        dirtyRef.current = false;
        autosaveRef.current();
      }
    }, 30_000);
    return () => clearInterval(interval);
  }, []);

  const handleMount = useCallback((editor: unknown, monaco: unknown) => {
    const ed = editor as {
      addCommand: (keybinding: number, handler: () => void) => void;
    };
    const m = monaco as { KeyMod: { CtrlCmd: number }; KeyCode: { KeyS: number } };
    ed.addCommand(m.KeyMod.CtrlCmd | m.KeyCode.KeyS, () => saveRef.current());
  }, []);

  return (
    <MonacoEditor
      language="markdown"
      theme="vs-dark"
      value={value}
      onChange={(next) => {
        dirtyRef.current = true;
        onChange(next ?? "");
      }}
      onMount={handleMount}
      options={{
        minimap: { enabled: false },
        wordWrap: "on",
        fontSize: 13,
        lineNumbers: "on",
        scrollBeyondLastLine: false,
        padding: { top: 12 },
        renderWhitespace: "none",
      }}
    />
  );
}
