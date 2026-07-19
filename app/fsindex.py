"""Filesystem description index — the fast path for colocated deploys.

When NOTES_DIR is set (viewer runs next to basic-memory on the notes volume),
card descriptions come straight from frontmatter on disk instead of MCP
read_note fan-out: a permalink -> description map built by walking *.md files,
refreshed at most every WALK_INTERVAL seconds and re-parsing only files whose
(mtime, size) changed. Hundreds of notes -> a walk is milliseconds.
"""
import os
import time

import yaml

from .history import NOTES_DIR

WALK_INTERVAL = 20.0

_desc: dict[str, str] = {}       # permalink -> frontmatter description
_path_perm: dict[str, str] = {}  # file path -> permalink (for deletions)
_stat: dict[str, tuple] = {}     # file path -> (mtime, size)
_last_walk = 0.0


def available() -> bool:
    return bool(NOTES_DIR)


def _parse(path: str):
    """(permalink, description) from a note's frontmatter, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(8192)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    end = head.find("\n---", 3)
    if end < 0:
        return None
    try:
        fm = yaml.safe_load(head[3:end])
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict) or not fm.get("permalink"):
        return None
    return str(fm["permalink"]), str(fm.get("description") or "")


def refresh(force: bool = False) -> None:
    """Stat-walk NOTES_DIR; parse only new/changed files, drop deleted ones.
    Blocking (call via asyncio.to_thread). Throttled to WALK_INTERVAL."""
    global _last_walk
    now = time.time()
    if not force and now - _last_walk < WALK_INTERVAL:
        return
    _last_walk = now
    seen = set()
    for root, dirs, files in os.walk(NOTES_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            seen.add(path)
            sig = (st.st_mtime, st.st_size)
            if _stat.get(path) == sig:
                continue
            _stat[path] = sig
            parsed = _parse(path)
            if parsed:
                _desc[parsed[0]] = parsed[1]
                _path_perm[path] = parsed[0]
    for path in set(_stat) - seen:
        _stat.pop(path, None)
        perm = _path_perm.pop(path, None)
        if perm:
            _desc.pop(perm, None)


def get(permalink: str):
    """Description for a permalink, or None when unknown (fall back to MCP)."""
    return _desc.get(permalink)
