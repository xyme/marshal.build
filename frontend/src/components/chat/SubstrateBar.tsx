"use client";

import { useRef, useState } from "react";
import { api } from "@/lib/api";
import type { ChatSession } from "@/lib/types";
import { toast } from "sonner";

const MAX_CHARS = 200_000;
const MAX_BINARY_BYTES = 1_400_000; // pdf-docx-ingestion: server cap is 1.5MB
const ACCEPT = ".md,.txt,.json,.yaml,.yml,.pdf,.docx";

/** Brownfield substrate attach/replace/remove (brownfield-substrate spec §5).
 *  Paste or read a single text file CLIENT-SIDE; only presence + source ride
 *  the session view — the content goes up once and lives server-side. */
export default function SubstrateBar({
  session,
  onUpdated,
}: {
  session: ChatSession;
  onUpdated: (s: ChatSession) => void;
}) {
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState("");
  const [source, setSource] = useState("");
  // pdf-docx-ingestion: binary documents have no client-side preview — they
  // hold as a PENDING chip until the deliberate Attach click (never silent).
  const [pendingDoc, setPendingDoc] = useState<{ b64: string; name: string; kb: number } | null>(
    null
  );
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function onFile(file: File) {
    if (/\.(pdf|docx)$/i.test(file.name)) {
      if (file.size > MAX_BINARY_BYTES) {
        toast.error("Documents are limited to 1.4MB.");
        return;
      }
      const b64 = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const url = String(reader.result ?? "");
          resolve(url.slice(url.indexOf(",") + 1)); // strip data: prefix
        };
        reader.onerror = () => reject(new Error("Could not read the file."));
        reader.readAsDataURL(file);
      }).catch((e) => {
        toast.error((e as Error).message);
        return null;
      });
      if (!b64) return;
      setContent("");
      setPendingDoc({ b64, name: file.name, kb: Math.max(1, Math.round(file.size / 1024)) });
      if (!source) setSource(file.name.slice(0, 120));
      return;
    }
    if (file.size > MAX_CHARS) {
      toast.error("Substrate is limited to 200KB — trim the export and retry.");
      return;
    }
    setPendingDoc(null);
    setContent(await file.text());
    if (!source) setSource(file.name.slice(0, 120));
  }

  async function attach() {
    const trimmed = content.trim();
    if (!trimmed && !pendingDoc) return;
    if (trimmed.length > MAX_CHARS) {
      toast.error("Substrate is limited to 200KB — trim the content and retry.");
      return;
    }
    setBusy(true);
    try {
      const payload = pendingDoc
        ? { document_b64: pendingDoc.b64, source: source.trim() || pendingDoc.name.slice(0, 120) }
        : { content: trimmed, source: source.trim() || null };
      const updated = await api<ChatSession>(`/v1/chat/sessions/${session.id}/substrate`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      onUpdated(updated);
      setOpen(false);
      setContent("");
      setPendingDoc(null);
      setSource("");
      toast.success("Substrate attached — marshal now treats it as your existing agent.");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      const updated = await api<ChatSession>(`/v1/chat/sessions/${session.id}/substrate`, {
        method: "DELETE",
      });
      onUpdated(updated);
      toast.success("Substrate removed.");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const sizeLabel = session.substrate_size
    ? `${Math.max(1, Math.round(session.substrate_size / 1024))} KB`
    : "";

  return (
    <div className="border-b border-slate-800/60 px-4 py-1.5 text-xs">
      {session.has_substrate && !open ? (
        <div className="flex items-center gap-2">
          <span
            className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-0.5 text-emerald-300"
            title="This session is grounded on your existing agent's definition"
          >
            Substrate: {session.substrate_source || "attached"} · {sizeLabel}
            <button
              onClick={remove}
              disabled={busy}
              aria-label="Remove substrate"
              className="ml-0.5 text-emerald-300/80 hover:text-emerald-100 disabled:opacity-40"
            >
              ×
            </button>
          </span>
          <button
            onClick={() => setOpen(true)}
            className="text-slate-400 hover:text-slate-200"
          >
            Replace
          </button>
        </div>
      ) : !open ? (
        <button
          onClick={() => setOpen(true)}
          className="text-indigo-300 hover:text-indigo-200"
          title="Migrating an existing agent? Paste its prompt, tools and config"
        >
          + Attach agent substrate (brownfield)
        </button>
      ) : (
        <div className="space-y-2 py-1.5">
          <p className="text-slate-400">
            Paste your existing agent&apos;s definition — system prompt, tool list,
            config, sample transcripts — or read it from a file (text, PDF or Word;
            text is extracted server-side). marshal treats it as current-state truth
            and helps you spec a governed rebuild.
          </p>
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={6}
            placeholder="Paste the agent definition here…"
            aria-label="Agent substrate content"
            className="block w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs outline-none focus:border-indigo-500"
          />
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={source}
              onChange={(e) => setSource(e.target.value.slice(0, 120))}
              placeholder="Source label (optional, e.g. prod support-bot v3)"
              aria-label="Substrate source label"
              className="w-72 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 outline-none focus:border-indigo-500"
            />
            <button
              onClick={() => fileRef.current?.click()}
              className="rounded-lg border border-slate-700 px-3 py-1.5 hover:border-slate-500"
            >
              Read from file…
            </button>
            <input
              ref={fileRef}
              type="file"
              accept={ACCEPT}
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) onFile(f);
                e.target.value = "";
              }}
            />
            {pendingDoc && (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-indigo-500/40 bg-indigo-500/10 px-2.5 py-0.5 text-indigo-300">
                {pendingDoc.name} · {pendingDoc.kb} KB — ready (text extracted on attach)
                <button
                  onClick={() => setPendingDoc(null)}
                  aria-label="Discard pending document"
                  className="ml-0.5 text-indigo-300/80 hover:text-indigo-100"
                >
                  ×
                </button>
              </span>
            )}
            <span className="text-slate-500">
              {content.length > 0 && `${content.length.toLocaleString()} / 200,000 chars`}
            </span>
            <div className="ml-auto flex gap-2">
              <button
                onClick={() => {
                  setOpen(false);
                  setContent("");
                  setPendingDoc(null);
                }}
                className="rounded-lg px-3 py-1.5 text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                onClick={attach}
                disabled={
                  busy ||
                  (!pendingDoc && !content.trim()) ||
                  content.trim().length > MAX_CHARS
                }
                className="rounded-lg bg-indigo-500 px-3 py-1.5 font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
              >
                {busy ? "Attaching…" : session.has_substrate ? "Replace substrate" : "Attach"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
