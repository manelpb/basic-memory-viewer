"""memory-viewer — a lean, read-only web UI over basic-memory's MCP tools.

Server-rendered (no SPA build). Every page opens one short-lived MCP session and
makes a handful of tool calls. Search is live via a small fetch that swaps the
note-list fragment.
"""
import asyncio
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import mcp
from .render import (
    render_markdown, prettify_title, snippet, parse_date, day_label,
    short_date, chip_class, category_of, clean_title,
)

import os

APP_TITLE = os.environ.get("APP_TITLE", "Memory")
APP_USER = os.environ.get("APP_USER", "")

BASE = Path(__file__).parent
app = FastAPI(title="memory-viewer")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals.update(app_title=APP_TITLE, app_user=APP_USER)


async def _projects(call, active):
    data = await call("list_memory_projects")
    names = [p["name"] for p in (data or {}).get("projects", [])]
    if mcp.DEFAULT_PROJECT in names:  # keep default first
        names.remove(mcp.DEFAULT_PROJECT)
        names.insert(0, mcp.DEFAULT_PROJECT)
    return [{"name": n, "active": n == active} for n in names]


RECENT_LIMIT = 60
_DESC_CACHE: dict[str, tuple[str, float]] = {}  # permalink -> (description, ts)
_DESC_TTL = 300.0


async def _descriptions(call, permalinks):
    """frontmatter.description per note (recent_activity omits it). Concurrent + TTL-cached."""
    now = time.time()
    out, missing = {}, []
    for p in permalinks:
        hit = _DESC_CACHE.get(p)
        if hit and now - hit[1] < _DESC_TTL:
            out[p] = hit[0]
        else:
            missing.append(p)

    async def one(p):
        try:
            n = await call("read_note", identifier=p, project=p.split("/")[0])
            return p, (n.get("frontmatter") or {}).get("description") or ""
        except Exception:
            return p, ""

    for p, d in await asyncio.gather(*[one(p) for p in missing]):
        _DESC_CACHE[p] = (d, now)
        out[p] = d
    return out


def _row(entity, active_permalink=None, desc=""):
    dt = parse_date(entity.get("created_at"))
    permalink = entity.get("permalink")
    cat = category_of(permalink)
    return {
        "permalink": permalink,
        "title": clean_title(entity.get("title", ""), cat),
        "chip": chip_class(cat),
        "date": short_date(dt),
        "snip": snippet(desc or entity.get("content") or entity.get("description") or ""),
        "active": permalink == active_permalink,
    }


async def _recent_groups(call, project, active_permalink=None):
    # Rows render immediately with no descriptions; the client lazy-hydrates each
    # card's description via /descriptions as it scrolls into view.
    data = await call("recent_activity", project=project, timeframe="90d", page_size=RECENT_LIMIT)
    entities = data if isinstance(data, list) else data.get("results", [])
    groups, cur = [], None
    for e in entities:
        label = day_label(parse_date(e.get("created_at")))
        if cur is None or cur["day"] != label:
            cur = {"day": label, "items": []}
            groups.append(cur)
        cur["items"].append(_row(e, active_permalink))
    return groups


async def _note(call, permalink):
    project = permalink.split("/")[0]
    note = await call("read_note", identifier=permalink, project=project)
    fm = note.get("frontmatter", {}) or {}
    title = fm.get("title") or note.get("title") or permalink.rsplit("/", 1)[-1]
    # Related: semantic search on the title, minus self
    related = []
    try:
        sr = await call("search_notes", query=prettify_title(title), project=project, page_size=7)
        for r in sr.get("results", []):
            rp = r.get("permalink")
            if rp != permalink:
                related.append({
                    "permalink": rp,
                    "title": clean_title(r.get("title", ""), category_of(rp)),
                    "chip": chip_class(category_of(rp)),
                })
        related = related[:6]
    except Exception:
        pass
    parts = permalink.split("/")
    cat = category_of(permalink)
    return {
        "title": clean_title(title, cat),
        "chip": chip_class(cat or fm.get("type")),
        "type": fm.get("type") or "note",
        "permalink": permalink,
        "description": fm.get("description") or "",
        "tags": [t for t in (fm.get("tags") or []) if t and t != fm.get("type")],
        "body_html": render_markdown(note.get("content", "")),
        "crumbs": parts,
        "related": related,
    }


@app.get("/livez", response_class=PlainTextResponse)
async def livez():
    return "ok"


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    ok = await mcp.health()
    return PlainTextResponse("ok" if ok else "unavailable", status_code=200 if ok else 503)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, project: str = mcp.DEFAULT_PROJECT):
    async with mcp.session() as call:
        projects = await _projects(call, project)
        groups = await _recent_groups(call, project)
        # open the most-recent note by default
        first = next((i for g in groups for i in g["items"]), None)
        note = await _note(call, first["permalink"]) if first else None
        if note and first:
            first["active"] = True
    return templates.TemplateResponse(request, "base.html", {
        "request": request, "projects": projects, "active_project": project,
        "groups": groups, "note": note, "search_mode": False, "query": "",
    })


@app.get("/note/{permalink:path}", response_class=HTMLResponse)
async def note_page(request: Request, permalink: str):
    project = permalink.split("/")[0]
    async with mcp.session() as call:
        projects = await _projects(call, project)
        groups = await _recent_groups(call, project, active_permalink=permalink)
        note = await _note(call, permalink)
    return templates.TemplateResponse(request, "base.html", {
        "request": request, "projects": projects, "active_project": project,
        "groups": groups, "note": note, "search_mode": False, "query": "",
    })


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", project: str = mcp.DEFAULT_PROJECT):
    q = q.strip()
    if not q:
        async with mcp.session() as call:
            groups = await _recent_groups(call, project)
        return templates.TemplateResponse(request, "_rows.html", {
            "request": request, "groups": groups, "search_mode": False, "query": "", "count": 0,
        })
    async with mcp.session() as call:
        sr = await call("search_notes", query=q, project=project, page_size=40)
    results = sr.get("results", [])
    items = [{
        "permalink": r.get("permalink"),
        "title": clean_title(r.get("title", ""), category_of(r.get("permalink"))),
        "chip": chip_class(category_of(r.get("permalink"))),
        "date": "",
        "snip": snippet(r.get("content") or ""),
        "active": False,
    } for r in results]
    groups = [{"day": "", "items": items}] if items else []
    return templates.TemplateResponse(request, "_rows.html", {
        "request": request, "groups": groups, "search_mode": True, "query": q,
        "count": len(items),
    })


@app.get("/descriptions")
async def descriptions(ids: str = ""):
    """Lazy-hydrate card descriptions: client sends permalinks of on-screen rows."""
    perms = [p for p in ids.split(",") if p][:40]
    if not perms:
        return {}
    async with mcp.session() as call:
        return await _descriptions(call, perms)


@app.get("/go")
async def go(to: str):
    """Resolve a [[wikilink]] target to a note via search, then redirect."""
    async with mcp.session() as call:
        sr = await call("search_notes", query=to, page_size=1, search_all_projects=True)
    results = sr.get("results", [])
    if results:
        return RedirectResponse(f"/note/{results[0]['permalink']}", status_code=302)
    return RedirectResponse(f"/?", status_code=302)
