"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { fetchSSE } from "@/lib/sse";
import type { DocType, FullSpecResponse, Generation, GenerationDoc } from "@/lib/types";

/** Session spec-set state shared by SpecPanel (chat) and the studio Spec tab (S18-02).
 *
 * Extracted verbatim from the chat page — one loader, one SSE attachment, two
 * consumers, so the surfaces cannot drift. Checkpoint sync only [R2]: the
 * generation SSE bus + explicit save/generate calls refresh the pane. We do
 * NOT re-derive the spec per chat turn — a model call per message would
 * breach the 3s chat p95 SLO and burn the caps we enforce (§7 stance).
 */
export function useSessionSpec(
  sessionId: string | null,
  opts?: {
    /** Fired when a checkpoint changed session/project state (generation done, save). */
    onSessionTouched?: () => void;
    /** Extra headers on mutating calls (the studio tags its surface, S16-04). */
    headers?: Record<string, string>;
  }
) {
  const [spec, setSpec] = useState<FullSpecResponse | null>(null);
  const [generation, setGeneration] = useState<Generation | null>(null);
  const genStreamRef = useRef<AbortController | null>(null);
  const touchedRef = useRef(opts?.onSessionTouched);
  touchedRef.current = opts?.onSessionTouched;
  const headersRef = useRef(opts?.headers);
  headersRef.current = opts?.headers;

  const generating = generation?.status === "running";

  const loadSpec = useCallback(async (sid: string) => {
    const specResp = await api<FullSpecResponse>(`/v1/chat/sessions/${sid}/spec`);
    setSpec(specResp);
  }, []);

  const attachGenerationStream = useCallback(
    (sid: string) => {
      genStreamRef.current?.abort();
      const controller = new AbortController();
      genStreamRef.current = controller;
      fetchSSE(
        `/api/backend/v1/chat/sessions/${sid}/generate-spec/stream`,
        { method: "GET", signal: controller.signal },
        {
          onEvent: (event, data) => {
            if (event === "snapshot") {
              const snap = data as { docs: Generation["docs"]; status: Generation["status"] };
              setGeneration((prev) =>
                prev ? { ...prev, docs: snap.docs, status: snap.status } : prev
              );
            } else if (event.startsWith("doc_")) {
              const payload = data as {
                doc: DocType;
                status?: GenerationDoc["status"];
                spec_id?: string;
                version?: number;
                error?: string;
              };
              setGeneration((prev) =>
                prev
                  ? {
                      ...prev,
                      docs: prev.docs.map((d) =>
                        d.type === payload.doc
                          ? {
                              ...d,
                              status: payload.status ?? d.status,
                              spec_id: payload.spec_id ?? d.spec_id,
                              version: payload.version ?? d.version,
                              error: payload.error ?? d.error,
                            }
                          : d
                      ),
                    }
                  : prev
              );
              if (payload.status === "done") loadSpec(sid).catch(() => {});
            } else if (event === "done") {
              const done = data as { status?: Generation["status"] };
              setGeneration((prev) =>
                prev ? { ...prev, status: done.status ?? "done" } : prev
              );
              loadSpec(sid).catch(() => {});
              touchedRef.current?.();
            }
          },
        }
      ).catch(() => {
        /* stream closed; `latest` re-syncs on next visit */
      });
    },
    [loadSpec]
  );

  /** Initial + per-session load; re-attaches to an in-flight generation. */
  const refresh = useCallback(async () => {
    if (!sessionId) return;
    const latestGen = await api<Generation | null>(
      `/v1/chat/sessions/${sessionId}/generate-spec/latest`
    );
    setGeneration(latestGen);
    await loadSpec(sessionId);
    if (latestGen?.status === "running") attachGenerationStream(sessionId);
  }, [sessionId, loadSpec, attachGenerationStream]);

  useEffect(() => {
    if (!sessionId) {
      setSpec(null);
      setGeneration(null);
      return;
    }
    refresh().catch(() => {});
    return () => genStreamRef.current?.abort();
  }, [sessionId, refresh]);

  /** POST generate (full set, or one doc) then follow its SSE stream. */
  const startGeneration = useCallback(
    async (docType?: DocType) => {
      if (!sessionId || generation?.status === "running") return;
      const path = docType
        ? `/v1/chat/sessions/${sessionId}/generate-spec/${docType}`
        : `/v1/chat/sessions/${sessionId}/generate-spec`;
      const gen = await api<Generation>(path, { method: "POST", headers: headersRef.current });
      setGeneration(gen);
      attachGenerationStream(sessionId);
    },
    [sessionId, generation?.status, attachGenerationStream]
  );

  const saveSpec = useCallback(async () => {
    if (!sessionId) return;
    const specResp = await api<FullSpecResponse>(`/v1/chat/sessions/${sessionId}/spec/save`, {
      method: "POST",
      headers: headersRef.current,
    });
    setSpec(specResp);
    touchedRef.current?.();
  }, [sessionId]);

  return { spec, generation, generating: !!generating, refresh, startGeneration, saveSpec };
}
