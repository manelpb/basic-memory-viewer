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

from . import fsindex, history, mcp, mock
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
templates.env.globals.update(
    app_title=APP_TITLE, app_user=APP_USER,
    history_enabled=history.enabled() or mcp.MOCK_DATA)


@app.on_event("startup")
async def _start_history():
    if history.native():
        asyncio.create_task(history.snapshot_loop())


_PROJ_CACHE: tuple[list, float] | None = None  # (names, ts)
_PROJ_TTL = 300.0


async def _projects(call, active):
    global _PROJ_CACHE
    now = time.monotonic()
    if _PROJ_CACHE and now - _PROJ_CACHE[1] < _PROJ_TTL:
        names = _PROJ_CACHE[0]
    else:
        data = await call("list_memory_projects")
        names = [p["name"] for p in (data or {}).get("projects", [])]
        if mcp.DEFAULT_PROJECT in names:  # keep default first
            names.remove(mcp.DEFAULT_PROJECT)
            names.insert(0, mcp.DEFAULT_PROJECT)
        _PROJ_CACHE = (names, now)
    return [{"name": n, "active": n == active} for n in names]


# basic-memory rejects a larger page_size outright ("page_size must be <= 100").
BM_MAX_PAGE_SIZE = 100
# Rows per page of the recent feed. This is a page size, not a ceiling: the feed
# pages back through the whole timeframe via /recent (see `_recent_groups`).
RECENT_LIMIT = max(1, min(int(os.environ.get("RECENT_LIMIT", "60")), BM_MAX_PAGE_SIZE))
# Recent feed window. Notes span the full history (real created_at), so keep this
# wide — nothing older than this is reachable, however far the feed pages back.
RECENT_TIMEFRAME = os.environ.get("RECENT_TIMEFRAME", "365d")  # basic-memory caps at 1y
# permalink -> (description, change token). The token is the note's
# updated_at/created_at from the feed: entries never expire, they are simply
# superseded when the note changes — better freshness AND fewer read_notes
# than the old 300s TTL.
_DESC_CACHE: dict[str, tuple[str, str]] = {}
_DESC_SEM = asyncio.Semaphore(8)  # cap concurrent read_note fan-out


async def _stream_descriptions(pairs):
    """Yield NDJSON `{permalink, description}` lines, one per note, as each resolves.

    `pairs` is [(permalink, change_token)]. Resolution order per note:
    1. NOTES_DIR frontmatter index (colocated deploys) — no MCP at all;
    2. token-keyed cache;
    3. MCP read_note fan-out (capped), streamed in completion order.
    frontmatter.description is what recent_activity omits.
    """
    missing = []
    if fsindex.available():
        await asyncio.to_thread(fsindex.refresh)
    for p, tok in pairs:
        if fsindex.available():
            d = fsindex.get(p)
            if d is not None:
                yield json.dumps({"permalink": p, "description": d}) + "\n"
                continue
        hit = _DESC_CACHE.get(p)
        if hit and hit[1] == tok:
            yield json.dumps({"permalink": p, "description": hit[0]}) + "\n"
        else:
            missing.append((p, tok))
    if not missing:
        return

    async def one(p, tok):
        async with _DESC_SEM:
            try:
                # Timeout so a wedged MCP call can't hold a semaphore slot forever.
                n = await asyncio.wait_for(
                    call("read_note", identifier=p, project=p.split("/")[0]), 15)
                return p, tok, (n.get("frontmatter") or {}).get("description") or ""
            except Exception:
                return p, tok, ""

    try:
        session = mcp.session()
        call = await session.__aenter__()
    except Exception:
        # MCP unreachable: answer with empty descriptions rather than aborting
        # the stream mid-chunk (which strands client-side spinners). Uncached on
        # purpose — the next request retries.
        for p, _tok in missing:
            yield json.dumps({"permalink": p, "description": ""}) + "\n"
        return
    try:
        tasks = [asyncio.create_task(one(p, tok)) for p, tok in missing]
        try:
            for fut in asyncio.as_completed(tasks):
                p, tok, d = await fut
                _DESC_CACHE[p] = (d, tok)
                yield json.dumps({"permalink": p, "description": d}) + "\n"
        finally:
            # Client aborts cancel this generator mid-stream. Without cleanup the
            # orphaned tasks outlive the session teardown and hang on the dead
            # session while holding _DESC_SEM slots — after 8 leaks every later
            # /descriptions request blocks forever (infinite spinners).
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await session.__aexit__(None, None, None)


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
        # change token for the description cache (see _stream_descriptions)
        "upd": entity.get("updated_at") or entity.get("created_at") or "",
    }


def _feed_key(e):
    # permalink breaks ties so a batch sorts the same way every time: notes written
    # in the same second are common (a bulk edit), and an unstable order would
    # shuffle rows between one page request and the next.
    return (e.get("created_at") or "", e.get("permalink") or "")


def _group_by_day(entities, active_permalink=None):
    # recent_activity does not return newest-first, so sort before grouping.
    # Rows render immediately with no descriptions; the client lazy-hydrates each
    # card's description via /descriptions as it scrolls into view.
    entities = sorted(entities, key=_feed_key, reverse=True)
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


