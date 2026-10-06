"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import MermaidBlock from "@/components/markdown/MermaidBlock";

/** Shared spec renderer: GFM markdown + mermaid fence support. */
export default function SpecMarkdown({ content }: { content: string }) {
  return (
    <div className="prose prose-sm prose-invert max-w-none [&_pre]:overflow-x-auto">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code(props) {
            const { className, children } = props;
            const language = /language-(\w+)/.exec(className ?? "")?.[1];
            if (language === "mermaid") {
              return <MermaidBlock code={String(children).trim()} />;
            }
            return <code className={className}>{children}</code>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
