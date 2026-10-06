"use client";

import Link from "next/link";
import { notFound, useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import Composer from "@/components/chat/Composer";
import ContextBar from "@/components/chat/ContextBar";
import MessageThread from "@/components/chat/MessageThread";
import SlowModelNotice from "@/components/chat/SlowModelNotice";
import HelpLink from "@/components/shell/HelpLink";
import ArtifactColumn from "@/components/studio/ArtifactColumn";
import ConfigRail from "@/components/studio/ConfigRail";
import { useSessionSpec } from "@/hooks/useSessionSpec";
import { useSlowModelNotice } from "@/hooks/useSlowModelNotice";
import { api } from "@/lib/api";
import { fetchSSE } from "@/lib/sse";
import type {
  ChatMessage,
  ChatSession,
  EffectiveParamsResp,
  MetaFeatures,
  TemplateUser,
} from "@/lib/types";

const SURFACE_HEADERS = { "x-marshal-surface": "studio" };

/** Studio build workspace (S18): chat beside the evolving artifact, with the
 * session's model/params visible and adjustable. Dark-launched behind
 * `studio_enabled` — flag off, non-admins get the 404 page (R1.2).
 * The wizard is untouched [D17]: guided-in-progress sessions link back to /chat.
 */
export default function StudioPage() {
  const { id } = useParams<{ id: string }>();
  const [features, setFeatures] = useState<MetaFeatures | null>(null);
  const [session, setSession] = useState<ChatSession | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [eff, setEff] = useState<EffectiveParamsResp | null>(null);
  const [templateName, setTemplateName] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [railBusy, setRailBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const { spec, generation, generating, refresh, startGeneration, saveSpec } = useSessionSpec(
    id ?? null,
    { headers: SURFACE_HEADERS }
  );
  // "Slow model" status line: armed on send, cleared on the first token.
  const slowNotice = useSlowModelNotice();

  // Surface preference (R1.3): landing here remembers the choice.
  useEffect(() => {
    try {
      localStorage.setItem("build-surface", "studio");
    } catch {
      /* private mode */
    }
  }, []);

  useEffect(() => {
    api<MetaFeatures>("/v1/meta/features").then(setFeatures).catch(() => setGone(true));
  }, []);

  const loadEffective = useCallback(() => {
    if (!id) return;
    api<EffectiveParamsResp>(`/v1/chat/sessions/${id}/effective-params`)
      .then(setEff)
      .catch(() => {});
  }, [id]);

  useEffect(() => {
    if (!id) return;
    Promise.all([
      api<ChatSession>(`/v1/chat/sessions/${id}`),
      api<ChatMessage[]>(`/v1/chat/sessions/${id}/messages`),
    ])
      .then(([s, msgs]) => {
        setSession(s);
        setMessages(msgs);
      })
      .catch(() => setGone(true));
    loadEffective();
  }, [id, loadEffective]);

  useEffect(() => {
    if (!session?.template_id) {
      setTemplateName(null);
      return;
    }
    api<TemplateUser>(`/v1/templates/${session.template_id}`)
      .then((t) => setTemplateName(t.name))
      .catch(() => setTemplateName(null));
  }, [session?.template_id]);

  // Gate: flag off + non-admin, business persona, or inaccessible session → 404
  if (gone || (features && !features.studio)) notFound();

  if (!features || !session) {
    return <p className="text-slate-400">Loading studio…</p>;
  }

  // Guided sessions stay in the wizard until completion [D17]
  if (session.mode === "guided" && session.guided_state && session.guided_state.step !== "done") {
    return (
      <div className="mx-auto max-w-md rounded-xl border border-slate-800 bg-slate-900/40 p-6 text-center">
        <p className="text-sm text-slate-300">
          This session is mid-wizard. Finish the guided steps in Chat — the studio opens
          once your spec exists.
        </p>
        <Link
          href={`/chat?session=${session.id}`}
          className="mt-4 inline-block rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400"
        >
          Open in Chat
        </Link>
      </div>
    );
  }

  async function send(content: string) {
    if (!id || streaming) return;
    setError(null);
    setStreaming(true);
    slowNotice.arm();
    const userMsg: ChatMessage = { role: "user", content, sk: `local-${Date.now()}` };
    const assistantSk = `local-a-${Date.now()}`;
    setMessages((prev) => [
      ...prev,
      userMsg,
      { role: "assistant", content: "", sk: assistantSk, streaming: true },
    ]);
    const controller = new AbortController();
    abortRef.current = controller;
    const appendDelta = (text: string) =>
      setMessages((prev) =>
        prev.map((m) => (m.sk === assistantSk ? { ...m, content: m.content + text } : m))
      );
    try {
      await fetchSSE(
        `/api/backend/v1/chat/sessions/${id}/messages`,
        { body: { content }, signal: controller.signal, headers: SURFACE_HEADERS },
        {
          onEvent: (event, data) => {
            if (event === "delta") {
              slowNotice.disarm();
              appendDelta((data as { text: string }).text);
            } else if (event === "done") {
              slowNotice.disarm();
              const meta = data as { model_id: string };
              setMessages((prev) =>
                prev.map((m) =>
                  m.sk === assistantSk ? { ...m, streaming: false, model_id: meta.model_id } : m
                )
              );
            } else if (event === "error") {
              slowNotice.disarm();
              const err = data as { message: string };
              setMessages((prev) =>
                prev.map((m) =>
                  m.sk === assistantSk ? { ...m, streaming: false, error: err.message } : m
                )
              );
            }
          },
        }
      );
    } catch (e) {
      slowNotice.disarm();
      const message = (e as Error).message;
      setMessages((prev) =>
        prev.map((m) => (m.sk === assistantSk ? { ...m, streaming: false, error: message } : m))
      );
    } finally {
      slowNotice.disarm();
      setStreaming(false);
      abortRef.current = null;
    }
  }

  function cancelStream() {
    abortRef.current?.abort();
    slowNotice.disarm();
    setStreaming(false);
    setMessages((prev) => prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)));
  }

  async function patchRail(fields: { model_id?: string; temperature?: number; max_tokens?: number }) {
    if (!id) return;
    setRailBusy(true);
    setError(null);
    try {
      const updated = await api<ChatSession>(`/v1/chat/sessions/${id}`, {
        method: "PATCH",
        body: JSON.stringify(fields),
      });
      setSession(updated); // ContextBar + rail agree immediately (R3.4)
      loadEffective();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRailBusy(false);
    }
  }

  // Staleness inputs (R2.3): newest spec doc vs messages after it, locally.
  const specAt = (["requirements", "design", "tasks"] as const)
    .map((t) => spec?.[t]?.latest?.created_at)
    .filter(Boolean)
    .sort()
    .pop() as string | null;
  const specTs = specAt ? Date.parse(specAt) : null;
  const messagesSince = specTs
    ? messages.filter((m) => {
        const n = Number(m.sk.split("#")[0]);
        return Number.isNaN(n) ? true : n > specTs; // local (unsynced) msgs are newer
      }).length
    : 0;

  const approxTokens = Math.round(messages.reduce((n, m) => n + m.content.length, 0) / 4);

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col gap-3">
      {features.studio_dark && (
        <div className="flex items-center justify-between rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-2 text-xs text-amber-300">
          <span>
            🌘 Dark launch — the studio flag is off; only admins can see this page.
            Flip <code>studio_enabled</code> at Admin → Model Controls to open it to power users.
          </span>
          <HelpLink doc="admin" label="About flags" />
        </div>
      )}

      <div className="flex items-center justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <h1 className="truncate text-lg font-semibold tracking-tight">{session.title}</h1>
          <span className="rounded bg-indigo-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300">
            Studio
          </span>
          <HelpLink doc="studio" label="How the studio works" />
        </div>
        <Link
          href={`/chat?session=${session.id}`}
          onClick={() => {
            try {
              localStorage.setItem("build-surface", "chat");
            } catch {
              /* private mode */
            }
          }}
          className="shrink-0 rounded-lg border border-slate-700 px-3 py-1.5 text-xs hover:border-slate-500"
        >
          Open in Chat
        </Link>
      </div>

      {/* Stacked below 1024px (R1.4); three columns from lg up */}
      <div className="flex min-h-0 flex-1 flex-col gap-3 lg:flex-row">
        <div className="flex min-h-0 min-w-0 flex-1 flex-col rounded-xl border border-slate-800 bg-slate-900/40 lg:basis-[44%]">
          <MessageThread messages={messages} />
          {error && (
            <div className="mx-4 mb-2 flex items-center justify-between gap-3 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
              <span>{error}</span>
              <HelpLink doc="troubleshooting" label="Troubleshooting" />
            </div>
          )}
          <Composer
            disabled={streaming}
            onSend={send}
            onCancel={cancelStream}
            streaming={streaming}
            onGenerate={() => startGeneration().catch((e) => setError((e as Error).message))}
            generating={generating}
            canGenerate={messages.length > 0}
          />
          <SlowModelNotice show={streaming && slowNotice.slow} />
          <ContextBar
            modelId={eff?.effective.model_id ?? session.model_id}
            approxTokens={approxTokens}
            sessionCreatedAt={session.created_at}
            showModel
            templateName={templateName}
          />
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-3 lg:basis-[56%] lg:flex-row">
          <div className="flex min-h-0 flex-1 flex-col lg:basis-[62%]">
            <ArtifactColumn
              sessionId={session.id}
              projectId={session.project_id}
              spec={spec}
              generation={generation}
              generating={generating}
              onRegenerateDoc={(docType) =>
                startGeneration(docType).catch((e) => setError((e as Error).message))
              }
              onSave={() => saveSpec().catch((e) => setError((e as Error).message))}
              sessionStatus={session.status}
              staleness={{ specAt: specAt ?? null, messagesSince }}
              onRefresh={() => {
                refresh().catch(() => {});
                api<ChatSession>(`/v1/chat/sessions/${id}`).then(setSession).catch(() => {});
              }}
            />
          </div>
          <div className="lg:basis-[38%]">
            <ConfigRail eff={eff} busy={railBusy} onChange={patchRail} />
          </div>
        </div>
      </div>
    </div>
  );
}
