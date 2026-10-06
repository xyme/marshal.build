"use client";

import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "@/lib/types";

function modelLabel(modelId?: string | null): string | null {
  if (!modelId) return null;
  if (modelId.includes("haiku")) return "Haiku";
  if (modelId.includes("sonnet")) return "Sonnet";
  if (modelId.includes("opus")) return "Opus";
  return modelId.split(".").pop() ?? modelId;
}

export default function MessageThread({ messages }: { messages: ChatMessage[] }) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    // S15-06: a scrollable region needs keyboard access (axe:
    // scrollable-region-focusable) — without tabIndex, keyboard users cannot
    // scroll the transcript. Labelled as a log so screen readers announce it.
    <div
      className="flex-1 space-y-4 overflow-y-auto p-4"
      tabIndex={0}
      role="log"
      aria-label="Conversation"
    >
      {messages.length === 0 && (
        <div className="pt-16 text-center text-sm text-slate-400">
          Tell marshal what you want to build — e.g. &ldquo;A chatbot that answers
          questions from our HR policy documents.&rdquo;
        </div>
      )}
      {messages.map((message) => (
        <div
          key={message.sk}
          className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}
        >
          <div
            className={`group relative max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
              message.role === "user"
                ? "bg-indigo-600 text-white"
                : "border border-slate-800 bg-slate-900 text-slate-200"
            }`}
          >
            {message.role === "assistant" ? (
              <div className="prose prose-sm prose-invert max-w-none [&_pre]:overflow-x-auto">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                {message.streaming && (
                  <span className="ml-1 inline-block h-4 w-1.5 animate-pulse bg-indigo-400 align-text-bottom" />
                )}
              </div>
            ) : (
              <p className="whitespace-pre-wrap">{message.content}</p>
            )}
            {message.error && (
              <p className="mt-2 rounded bg-red-500/15 px-2 py-1 text-xs text-red-300">
                ⚠ {message.error}
              </p>
            )}
            <div className="mt-1 flex items-center gap-2">
              {message.role === "assistant" && modelLabel(message.model_id) && (
                <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300">
                  {modelLabel(message.model_id)}
                </span>
              )}
              {!message.streaming && message.content && (
                <button
                  onClick={() => navigator.clipboard.writeText(message.content)}
                  className="invisible text-[10px] uppercase tracking-wide text-slate-400 hover:text-slate-300 group-hover:visible"
                >
                  Copy
                </button>
              )}
            </div>
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
