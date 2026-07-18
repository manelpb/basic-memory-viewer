"""memory-viewer — a lean, read-only web UI over basic-memory's MCP tools.

Server-rendered (no SPA build). Every page opens one short-lived MCP session and
makes a handful of tool calls. Search is live via a small fetch that swaps the
note-list fragment.
"""
import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # populate env from .env before mcp/config read it

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse, RedirectResponse, PlainTextResponse, StreamingResponse,
)
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
# Recent feed window. Notes span the full history (real created_at), so keep this
# wide — the feed is capped by RECENT_LIMIT, not the window.
RECENT_TIMEFRAME = os.environ.get("RECENT_TIMEFRAME", "365d")  # basic-memory caps at 1y
_DESC_CACHE: dict[str, tuple[str, float]] = {}  # permalink -> (description, ts)
_DESC_TTL = 300.0
_DESC_SEM = asyncio.Semaphore(8)  # cap concurrent read_note fan-out


async def _stream_descriptions(permalinks):
    """Yield NDJSON `{permalink, description}` lines, one per note, as each resolves.

    Cached hits stream immediately; misses fire concurrently (capped) and stream
    in completion order so the client fills each card as its answer lands — no
    all-at-once flush. frontmatter.description is what recent_activity omits.
    """
    now = time.time()
    missing = []
    for p in permalinks:
        hit = _DESC_CACHE.get(p)
        if hit and now - hit[1] < _DESC_TTL:
            yield json.dumps({"permalink": p, "description": hit[0]}) + "\n"
        else:
            missing.append(p)
    if not missing:
        return

    async def one(p):
        async with _DESC_SEM:
            try:
                n = await call("read_note", identifier=p, project=p.split("/")[0])
                return p, (n.get("frontmatter") or {}).get("description") or ""
            except Exception:
                return p, ""

    async with mcp.session() as call:
        tasks = [asyncio.create_task(one(p)) for p in missing]
        for fut in asyncio.as_completed(tasks):
            p, d = await fut
            _DESC_CACHE[p] = (d, time.time())
            yield json.dumps({"permalink": p, "description": d}) + "\n"


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


def _group_by_day(entities, active_permalink=None):
    # recent_activity does not return newest-first, so sort before grouping.
    # Rows render immediately with no descriptions; the client lazy-hydrates each
    # card's description via /descriptions as it scrolls into view.
    entities = sorted(entities, key=lambda e: e.get("created_at") or "", reverse=True)
    groups, cur = [], None
    for e in entities:
        label = day_label(parse_date(e.get("created_at")))
        if cur is None or cur["day"] != label:
            cur = {"day": label, "items": []}
            groups.append(cur)
        cur["items"].append(_row(e, active_permalink))
    return groups


def _entities(data):
    return data if isinstance(data, list) else (data or {}).get("results", [])


async def _recent_groups(call, project, active_permalink=None):
    data = await call("recent_activity", project=project, timeframe=RECENT_TIMEFRAME, page_size=RECENT_LIMIT)
    return _group_by_day(_entities(data), active_permalink)


async def _recent_all(call, names, active_permalink=None):
    """Cross-project recent feed: fan out per project, merge newest-first.
    Lets the home page show everything recent without pinning one default project."""
    results = await asyncio.gather(*[
        call("recent_activity", project=n, timeframe=RECENT_TIMEFRAME, page_size=RECENT_LIMIT)
        for n in names
    ], return_exceptions=True)
    merged = []
    for r in results:
        if not isinstance(r, Exception):
            merged.extend(_entities(r))
    merged.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return _group_by_day(merged[:RECENT_LIMIT], active_permalink)


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
async def index(request: Request, project: str = ""):
    project = project.strip()
    async with mcp.session() as call:
        if project:
            # projects + recent list are independent → fetch concurrently
            projects, groups = await asyncio.gather(
                _projects(call, project), _recent_groups(call, project))
        else:
            # no project chosen → cross-project recent feed (needs the names first)
            projects = await _projects(call, project)
            groups = await _recent_all(call, [p["name"] for p in projects])
        # open the most-recent note by default
        first = next((i for g in groups for i in g["items"]), None)
        note = await _note(call, first["permalink"]) if first else None
        if note and first:
            first["active"] = True
    return templates.TemplateResponse(request, "base.html", {
        "request": request, "projects": projects, "active_project": project,
        "groups": groups, "note": note, "search_mode": False, "query": "",
        "note_page": False,
    })


@app.get("/note/{permalink:path}", response_class=HTMLResponse)
async def note_page(request: Request, permalink: str):
    project = permalink.split("/")[0]
    # Client-side navigation asks for just the reading pane; skip list/projects.
    if request.headers.get("x-fragment"):
        async with mcp.session() as call:
            note = await _note(call, permalink)
        return templates.TemplateResponse(request, "_note.html", {
            "request": request, "note": note, "active_project": project,
        })
    async with mcp.session() as call:
        # all three are independent → fetch concurrently
        projects, groups, note = await asyncio.gather(
            _projects(call, project),
            _recent_groups(call, project, active_permalink=permalink),
            _note(call, permalink))
    return templates.TemplateResponse(request, "base.html", {
        "request": request, "projects": projects, "active_project": project,
        "groups": groups, "note": note, "search_mode": False, "query": "",
        "note_page": True,
    })


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", project: str = ""):
    q, project = q.strip(), project.strip()
    if not q:
        async with mcp.session() as call:
            if project:
                groups = await _recent_groups(call, project)
            else:
                projects = await _projects(call, project)
                groups = await _recent_all(call, [p["name"] for p in projects])
        return templates.TemplateResponse(request, "_rows.html", {
            "request": request, "groups": groups, "search_mode": False, "query": "", "count": 0,
        })
    async with mcp.session() as call:
        if project:
            sr = await call("search_notes", query=q, project=project, page_size=40)
        else:
            sr = await call("search_notes", query=q, search_all_projects=True, page_size=40)
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
    """Lazy-hydrate card descriptions: client sends permalinks of on-screen rows.
    Streams NDJSON so each card fills as its description resolves (no batch flush)."""
    perms = [p for p in ids.split(",") if p][:40]
    return StreamingResponse(
        _stream_descriptions(perms), media_type="application/x-ndjson")


@app.get("/go")
async def go(to: str):
    """Resolve a [[wikilink]] target to a note via search, then redirect."""
    async with mcp.session() as call:
        sr = await call("search_notes", query=to, page_size=1, search_all_projects=True)
    results = sr.get("results", [])
    if results:
        return RedirectResponse(f"/note/{results[0]['permalink']}", status_code=302)
    return RedirectResponse(f"/?", status_code=302)
