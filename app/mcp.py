"""Thin async client for basic-memory's MCP server (SSE transport at /mcp).

One short-lived session per request: robust and stateless — the server accepts
many concurrent SSE clients, and there is no shared mutable session to go stale
or hit anyio cross-task issues. All tools are called with output_format=json.
"""
import json
import os
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.sse import sse_client

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
DEFAULT_PROJECT = os.environ.get("BM_PROJECT", "main")


@asynccontextmanager
async def session():
    """Open one MCP session; yields an async `call(name, **args)` returning parsed JSON."""
    async with sse_client(MCP_URL) as (read, write):
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
