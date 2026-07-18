# basic-memory-viewer

A lean, **read-only** web UI for browsing and searching
[basic-memory](https://github.com/basicmachines-co/basic-memory) notes — a
self-hosted take on the basic-memory Cloud app.

It is a thin frontend over basic-memory's own MCP tools: it opens a short-lived
MCP session per request and calls `search_notes`, `read_note`,
`recent_activity`, and `list_memory_projects`. There is **no database and no
build step** — nothing to keep in sync. Search is basic-memory's own hybrid
full-text + semantic ranking; the view always reflects the live knowledge base.

## Features

- Three-pane layout: projects · note list · reading pane
- **Semantic search** (delegated to basic-memory's `search_notes`)
- Markdown rendering with `[[wikilink]]` resolution and a "Related" panel
- Card descriptions lazy-hydrate on scroll (batched, TTL-cached)
- Light/dark themes, read-only (no edit/delete affordances)

## Run locally

Point it at a reachable basic-memory MCP endpoint (SSE transport at `/mcp`):

```bash
pip install -r requirements.txt
MCP_URL=http://localhost:8000/mcp uvicorn app.main:app --port 8200
```

For a cluster-hosted basic-memory, port-forward it first:

```bash
kubectl port-forward -n basic-memory svc/basic-memory 8000:8000
```

## Configuration

| Env | Default | Purpose |
|-|-|-|
| `MCP_URL` | `http://localhost:8000/mcp` | basic-memory MCP SSE endpoint |
| `BM_PROJECT` | `main` | default project shown |
| `APP_TITLE` | `Memory` | brand name shown in the UI |
| `APP_USER` | _(empty)_ | optional account name in the sidebar |

## Container

```bash
docker build -t basic-memory-viewer .
docker run -p 8000:8000 -e MCP_URL=... basic-memory-viewer
```

CI (`.github/workflows/build.yml`) builds and pushes
`ghcr.io/manelpb/basic-memory-viewer` on every push to `main`.

## Deployment

Runs anywhere a container does — it only needs `MCP_URL` pointing at a reachable
basic-memory MCP endpoint. Deploy it alongside your basic-memory instance (same
network) so it can reach the MCP service directly. Kubernetes manifests
(Deployment / Service / Ingress) are maintained separately in your own GitOps
repo; this repo holds only the application and its image build.

## Endpoints

| Path | Purpose |
|-|-|
| `/` | index (recent notes for the default project) |
| `/note/{permalink}` | rendered note + related |
| `/search?q=&project=` | search results fragment (semantic) |
| `/descriptions?ids=` | batch note descriptions (lazy card hydration) |
| `/go?to=` | resolve a `[[wikilink]]` target → redirect |
| `/livez` `/healthz` | liveness / readiness (readiness checks MCP) |