async def _recent_groups(call, project, active_permalink=None, page=1):
    """One page of a single project's feed. Returns (groups, has_more).

    recent_activity pages natively, so this is one call per page. A full page
    back means there may be another; the next page returning nothing is what
    settles it (an exact multiple over-promises once, harmlessly).
    """
    data = await call("recent_activity", project=project, timeframe=RECENT_TIMEFRAME,
                      page=page, page_size=RECENT_LIMIT)
    entities = _entities(data)
    return _group_by_day(entities, active_permalink), len(entities) >= RECENT_LIMIT


async def _take(call, project, count):
    """The newest `count` feed entities of one project.

    recent_activity caps page_size at 100, so a deeper window is stitched from
    consecutive native pages. The page size stays fixed across the loop — the
    server's offset is (page - 1) * page_size, so varying it mid-walk would skip
    or repeat rows.
    """
    size = min(BM_MAX_PAGE_SIZE, count)
    out, page = [], 1
    while len(out) < count:
        batch = _entities(await call("recent_activity", project=project,
                                     timeframe=RECENT_TIMEFRAME, page=page, page_size=size))
        out.extend(batch)
        if len(batch) < size:  # short page = end of the window
            break
        page += 1
    return out[:count]


async def _recent_all(call, names, active_permalink=None, page=1):
    """Cross-project recent feed: fan out per project, merge newest-first.
    Lets the home page show everything recent without pinning one default project.

    Page N cannot be assembled from each project's page N — the merge interleaves
    them — so each project is asked for everything up to the end of the window and
    the merged list is sliced. One extra row past the end is what tells us whether
    a further page exists.

    A row can still repeat at a page boundary: recent_activity's own order is not
    exactly created_at-descending, so a note pulled in by the wider fetch can sort
    ahead of the boundary and shift it. The client drops rows it already has.
    """
    end = RECENT_LIMIT * page
    results = await asyncio.gather(*[_take(call, n, end + 1) for n in names],
                                   return_exceptions=True)
    merged = []
    for r in results:
        if not isinstance(r, Exception):
            merged.extend(r)
    merged.sort(key=_feed_key, reverse=True)
    return _group_by_day(merged[end - RECENT_LIMIT:end], active_permalink), len(merged) > end


async def _note(call, permalink):
    # Related is NOT computed here: it is a semantic search costing ~1s in
    # basic-memory, so the client lazy-loads it from /related after render.
    project = permalink.split("/")[0]
    note = await call("read_note", identifier=permalink, project=project)
    fm = note.get("frontmatter", {}) or {}
    title = fm.get("title") or note.get("title") or permalink.rsplit("/", 1)[-1]
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
    }


