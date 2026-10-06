export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/**
 * Error payloads must surface as sentences, never raw JSON (S15-05).
 * Handles the three shapes the backend produces:
 *  - plain strings,
 *  - structured governance refusals `{code, message | detail, ...}`
 *    (cost caps, rate limits, the admin-MFA gate),
 *  - FastAPI validation errors: an array of `{loc, msg}` objects.
 */
function humanizeDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((item) => {
        if (typeof item !== "object" || item === null) return String(item);
        const { loc, msg } = item as { loc?: unknown[]; msg?: string };
        const field = Array.isArray(loc) ? String(loc[loc.length - 1]) : "";
        return field && msg ? `${field}: ${msg}` : (msg ?? "");
      })
      .filter(Boolean);
    if (msgs.length) return msgs.join(" · ");
  }
  if (typeof detail === "object" && detail !== null) {
    const d = detail as { message?: string; detail?: string };
    if (typeof d.message === "string") return d.message;
    if (typeof d.detail === "string") return d.detail;
  }
  return JSON.stringify(detail);
}

/** JSON fetch against the backend proxy. Path starts with /v1/... */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/backend${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (body?.detail) detail = humanizeDetail(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
