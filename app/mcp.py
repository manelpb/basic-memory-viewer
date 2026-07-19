"""Thin async client for basic-memory's MCP server at /mcp.

Transport is streamable HTTP by default (the current MCP spec transport);
set MCP_TRANSPORT=sse for basic-memory instances still running the
deprecated SSE transport.

One short-lived session per request: robust and stateless — the server accepts
many concurrent clients, and there is no shared mutable session to go stale
or hit anyio cross-task issues. All tools are called with output_format=json.
"""
import json
import os
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


async def health() -> bool:
    try:
        async with session() as call:
            await call("list_memory_projects")
        return True
    except Exception:
        return False