async def _related(call, permalink, title):
    """Semantic search on the note title, minus the note itself."""
    related = []
    try:
        sr = await call("search_notes", query=prettify_title(title),
                        project=permalink.split("/")[0], page_size=7)
        for r in sr.get("results", []):
            rp = r.get("permalink")
            if rp != permalink:
                related.append({
                    "permalink": rp,
                    "title": clean_title(r.get("title", ""), category_of(rp)),
                    "chip": chip_class(category_of(rp)),
                })
    except Exception:
        pass
    return related[:6]


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
            projects, (groups, has_more) = await asyncio.gather(
                _projects(call, project), _recent_groups(call, project))
        else:
            # no project chosen → cross-project recent feed (needs the names first)
            projects = await _projects(call, project)
            groups, has_more = await _recent_all(call, [p["name"] for p in projects])
        # open the most-recent note by default
        first = next((i for g in groups for i in g["items"]), None)
        note = await _note(call, first["permalink"]) if first else None
        if note and first:
            first["active"] = True
    _prewarm(groups)
    return templates.TemplateResponse(request, "base.html", {
        "request": request, "projects": projects, "active_project": project,
        "groups": groups, "note": note, "search_mode": False, "query": "",
        "note_page": False, "has_more": has_more, "next_page": 2,
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
        projects, (groups, has_more), note = await asyncio.gather(
            _projects(call, project),
            _recent_groups(call, project, active_permalink=permalink),
            _note(call, permalink))
    _prewarm(groups)
    return templates.TemplateResponse(request, "base.html", {
        "request": request, "projects": projects, "active_project": project,
        "groups": groups, "note": note, "search_mode": False, "query": "",
        "note_page": True, "has_more": has_more, "next_page": 2,
    })


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", project: str = ""):
    q, project = q.strip(), project.strip()
    if not q:
        async with mcp.session() as call:
            if project:
                groups, has_more = await _recent_groups(call, project)
            else:
                projects = await _projects(call, project)
                groups, has_more = await _recent_all(call, [p["name"] for p in projects])
        return templates.TemplateResponse(request, "_rows.html", {
            "request": request, "groups": groups, "search_mode": False, "query": "", "count": 0,
            "has_more": has_more, "next_page": 2,
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


@app.get("/recent", response_class=HTMLResponse)
async def recent_fragment(request: Request, project: str = "", page: int = 1):
    """Page 2+ of the recent feed, as bare rows the client appends.

    Page 1 is rendered inline by whichever page you landed on; this is what the
    feed's "Load more" button asks for. Search results are not paged — they come
    from search_notes, which has its own cap.
    """
    project, page = project.strip(), max(1, page)
    async with mcp.session() as call:
        if project:
            groups, has_more = await _recent_groups(call, project, page=page)
        else:
            projects = await _projects(call, project)
            groups, has_more = await _recent_all(
                call, [p["name"] for p in projects], page=page)
    _prewarm(groups)
    return templates.TemplateResponse(request, "_rowgroups.html", {
        "request": request, "groups": groups, "search_mode": False,
        "appending": True, "has_more": has_more, "next_page": page + 1,
    })


def _prewarm(groups):
    """Fire-and-forget cache fill for freshly rendered feed rows, so the first
    scroll hits a warm cache. MCP mode only: the fs index needs no warming and
    mock data is instant."""
    if fsindex.available() or mcp.MOCK_DATA:
        return
    pairs = [(i["permalink"], i["upd"]) for g in groups for i in g["items"]]
    pairs = [(p, t) for p, t in pairs
             if not (_DESC_CACHE.get(p) and _DESC_CACHE[p][1] == t)][:40]
    if not pairs:
        return

    async def drain():
        try:
            async for _ in _stream_descriptions(pairs):
                pass
        except Exception:
            pass

    asyncio.create_task(drain())


@app.get("/descriptions")
async def descriptions(ids: str = ""):
    """Lazy-hydrate card descriptions: client sends `permalink|change_token`
    entries for on-screen rows. Streams NDJSON so each card fills as its
    description resolves (no batch flush)."""
    pairs = []
    for entry in ids.split(","):
        if entry:
            p, _, tok = entry.partition("|")
            pairs.append((p, tok))
    return StreamingResponse(
        _stream_descriptions(pairs[:40]), media_type="application/x-ndjson")


@app.get("/related/{permalink:path}", response_class=HTMLResponse)
async def related_fragment(request: Request, permalink: str, title: str = ""):
    """Lazy 'Related' panel: semantic search is slow (~1s), so the note renders
    first and the client fills this fragment in afterwards."""
    async with mcp.session() as call:
        related = await _related(call, permalink, title or permalink.rsplit("/", 1)[-1])
    return templates.TemplateResponse(request, "_related.html", {
        "request": request, "related": related,
    })


async def _note_relpath(permalink: str) -> str:
    """Map a permalink to its path inside NOTES_DIR: <project>/<file_path>.
    read_note is the source of truth for the real filename (slug != filename)."""
    project = permalink.split("/")[0]
    async with mcp.session() as call:
        note = await call("read_note", identifier=permalink, project=project)
    fp = (note or {}).get("file_path")
    if not fp:
        raise LookupError(permalink)
    return f"{project}/{fp}"


async def _history_proxy(path: str, params: dict):
    import httpx
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(history.HISTORY_URL + path, params=params)
        return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/history/{permalink:path}")
async def note_history(permalink: str):
    """Version list for a note. 404 when the feature is off or the note is unknown."""
    if mcp.MOCK_DATA:
        return {"versions": mock.versions(permalink)}
    if history.HISTORY_URL and not history.native():
        return await _history_proxy(f"/history/{permalink}", {})
    if not history.native():
        return JSONResponse({"error": "history disabled"}, status_code=404)
    try:
        relpath = await _note_relpath(permalink)
    except LookupError:
        return JSONResponse({"error": "note not found"}, status_code=404)
    return {"versions": await asyncio.to_thread(history.versions, relpath)}


@app.get("/history-diff/{permalink:path}")
async def note_history_diff(permalink: str, rev: str):
    """Diff of a past revision against the current file, plus the old content."""
    if mcp.MOCK_DATA:
        old, new = mock.content(permalink, rev), mock.content(permalink, "current")
        return {"rows": history.diff_rows(old, new), "content": old}
    if history.HISTORY_URL and not history.native():
        return await _history_proxy(f"/history-diff/{permalink}", {"rev": rev})
    if not history.native():
        return JSONResponse({"error": "history disabled"}, status_code=404)
    try:
        relpath = await _note_relpath(permalink)
    except LookupError:
        return JSONResponse({"error": "note not found"}, status_code=404)
    old = await asyncio.to_thread(history.content, relpath, rev)
    new = await asyncio.to_thread(history.content, relpath, "current")
    return {"rows": history.diff_rows(old, new), "content": old}


@app.get("/go")
async def go(to: str):
    """Resolve a [[wikilink]] target to a note via search, then redirect."""
    async with mcp.session() as call:
        sr = await call("search_notes", query=to, page_size=1, search_all_projects=True)
    results = sr.get("results", [])
    if results:
        return RedirectResponse(f"/note/{results[0]['permalink']}", status_code=302)
    return RedirectResponse(f"/?", status_code=302)
