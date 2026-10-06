export interface SSEHandlers {
  onEvent: (event: string, data: unknown) => void;
  onError?: (error: Error) => void;
}

/**
 * SSE over fetch (POST or GET) — needed because EventSource cannot send
 * POST bodies. Parses `event:`/`data:` frames; tolerates \r\n line endings.
 */
export async function fetchSSE(
  url: string,
  {
    method = "POST",
    body,
    signal,
    headers,
  }: {
    method?: string;
    body?: unknown;
    signal?: AbortSignal;
    headers?: Record<string, string>;
  },
  handlers: SSEHandlers
): Promise<void> {
  const response = await fetch(url, {
    method,
    headers: {
      ...(body !== undefined ? { "content-type": "application/json" } : {}),
      ...(headers ?? {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const parsed = await response.json();
      if (typeof parsed?.detail === "string") detail = parsed.detail;
      else if (parsed?.detail?.message) detail = parsed.detail.message; // structured 429s (cost cap / rate limit)
      else if (parsed?.detail) detail = JSON.stringify(parsed.detail);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (!response.body) throw new Error("No response body");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const processFrame = (frame: string) => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (event === "message" && dataLines.length === 0) return; // comment/ping frame
    let data: unknown = null;
    if (dataLines.length) {
      const raw = dataLines.join("\n");
      try {
        data = JSON.parse(raw);
      } catch {
        data = raw;
      }
    }
    handlers.onEvent(event, data);
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        if (frame.trim()) processFrame(frame);
      }
    }
  } catch (error) {
    if ((error as Error).name === "AbortError") return;
    handlers.onError?.(error as Error);
    throw error;
  }
}
