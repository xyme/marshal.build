# marshal SharePoint MCP adapter

A small operator-run service that puts SharePoint Online behind an ordinary
marshal `mcp_server` connector. The adapter owns the Microsoft credentials
(Entra client-credentials flow + token cache); marshal custodies ONLY the
adapter's static API key. Read-only by construction: four tools, per-site
allowlisting, honest format refusals, hard size caps.

Spec: the SharePoint-connector specification (internal, 13.5S; see docs/ARCHITECTURE.md for the public account). The platform side
is ZERO-change — probe, grounding and tool-loop agents already speak MCP.

## Tools

| Tool | What it does | Caps |
| --- | --- | --- |
| `list_sites` | The allowlisted sites, resolved against Graph | — |
| `list_documents` | Documents in an allowed site (optional folder path) | 50 items |
| `search_documents` | Keyword search across allowed sites | 20 results |
| `get_document_text` | Text content of one document | 2MB fetch, 40k chars; txt/md/csv/json/html/docx only, no OCR |

## Microsoft side

1. Entra app registration (single tenant), **client credentials** only.
2. Application permission `Sites.Selected` — then grant the app access to
   each site individually (the grant list IS the data boundary):
   `POST /sites/{site-id}/permissions` with the app id, role `read`.
3. When evaluating, point the adapter at a disposable test tenant with
   synthetic documents before granting it any production site.

## Configuration (environment)

| Variable | Meaning |
| --- | --- |
| `SP_TENANT_ID` / `SP_CLIENT_ID` / `SP_CLIENT_SECRET` | The Entra app |
| `SP_ALLOWED_SITES` | Comma-separated site identifiers, e.g. `contoso.sharepoint.com:/sites/engineering` — enforced adapter-side on every call, defense in depth above Sites.Selected |
| `SP_API_KEY` | The static key marshal's registry custodies — generate long and random |
| `SP_AUTH_HEADER` | Header carrying the key (default `Authorization`) |
| `PORT` | Listen port (default 8080) |

## Run it

Local (drill):

```bash
pip install -r requirements.txt
SP_TENANT_ID=… SP_CLIENT_ID=… SP_CLIENT_SECRET=… \
SP_ALLOWED_SITES="contoso.sharepoint.com:/sites/x" SP_API_KEY=… \
python adapter.py
```

Container: `docker build -t sharepoint-adapter .` — runs on any container
host. The image also embeds the Lambda Web Adapter, so the RECOMMENDED
hosting is one Lambda container + function URL in the control-plane
account: push to ECR, create the `marshal/adapters/sharepoint` secret, and
deploy `template.yaml` (header comments carry the exact commands). The
function URL uses no AWS auth BY DESIGN — the adapter enforces its own
static key on every request, which is exactly the credential marshal
registers.

## Register in marshal (owner gate 4)

Admin → Integrations → register connector: type `mcp_server`, base_url =
the adapter endpoint, credential = `SP_API_KEY`, header = `SP_AUTH_HEADER`,
description NAMING the tenant and sites (registry honesty, spec R4.4).
Probe green ⇒ the card lists the four tools. With `connectors_enabled` on,
specs declare `SHALL use MCP tools from connector sharepoint`.

## Key rotation / revocation

Rotate: update the secret + restart (or redeploy) the adapter, update the
connector credential in Admin → Integrations, re-probe. Revoke Microsoft
access: remove the app's per-site grants (tools then fail with a clear
permission error — that's AC-5, surfaced on probe).

## Operational notes

- Logs carry tool names and the `x-marshal-caller` attribution header,
  never document content or credentials.
- The token cache refreshes single-flight ~5 minutes before expiry.
- `GET /healthz` for liveness.

## Tests

```bash
pip install -r requirements.txt pytest==8.4.2 && python -m pytest tests/ -q
```
