"use client";

import { useEffect, useId, useState } from "react";

/** Renders a ```mermaid fence as a diagram (multi-doc spec R3.1). Lazy-loads mermaid. */
export default function MermaidBlock({ code }: { code: string }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
        const { svg: rendered } = await mermaid.render(`mmd-${id}`, code);
        if (!cancelled) setSvg(rendered);
      } catch (e) {
        if (!cancelled) setError((e as Error).message.split("\n")[0]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code, id]);

  if (error) {
    return (
      <div className="my-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
        <p className="mb-2 text-xs text-amber-400">Diagram failed to render: {error}</p>
        <pre className="overflow-x-auto text-xs text-slate-400">{code}</pre>
      </div>
    );
  }
  if (!svg) {
    return (
      <div className="my-3 flex h-32 items-center justify-center rounded-lg border border-slate-800 bg-slate-950/60 text-xs text-slate-400">
        Rendering diagram…
      </div>
    );
  }
  return (
    <div
      className="my-3 overflow-x-auto rounded-lg border border-slate-800 bg-slate-950/60 p-3 [&_svg]:mx-auto"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
