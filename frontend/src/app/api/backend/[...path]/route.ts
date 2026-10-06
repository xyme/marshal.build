import { NextRequest } from "next/server";
import { auth } from "@/auth";

/**
 * Server-side proxy to the FastAPI backend (foundation spec design):
 * browser → /api/backend/v1/... → FastAPI /api/v1/... with the user's
 * Cognito access token attached. Streams SSE bodies through untouched.
 */

export const dynamic = "force-dynamic";

const HOP_BY_HOP = new Set(["connection", "keep-alive", "transfer-encoding", "content-length"]);

async function proxy(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  // B9 service-account passthrough (integration-wave R2.2): a mat_ bearer is
  // the platform's OWN token format, validated server-side by FastAPI — the
  // proxy forwards it verbatim so machine callers use the same single ingress
  // (CloudFront → WAF → here) without a browser session. Anything else still
  // requires the session; Cognito JWTs never bypass it.
  const incomingAuth = request.headers.get("authorization") ?? "";
  const serviceToken = incomingAuth.startsWith("Bearer mat_") ? incomingAuth : null;

  const session = serviceToken ? null : await auth();
  if (!serviceToken && (!session?.accessToken || session?.tokenError)) {
    return Response.json({ detail: "Not authenticated" }, { status: 401 });
  }
  const { path } = await params;
  const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";
  const target = `${backendUrl}/api/${path.join("/")}${request.nextUrl.search}`;

  const headers = new Headers();
  headers.set("authorization", serviceToken ?? `Bearer ${session!.accessToken}`);
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  // x-user-email removed (pre-Beta hardening): the backend now reads the
  // verified email from Cognito at JIT instead of trusting a header.

  const hasBody = !["GET", "HEAD"].includes(request.method);
  const doFetch = () =>
    fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? request.body : undefined,
      // @ts-expect-error — required by undici when streaming a request body
      duplex: hasBody ? "half" : undefined,
      signal: request.signal,
      cache: "no-store",
    });

  let response: Response;
  try {
    response = await doFetch();
  } catch (error) {
    // Transient socket death (e.g. proxy reused a closed keep-alive connection):
    // retry once for idempotent requests instead of surfacing a 500.
    if (!hasBody) {
      response = await doFetch();
    } else {
      throw error;
    }
  }

  const responseHeaders = new Headers();
  response.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) responseHeaders.set(key, value);
  });
  return new Response(response.body, { status: response.status, headers: responseHeaders });
}

export {
  proxy as GET,
  proxy as POST,
  proxy as PUT,
  proxy as PATCH,
  proxy as DELETE,
};
