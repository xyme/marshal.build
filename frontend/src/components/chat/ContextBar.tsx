"use client";

import HelpLink from "@/components/shell/HelpLink";

export default function ContextBar({
  modelId,
  approxTokens,
  sessionCreatedAt,
  showModel,
  templateName,
}: {
  modelId: string | null;
  approxTokens: number;
  sessionCreatedAt: string;
  showModel: boolean;
  templateName?: string | null;
}) {
  const minutes = Math.max(
    0,
    Math.round((Date.now() - new Date(sessionCreatedAt).getTime()) / 60000)
  );
  const tokens =
    approxTokens > 1000 ? `${(approxTokens / 1000).toFixed(1)}k` : String(approxTokens);

  return (
    <div className="flex items-center gap-4 border-t border-slate-800 px-4 py-1.5 text-[11px] text-slate-400">
      {showModel && modelId && (
        <span className="flex items-center gap-1.5">
          Model: <span className="text-slate-400">{modelId}</span>
          {modelId.startsWith("ext/") && (
            <span
              className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-sky-300"
              title="Custom endpoint hosted outside Bedrock — prompts in this session leave AWS"
            >
              External
            </span>
          )}
        </span>
      )}
      {templateName && (
        <span>
          Template: <span className="text-indigo-300">{templateName}</span>
        </span>
      )}
      <span>
        Context: <span className="text-slate-400">~{tokens} tokens</span>
      </span>
      <span>
        Session: <span className="text-slate-400">{minutes} min</span>
      </span>
      <span className="ml-auto">
        <HelpLink doc="quickstart" label="How chat becomes a spec" />
      </span>
    </div>
  );
}
