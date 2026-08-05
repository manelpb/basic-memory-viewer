"""Thin async client for basic-memory's MCP server at /mcp.

Transport is streamable HTTP by default (the current MCP spec transport);
set MCP_TRANSPORT=sse for basic-memory instances still running the
deprecated SSE transport.

One short-lived session per request: robust and stateless — the server accepts
many concurrent clients, and there is no shared mutable session to go stale
or hit anyio cross-task issues. All tools are called with output_format=json.

The exception is `health()`, which is called on a fixed probe cadence rather than
by a user: it caches successes, because sessions are not free to the server (see
the comment there).
"""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "http")  # "http" | "sse"
DEFAULT_PROJECT = os.environ.get("BM_PROJECT", "main")
MOCK_DATA = os.environ.get("MOCK_DATA") == "1"


@asynccontextmanager
async def _transport():
    """Yield a (read, write) stream pair for the configured transport."""
    if MCP_TRANSPORT == "sse":
        async with sse_client(MCP_URL) as (read, write):
            yield read, write
    else:
        async with streamablehttp_client(MCP_URL) as (read, write, _get_session_id):
            yield read, write


@asynccontextmanager
async def session():
    """Open one MCP session; yields an async `call(name, **args)` returning parsed JSON."""
    if MOCK_DATA:  # demo/screenshot mode — no basic-memory needed
        from . import mock
        yield mock.call
        return
    async with _transport() as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            async def call(name, **args):
                args.setdefault("output_format", "json")
                res = await s.call_tool(name, args)
                text = res.content[0].text if res.content else "null"
                return json.loads(text)

            yield call


# basic-memory retains ~11-24 kB per streamable-HTTP MCP session and never frees it
# (measured 2026-08-05: +4,860 kB RSS over 448 sessions, fastmcp 3.3.1 / mcp 1.27.1).
# An uncached health() therefore turns probe cadence directly into a memory leak in
# the server: one session every 15s = 5,760/day, which walked basic-memory into its
# 2Gi limit and OOMKilled it every ~9 days. Cache successes so probe traffic costs
# one session per HEALTH_TTL rather than one per request.
HEALTH_TTL = float(os.environ.get("HEALTH_TTL", "60"))

_health_lock = asyncio.Lock()
_health_ok_until = 0.0


async def health() -> bool:
    """True if basic-memory answers an MCP tool call. Successes cached HEALTH_TTL seconds.

    Failures are never cached — recovery must be visible on the very next probe.
    """
    global _health_ok_until
    if time.monotonic() < _health_ok_until:
        return True
    # Serialize concurrent probes so a burst costs one session, not one each.
    async with _health_lock:
        if time.monotonic() < _health_ok_until:
            return True
        try:
            async with session() as call:
                await call("list_memory_projects")
        except Exception:
            return False
        _health_ok_until = time.monotonic() + HEALTH_TTL
        return True
