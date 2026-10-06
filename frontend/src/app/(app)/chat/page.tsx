"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import HelpLink from "@/components/shell/HelpLink";
import Composer from "@/components/chat/Composer";
import ContextBar from "@/components/chat/ContextBar";
import GuidedWizard from "@/components/chat/GuidedWizard";
import MessageThread from "@/components/chat/MessageThread";
import NewSessionModal from "@/components/chat/NewSessionModal";
import SessionSidebar from "@/components/chat/SessionSidebar";
import SlowModelNotice from "@/components/chat/SlowModelNotice";
import SpecPanel from "@/components/chat/SpecPanel";
import SubstrateBar from "@/components/chat/SubstrateBar";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useUser } from "@/components/user-context";
import { useSessionSpec } from "@/hooks/useSessionSpec";
import { useSlowModelNotice } from "@/hooks/useSlowModelNotice";
import { api } from "@/lib/api";
import { fetchSSE } from "@/lib/sse";
import type { ChatMessage, ChatSession, DocType, MetaFeatures, TemplateUser } from "@/lib/types";

export default function ChatPage() {
  const { user } = useUser();
  const router = useRouter();
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [features, setFeatures] = useState<MetaFeatures | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [showNewSession, setShowNewSession] = useState(false);
  const [composerSeed, setComposerSeed] = useState<string | null>(null);
  const [templateName, setTemplateName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const active = sessions.find((s) => s.id === activeId) ?? null;

  const loadSessions = useCallback(async () => {
    const list = await api<ChatSession[]>("/v1/chat/sessions");
    setSessions(list);
    return list;
  }, []);

  // Spec set + generation SSE lifecycle — shared with the studio (S18-02).
  const { spec, generation, generating, startGeneration: startGen, saveSpec: saveSpecHook } =
    useSessionSpec(activeId, {
      onSessionTouched: () => {
        loadSessions().catch(() => {});
      },
    });
  // "Slow model" status line: armed on send, cleared on the first token.
  const slowNotice = useSlowModelNotice();

  useEffect(() => {
    // Deep link from the studio (R1.3): /chat?session=<id>
    const wanted = new URLSearchParams(window.location.search).get("session");
    loadSessions()
      .then((list) => {
        if (wanted && list.some((s) => s.id === wanted)) setActiveId(wanted);
        else if (list.length > 0) setActiveId((prev) => prev ?? list[0].id);
      })
      .catch((e) => setError(e.message));
    api<MetaFeatures>("/v1/meta/features").then(setFeatures).catch(() => {});
  }, [loadSessions]);

  useEffect(() => {
    if (!activeId) {
      setMessages([]);
      return;
    }
    setError(null);
    api<ChatMessage[]>(`/v1/chat/sessions/${activeId}/messages`)
      .then(setMessages)
      .catch((e) => setError(e.message));
  }, [activeId]);

  // Template name for the context bar
  useEffect(() => {
    if (!active?.template_id) {
      setTemplateName(null);
      return;
    }
    api<TemplateUser>(`/v1/templates/${active.template_id}`)
      .then((t) => setTemplateName(t.name))
      .catch(() => setTemplateName(null));
  }, [active?.template_id]);

  async function createSession(
    templateId: string | null,
    starterPrompt: string | null,
    mode?: "freeform" | "guided",
    surface?: "chat" | "studio"
  ) {
    try {
      const session = await api<ChatSession>("/v1/chat/sessions", {
        method: "POST",
        body: JSON.stringify({ template_id: templateId, mode: mode ?? null }),
      });
      if (surface === "studio") {
        try {
          localStorage.setItem("build-surface", "studio");
        } catch {
          /* private mode */
        }
        router.push(`/studio/${session.id}`);
        return;
      }
      setSessions((prev) => [session, ...prev]);
      setComposerSeed(starterPrompt);
      setActiveId(session.id);
      setShowNewSession(false);
    } catch (e) {
      setError((e as Error).message);
      setShowNewSession(false);
    }
  }

  const updateSession = useCallback((updated: ChatSession) => {
    setSessions((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
  }, []);

  async function deleteSession(id: string) {
    await api(`/v1/chat/sessions/${id}`, { method: "DELETE" });
    setSessions((prev) => prev.filter((s) => s.id !== id));
    if (activeId === id) setActiveId(null);
  }

  async function renameSession(id: string, title: string) {
    const updated = await api<ChatSession>(`/v1/chat/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
    setSessions((prev) => prev.map((s) => (s.id === id ? updated : s)));
  }

  async function send(content: string) {
    if (!activeId || streaming) return;
    setError(null);
    setStreaming(true);
    slowNotice.arm();
    setComposerSeed(null);
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
        `/api/backend/v1/chat/sessions/${activeId}/messages`,
        { body: { content }, signal: controller.signal },
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
      loadSessions().catch(() => {});
    }
  }

  function cancelStream() {
    abortRef.current?.abort();
    slowNotice.disarm();
    setStreaming(false);
    setMessages((prev) => prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)));
  }

  async function startGeneration(docType?: DocType) {
    setError(null);
    try {
      await startGen(docType);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function saveSpec() {
    await saveSpecHook();
  }

  const approxTokens = Math.round(messages.reduce((n, m) => n + m.content.length, 0) / 4);

  return (
    <div className="flex h-[calc(100vh-8rem)] gap-4">
      <SessionSidebar
        sessions={sessions}
        activeId={activeId}
        onSelect={setActiveId}
        onNew={() => setShowNewSession(true)}
        onDelete={deleteSession}
        onRename={renameSession}
      />

      <div className="flex min-w-0 flex-1 flex-col rounded-xl border border-slate-800 bg-slate-900/40">
        {active && active.mode === "guided" && active.guided_state && active.guided_state.step !== "done" ? (
          <GuidedWizard
            session={active}
            generation={generation}
            spec={spec}
            canSwitchFreeform={user?.persona === "power" || user?.role === "admin"}
            onSessionUpdate={updateSession}
            onGenerate={() => startGeneration()}
            onSave={saveSpec}
            studioHref={features?.studio ? `/studio/${active.id}` : null}
          />
        ) : active ? (
          <>
            {features?.studio && (
              <div className="flex justify-end border-b border-slate-800/60 px-4 py-1.5">
                <Link
                  href={`/studio/${active.id}`}
                  onClick={() => {
                    try {
                      localStorage.setItem("build-surface", "studio");
                    } catch {
                      /* private mode */
                    }
                  }}
                  className="text-xs text-indigo-300 hover:text-indigo-200"
                >
                  Open in Studio ↗
                </Link>
              </div>
            )}
            <SubstrateBar
              session={active}
              onUpdated={(s) =>
                setSessions((prev) => prev.map((x) => (x.id === s.id ? { ...x, ...s } : x)))
              }
            />
            <MessageThread messages={messages} />
            {error && (
              <div className="mx-4 mb-2 flex items-center justify-between gap-3 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                <span>{error}</span>
                <HelpLink doc="troubleshooting" label="Troubleshooting" />
              </div>
            )}
            <Composer
              key={`${active.id}-${composerSeed ?? ""}`}
              seed={active.mode === "guided" ? null : composerSeed}
              placeholder={
                active.mode === "guided"
                  ? "Request a change to your spec — marshal will help, then Regenerate applies it…"
                  : undefined
              }
              disabled={streaming}
              onSend={send}
              onCancel={cancelStream}
              streaming={streaming}
              onGenerate={() => startGeneration()}
              generating={!!generating}
              canGenerate={messages.length > 0}
            />
            <SlowModelNotice show={streaming && slowNotice.slow} />
            <ContextBar
              modelId={active.model_id}
              approxTokens={approxTokens}
              sessionCreatedAt={active.created_at}
              showModel={user?.persona === "power"}
              templateName={templateName}
            />
          </>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center text-slate-400">
            <p className="text-4xl">💬</p>
            <p className="mt-3">
              {user?.persona === "power"
                ? "Start a session and describe the agent you want to spec."
                : "Start a chat and tell marshal about your agent idea."}
            </p>
            <button
              onClick={() => setShowNewSession(true)}
              className="mt-4 rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-400"
            >
              + New session
            </button>
          </div>
        )}
      </div>

      <SpecPanel
        spec={spec}
        generation={generation}
        generating={!!generating}
        onRegenerateDoc={(docType) => startGeneration(docType)}
        onSave={saveSpec}
        sessionStatus={active?.status}
        projectId={active?.project_id ?? null}
      />

      {showNewSession && (
        <NewSessionModal
          onClose={() => setShowNewSession(false)}
          onCreate={createSession}
          canChooseMode={user?.persona === "power" || user?.role === "admin"}
          studioAvailable={!!features?.studio}
        />
      )}
    </div>
  );
}
