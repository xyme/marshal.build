"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { useAdminReadonly } from "@/lib/admin-readonly";
import { api } from "@/lib/api";

/** Admin → Integrations (integration wave, 24 Aug 2026):
 * B9 service accounts + tokens, B10 outbound webhooks, B11 chat-ops channel.
 * Secrets (token values, webhook signing secrets) appear EXACTLY ONCE, in the
 * response to the action that created them. */

interface ServiceToken {
  id: string;
  name: string;
  prefix: string;
  expires_at: string;
  revoked: boolean;
  last_used_at: string | null;
}
interface ServiceAccount {
  id: string;
  name: string;
  role: string;
  status: string;
  tokens: ServiceToken[];
}
interface Webhook {
  id: string;
  url: string;
  description: string | null;
  event_types: string[];
  active: boolean;
}
interface Delivery {
  id: string;
  event_type: string;
  status: string;
  attempts: number;
  last_status_code: number | null;
  last_error: string | null;
  created_at: string;
}
interface ChatOps {
  enabled: boolean;
  provider: string;
  events: string[];
  has_url: boolean;
  event_types: string[];
}

export default function IntegrationsPage() {
  const readonly = useAdminReadonly();
  const [accounts, setAccounts] = useState<ServiceAccount[]>([]);
  const [webhooks, setWebhooks] = useState<Webhook[]>([]);
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  const [chatOps, setChatOps] = useState<ChatOps | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [secretReveal, setSecretReveal] = useState<{ label: string; value: string } | null>(null);
  const [deliveries, setDeliveries] = useState<{ id: string; items: Delivery[] } | null>(null);

  const load = useCallback(async () => {
    try {
      const [sa, wh, co] = await Promise.all([
        api<{ items: ServiceAccount[] }>("/v1/admin/service-accounts"),
        api<{ items: Webhook[]; event_types: string[] }>("/v1/admin/integrations/webhooks"),
        api<ChatOps>("/v1/admin/integrations/chat-ops"),
      ]);
      setAccounts(sa.items);
      setWebhooks(wh.items);
      setEventTypes(wh.event_types);
      setChatOps(co);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  async function act(fn: () => Promise<unknown>, okMessage: string) {
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(okMessage);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  // ---- service accounts
  const [newAccount, setNewAccount] = useState({ name: "", role: "power" });
  async function createAccount() {
    await act(
      () => api("/v1/admin/service-accounts", { method: "POST", body: JSON.stringify(newAccount) }),
      `Service account "${newAccount.name}" created.`
    );
    setNewAccount({ name: "", role: "power" });
  }
  async function mintToken(account: ServiceAccount) {
    setError(null);
    try {
      const result = await api<{ token: string; expires_at: string }>(
        `/v1/admin/service-accounts/${account.id}/tokens`,
        { method: "POST", body: JSON.stringify({ name: "token", expires_in_days: 90 }) }
      );
      setSecretReveal({
        label: `Token for ${account.name} (expires ${new Date(result.expires_at).toLocaleDateString()}) — copy it now, it is shown once`,
        value: result.token,
      });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  // ---- webhooks
  const [newWebhook, setNewWebhook] = useState({ url: "", events: [] as string[] });
  async function createWebhook() {
    setError(null);
    try {
      const result = await api<{ secret: string; url: string }>(
        "/v1/admin/integrations/webhooks",
        {
          method: "POST",
          body: JSON.stringify({ url: newWebhook.url, event_types: newWebhook.events }),
        }
      );
      setSecretReveal({
        label: `Signing secret for ${result.url} — copy it now, it is shown once`,
        value: result.secret,
      });
      setNewWebhook({ url: "", events: [] });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  // ---- chat-ops
  const [chatOpsUrl, setChatOpsUrl] = useState("");

  return (
    <div className="space-y-8">
      <h1 className="text-xl font-semibold">Integrations</h1>
      {notice && (
        <p className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">{notice}</p>
      )}
      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</p>
      )}
      {secretReveal && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4">
          <p className="text-sm text-amber-200">{secretReveal.label}</p>
          <div className="mt-2 flex items-center gap-2">
            <code className="break-all rounded bg-slate-950 px-2 py-1 text-xs text-slate-200">{secretReveal.value}</code>
            <button
              onClick={() => navigator.clipboard.writeText(secretReveal.value)}
              className="shrink-0 rounded-lg border border-slate-700 px-2 py-1 text-xs hover:border-slate-500"
            >
              copy
            </button>
            <button
              onClick={() => setSecretReveal(null)}
              className="shrink-0 rounded-lg border border-slate-700 px-2 py-1 text-xs hover:border-slate-500"
            >
              dismiss
            </button>
          </div>
        </div>
      )}

      {/* ---------------------------------------------- B9 service accounts */}
      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="font-medium">Service accounts</h2>
        <p className="mt-1 text-xs text-slate-400">
          Machine identities for CI and integrations. Act-only (a human owns every project);
          tokens expire ≤365 days, two live per account, revocable instantly. Never admin.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            value={newAccount.name}
            onChange={(e) => setNewAccount((v) => ({ ...v, name: e.target.value }))}
            placeholder="Account name (e.g. ci-runner)"
            className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          />
          <select
            value={newAccount.role}
            onChange={(e) => setNewAccount((v) => ({ ...v, role: e.target.value }))}
            className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm"
            aria-label="Service account role"
          >
            <option value="power">power</option>
            <option value="business">business</option>
          </select>
          <button
            onClick={createAccount}
            disabled={readonly || newAccount.name.trim().length < 3}
            className="rounded-lg bg-indigo-500 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
          >
            Create account
          </button>
        </div>
        <ul className="mt-4 space-y-3">
          {accounts.map((account) => (
            <li key={account.id} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{account.name}</span>
                <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase text-indigo-300">{account.role}</span>
                {account.status !== "active" && (
                  <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-[10px] uppercase text-red-300">{account.status}</span>
                )}
                <span className="ml-auto flex gap-2">
                  <button
                    onClick={() => mintToken(account)}
                    disabled={readonly}
                    className="rounded border border-slate-700 px-2 py-1 text-xs hover:border-indigo-400 disabled:opacity-40"
                  >
                    Mint token
                  </button>
                  <button
                    onClick={() =>
                      act(
                        () =>
                          api(`/v1/admin/service-accounts/${account.id}/status`, {
                            method: "PUT",
                            body: JSON.stringify({ status: account.status === "active" ? "suspended" : "active" }),
                          }),
                        account.status === "active" ? "Account suspended." : "Account reactivated."
                      )
                    }
                    disabled={readonly}
                    className="rounded border border-slate-700 px-2 py-1 text-xs hover:border-amber-400 disabled:opacity-40"
                  >
                    {account.status === "active" ? "Suspend" : "Reactivate"}
                  </button>
                </span>
              </div>
              {account.tokens.length > 0 && (
                <ul className="mt-2 space-y-1 text-xs text-slate-400">
                  {account.tokens.map((token) => (
                    <li key={token.id} className="flex items-center gap-2">
                      <code>{token.prefix}</code>
                      <span>expires {new Date(token.expires_at).toLocaleDateString()}</span>
                      <span>{token.last_used_at ? `last used ${new Date(token.last_used_at).toLocaleString()}` : "never used"}</span>
                      {token.revoked ? (
                        <span className="text-red-400">revoked</span>
                      ) : (
                        <button
                          onClick={() =>
                            act(
                              () =>
                                api(`/v1/admin/service-accounts/${account.id}/tokens/${token.id}`, { method: "DELETE" }),
                              "Token revoked."
                            )
                          }
                          disabled={readonly}
                          className="text-red-400 hover:text-red-300 disabled:opacity-40"
                        >
                          revoke
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
          {accounts.length === 0 && <li className="text-xs text-slate-500">No service accounts yet.</li>}
        </ul>
      </section>

      {/* ---------------------------------------------- B10 webhooks */}
      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="font-medium">Outbound webhooks</h2>
        <p className="mt-1 text-xs text-slate-400">
          HMAC-signed POSTs for build / deployment / risk lifecycle events. At-least-once with
          bounded retries; dead deliveries raise a platform alert. Payloads never carry secrets.
        </p>
        <div className="mt-3 space-y-2">
          <input
            value={newWebhook.url}
            onChange={(e) => setNewWebhook((v) => ({ ...v, url: e.target.value }))}
            placeholder="https://your-endpoint.example/hook"
            className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          />
          <div className="flex flex-wrap gap-2">
            {eventTypes.map((event) => (
              <label key={event} className="flex items-center gap-1 rounded border border-slate-800 px-2 py-1 text-xs text-slate-300">
                <input
                  type="checkbox"
                  checked={newWebhook.events.includes(event)}
                  onChange={(e) =>
                    setNewWebhook((v) => ({
                      ...v,
                      events: e.target.checked ? [...v.events, event] : v.events.filter((x) => x !== event),
                    }))
                  }
                />
                {event}
              </label>
            ))}
            <button
              onClick={createWebhook}
              disabled={readonly || !newWebhook.url.startsWith("https://") || newWebhook.events.length === 0}
              className="rounded-lg bg-indigo-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
            >
              Register
            </button>
          </div>
        </div>
        <ul className="mt-4 space-y-2">
          {webhooks.map((hook) => (
            <li key={hook.id} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <code className="truncate text-xs text-slate-300">{hook.url}</code>
                {!hook.active && <span className="rounded bg-slate-700/40 px-1.5 py-0.5 text-[10px] uppercase text-slate-400">inactive</span>}
                <span className="ml-auto flex gap-2">
                  <button
                    onClick={() =>
                      act(async () => {
                        const result = await api<{ ok: boolean; status_code?: number; error?: string }>(
                          `/v1/admin/integrations/webhooks/${hook.id}/test`,
                          { method: "POST" }
                        );
                        if (!result.ok) throw new Error(result.error ?? `HTTP ${result.status_code}`);
                      }, "Test delivery succeeded.")
                    }
                    disabled={readonly}
                    className="rounded border border-slate-700 px-2 py-1 text-xs hover:border-indigo-400 disabled:opacity-40"
                  >
                    Test
                  </button>
                  <button
                    onClick={() =>
                      act(
                        () =>
                          api(`/v1/admin/integrations/webhooks/${hook.id}`, {
                            method: "PUT",
                            body: JSON.stringify({ active: !hook.active }),
                          }),
                        hook.active ? "Webhook deactivated." : "Webhook activated."
                      )
                    }
                    disabled={readonly}
                    className="rounded border border-slate-700 px-2 py-1 text-xs hover:border-amber-400 disabled:opacity-40"
                  >
                    {hook.active ? "Deactivate" : "Activate"}
                  </button>
                  <button
                    onClick={async () => {
                      const result = await api<{ items: Delivery[] }>(`/v1/admin/integrations/webhooks/${hook.id}/deliveries`);
                      setDeliveries({ id: hook.id, items: result.items });
                    }}
                    className="rounded border border-slate-700 px-2 py-1 text-xs hover:border-slate-500"
                  >
                    Deliveries
                  </button>
                  <button
                    onClick={() =>
                      act(() => api(`/v1/admin/integrations/webhooks/${hook.id}`, { method: "DELETE" }), "Webhook deleted.")
                    }
                    disabled={readonly}
                    className="rounded border border-slate-700 px-2 py-1 text-xs text-red-400 hover:border-red-400 disabled:opacity-40"
                  >
                    Delete
                  </button>
                </span>
              </div>
              <div className="mt-1 text-xs text-slate-500">{hook.event_types.join(" · ")}</div>
              {deliveries?.id === hook.id && (
                <div className="mt-2 rounded border border-slate-800 bg-slate-950 p-2">
                  {deliveries.items.length === 0 ? (
                    <p className="text-xs text-slate-500">No deliveries yet.</p>
                  ) : (
                    <ul className="space-y-1 text-xs">
                      {deliveries.items.map((delivery) => (
                        <li key={delivery.id} className="flex flex-wrap gap-2">
                          <span
                            className={
                              delivery.status === "delivered"
                                ? "text-emerald-400"
                                : delivery.status === "dead"
                                  ? "text-red-400"
                                  : "text-amber-300"
                            }
                          >
                            {delivery.status}
                          </span>
                          <span className="text-slate-300">{delivery.event_type}</span>
                          <span className="text-slate-500">
                            {delivery.attempts} attempt(s)
                            {delivery.last_status_code ? ` · HTTP ${delivery.last_status_code}` : ""}
                            {delivery.last_error ? ` · ${delivery.last_error}` : ""}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </li>
          ))}
          {webhooks.length === 0 && <li className="text-xs text-slate-500">No webhooks registered.</li>}
        </ul>
      </section>

      {/* ---------------------------------------------- B11 chat-ops */}
      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
        <h2 className="font-medium">Chat-ops channel</h2>
        <p className="mt-1 text-xs text-slate-400">
          One org-level Slack or Teams incoming webhook. Failures degrade silently — in-app
          notifications remain the guaranteed channel. The URL is write-only.
        </p>
        {chatOps && (
          <div className="mt-3 space-y-3 text-sm">
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={chatOps.enabled}
                  onChange={(e) =>
                    act(
                      () =>
                        api("/v1/admin/integrations/chat-ops", {
                          method: "PUT",
                          body: JSON.stringify({ enabled: e.target.checked }),
                        }),
                      e.target.checked ? "Chat-ops enabled." : "Chat-ops disabled."
                    )
                  }
                />
                Enabled
              </label>
              <select
                value={chatOps.provider}
                onChange={(e) =>
                  act(
                    () =>
                      api("/v1/admin/integrations/chat-ops", {
                        method: "PUT",
                        body: JSON.stringify({ provider: e.target.value }),
                      }),
                    "Provider updated."
                  )
                }
                disabled={readonly}
                className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm disabled:opacity-50"
                aria-label="Chat-ops provider"
              >
                <option value="slack">Slack</option>
                <option value="teams">Teams</option>
              </select>
              <span className="text-xs text-slate-400">{chatOps.has_url ? "URL configured" : "No URL yet"}</span>
              <button
                onClick={() =>
                  act(async () => {
                    const result = await api<{ ok: boolean; status_code?: number; error?: string }>(
                      "/v1/admin/integrations/chat-ops/test",
                      { method: "POST" }
                    );
                    if (!result.ok) throw new Error(result.error ?? `HTTP ${result.status_code}`);
                  }, "Test post delivered.")
                }
                disabled={readonly}
                className="rounded border border-slate-700 px-2 py-1 text-xs hover:border-indigo-400 disabled:opacity-40"
              >
                Send test
              </button>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <input
                value={chatOpsUrl}
                onChange={(e) => setChatOpsUrl(e.target.value)}
                placeholder="https://hooks.slack.com/services/…"
                className="w-96 max-w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
              />
              <button
                onClick={() =>
                  act(
                    () =>
                      api("/v1/admin/integrations/chat-ops", {
                        method: "PUT",
                        body: JSON.stringify({ url: chatOpsUrl }),
                      }),
                    "Webhook URL stored (write-only)."
                  ).then(() => setChatOpsUrl(""))
                }
                disabled={readonly || !chatOpsUrl.startsWith("https://")}
                className="rounded-lg bg-indigo-500 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
              >
                Save URL
              </button>
            </div>
            <div className="flex flex-wrap gap-2">
              {chatOps.event_types.map((event) => (
                <label key={event} className="flex items-center gap-1 rounded border border-slate-800 px-2 py-1 text-xs text-slate-300">
                  <input
                    type="checkbox"
                    checked={chatOps.events.includes(event)}
                    onChange={(e) =>
                      act(
                        () =>
                          api("/v1/admin/integrations/chat-ops", {
                            method: "PUT",
                            body: JSON.stringify({
                              events: e.target.checked
                                ? [...chatOps.events, event]
                                : chatOps.events.filter((x) => x !== event),
                            }),
                          }),
                        "Event routing updated."
                      )
                    }
                  />
                  {event}
                </label>
              ))}
            </div>
          </div>
        )}
      </section>

      <ConnectorsSection readonly={readonly} />
    </div>
  );
}

// ------------------------------------------------------------ C0 connectors

interface Connector {
  slug: string;
  name: string;
  type: string;
  base_url: string;
  description: string;
  auth_header: string;
  has_credential: boolean;
  active: boolean;
  last_probe: {
    ok: boolean;
    status: number | null;
    error: string | null;
    error_class: string | null;
    rtt_ms?: number;
    checked_at: string;
  } | null;
  /** C2: read-only MCP handshake metadata (mcp_server type only). */
  mcp?: {
    ok: boolean;
    protocol: string | null;
    server: string | null;
    tools: { name: string; description: string }[];
    error: string | null;
    checked_at: string;
  } | null;
}

const CONNECTOR_TYPE_LABELS: Record<string, string> = {
  data_source: "Data source",
  http_api: "HTTP API",
  agent_registry: "Agent registry",
  mcp_server: "MCP server",
};

/** C0 registry (external-import-connectors spec): inventory + reachability.
 *  Honesty rule B-C0.4: the consumption note below renders from the LIVE
 *  connectors_enabled flag — never a hardcoded claim either way. */
function ConnectorsSection({ readonly }: { readonly: boolean }) {
  const [items, setItems] = useState<Connector[]>([]);
  const [types, setTypes] = useState<string[]>([]);
  const [consumptionOn, setConsumptionOn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ connectors_enabled?: boolean }>("/v1/meta/features")
      .then((f) => setConsumptionOn(!!f.connectors_enabled))
      .catch(() => {});
  }, []);
  const [draft, setDraft] = useState({
    slug: "",
    name: "",
    type: "data_source",
    base_url: "",
    auth_header: "",
    credential: "",
  });

  const load = useCallback(async () => {
    try {
      const body = await api<{ items: Connector[]; types: string[] }>(
        "/v1/admin/integrations/connectors"
      );
      setItems(body.items);
      setTypes(body.types);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function act(fn: () => Promise<unknown>, okMessage: string) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      toast.success(okMessage);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function createConnector() {
    const payload: Record<string, unknown> = {
      slug: draft.slug.trim(),
      name: draft.name.trim(),
      type: draft.type,
      base_url: draft.base_url.trim(),
    };
    if (draft.auth_header.trim()) payload.auth_header = draft.auth_header.trim();
    if (draft.credential.trim()) payload.credential = draft.credential.trim();
    act(
      () =>
        api("/v1/admin/integrations/connectors", {
          method: "POST",
          body: JSON.stringify(payload),
        }),
      "Connector registered."
    ).then(() =>
      setDraft({ slug: "", name: "", type: "data_source", base_url: "", auth_header: "", credential: "" })
    );
  }

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
      <h2 className="font-medium">Connectors</h2>
      <p className="mt-1 text-sm text-slate-400">
        Registered references to external systems — data sources, HTTP APIs, agent
        registries, MCP servers — with write-only credentials and reachability probes.
        {consumptionOn ? (
          <>
            <strong className="text-slate-300">
              {" "}Active connectors are consumable from specs
            </strong>{" "}
            — a requirements document declares &quot;SHALL use connector
            &lt;slug&gt;&quot;, and the deployer provisions the credential into the
            deployment&apos;s account (copy custody).
          </>
        ) : (
          <>
            <strong className="text-slate-300">
              {" "}Generated agents do not consume connectors yet
            </strong>{" "}
            — consumption ships dark until an administrator enables it.
          </>
        )}{" "}
        Register production credentials thoughtfully during the synthetic-only beta.
      </p>

      {error && (
        <p className="mt-3 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <input
          value={draft.slug}
          onChange={(e) => setDraft({ ...draft, slug: e.target.value })}
          placeholder="slug (permanent, e.g. claims-db)"
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
        />
        <input
          value={draft.name}
          onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          placeholder="Display name"
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
        />
        <select
          value={draft.type}
          onChange={(e) => setDraft({ ...draft, type: e.target.value })}
          aria-label="Connector type"
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
        >
          {(types.length ? types : Object.keys(CONNECTOR_TYPE_LABELS)).map((t) => (
            <option key={t} value={t}>
              {CONNECTOR_TYPE_LABELS[t] ?? t}
            </option>
          ))}
        </select>
        <input
          value={draft.base_url}
          onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
          placeholder="https://…"
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm sm:col-span-2"
        />
        <input
          value={draft.auth_header}
          onChange={(e) => setDraft({ ...draft, auth_header: e.target.value })}
          placeholder="Auth header (default: Authorization)"
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
        />
        <input
          value={draft.credential}
          onChange={(e) => setDraft({ ...draft, credential: e.target.value })}
          type="password"
          placeholder="Credential (write-only, optional)"
          autoComplete="off"
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm sm:col-span-2"
        />
        <button
          onClick={createConnector}
          disabled={
            readonly || busy || draft.slug.trim().length < 2 ||
            !draft.name.trim() || !draft.base_url.trim().startsWith("https://")
          }
          className="rounded-lg bg-indigo-500 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-40"
        >
          Register connector
        </button>
      </div>

      <ul className="mt-4 space-y-2">
        {items.length === 0 && (
          <li className="text-sm text-slate-400">No connectors registered.</li>
        )}
        {items.map((connector) => (
          <li key={connector.slug} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-slate-200">{connector.name}</span>
              <span className="rounded bg-indigo-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300">
                {CONNECTOR_TYPE_LABELS[connector.type] ?? connector.type}
              </span>
              {!connector.active && (
                <span className="rounded bg-slate-700/40 px-1.5 py-0.5 text-[10px] uppercase text-slate-400">
                  inactive
                </span>
              )}
              {connector.has_credential && (
                <span className="rounded bg-slate-700/40 px-1.5 py-0.5 text-[10px] uppercase text-slate-400">
                  credential set
                </span>
              )}
              {connector.last_probe && (
                <span
                  className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${
                    connector.last_probe.ok
                      ? "bg-emerald-500/15 text-emerald-400"
                      : "bg-red-500/15 text-red-300"
                  }`}
                  title={connector.last_probe.error ?? undefined}
                >
                  {connector.last_probe.ok
                    ? `reachable (${connector.last_probe.status})`
                    : connector.last_probe.error_class ?? "unreachable"}
                </span>
              )}
              {connector.type === "mcp_server" && connector.mcp && (
                <span
                  className={`rounded px-1.5 py-0.5 text-[10px] ${
                    connector.mcp.ok
                      ? "bg-indigo-500/15 text-indigo-300"
                      : "bg-amber-500/15 text-amber-300"
                  }`}
                  title={
                    connector.mcp.ok
                      ? connector.mcp.tools.map((t) => t.name).join(", ")
                      : connector.mcp.error ?? undefined
                  }
                >
                  {connector.mcp.ok
                    ? `MCP ${connector.mcp.server ?? ""} · ${connector.mcp.tools.length} tool(s)`
                    : "MCP handshake failed"}
                </span>
              )}
              <span className="ml-auto flex gap-2">
                <button
                  onClick={() =>
                    act(
                      () =>
                        api(`/v1/admin/integrations/connectors/${connector.slug}/probe`, {
                          method: "POST",
                        }),
                      "Probe finished."
                    )
                  }
                  disabled={readonly || busy}
                  className="rounded border border-slate-700 px-2 py-1 text-xs hover:border-indigo-400 disabled:opacity-40"
                >
                  Probe
                </button>
                <button
                  onClick={() =>
                    act(
                      () =>
                        api(`/v1/admin/integrations/connectors/${connector.slug}/status`, {
                          method: "PUT",
                          body: JSON.stringify({ active: !connector.active }),
                        }),
                      connector.active ? "Connector deactivated." : "Connector activated."
                    )
                  }
                  disabled={readonly || busy}
                  className="rounded border border-slate-700 px-2 py-1 text-xs hover:border-amber-400 disabled:opacity-40"
                >
                  {connector.active ? "Deactivate" : "Activate"}
                </button>
                <button
                  onClick={() =>
                    act(
                      () =>
                        api(`/v1/admin/integrations/connectors/${connector.slug}`, {
                          method: "DELETE",
                        }),
                      "Connector deleted."
                    )
                  }
                  disabled={readonly || busy}
                  className="rounded border border-slate-700 px-2 py-1 text-xs text-red-400 hover:border-red-400 disabled:opacity-40"
                >
                  Delete
                </button>
              </span>
            </div>
            <div className="mt-1 truncate text-xs text-slate-500">{connector.base_url}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}
