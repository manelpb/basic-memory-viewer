"""Optional note version history, backed by a git repo over the notes directory.

Three modes, chosen by env:
- NOTES_DIR set: this instance owns history. A background loop `git commit`s the
  notes dir every SNAPSHOT_INTERVAL seconds (repo auto-created at
  NOTES_DIR/.git), and versions/content/diffs are read straight from git.
  Run the viewer image colocated with basic-memory (same volume) in this mode.
- HISTORY_URL set: this instance proxies /history/* to a NOTES_DIR instance.
- Neither: the feature is hidden from the UI entirely.

Read-only stance: history is view/copy only — there is no restore.
"""
import asyncio
import difflib
import os
import subprocess

NOTES_DIR = os.environ.get("NOTES_DIR", "")
HISTORY_URL = os.environ.get("HISTORY_URL", "").rstrip("/")
SNAPSHOT_INTERVAL = int(os.environ.get("SNAPSHOT_INTERVAL", "300"))
# Optional off-site backup: push every snapshot to this remote (SSH url with a
# mounted deploy key, or HTTPS url carrying a token from a secret).
GIT_REMOTE = os.environ.get("GIT_REMOTE", "")
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main")

# Collapse runs of unchanged lines longer than this, keeping EDGE lines around edits.
COLLAPSE = 8
EDGE = 3


def native() -> bool:
    return bool(NOTES_DIR)


def enabled() -> bool:
    return bool(NOTES_DIR or HISTORY_URL)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ("git", "-C", NOTES_DIR, *args), capture_output=True, text=True, timeout=30)


def ensure_repo() -> None:
    if _git("rev-parse", "--git-dir").returncode != 0:
        _git("init", "-b", GIT_BRANCH)
        _git("config", "user.email", "history@memory-viewer")
        _git("config", "user.name", "memory-viewer history")
    if GIT_REMOTE:
        if _git("remote", "get-url", "origin").returncode != 0:
            _git("remote", "add", "origin", GIT_REMOTE)
        else:
            _git("remote", "set-url", "origin", GIT_REMOTE)


def snapshot() -> None:
    _git("add", "-A")
    committed = _git("commit", "-m", "snapshot")  # non-zero when nothing changed; fine
    if GIT_REMOTE and committed.returncode == 0:
        push = _git("push", "-u", "origin", GIT_BRANCH)
        if push.returncode != 0:
            print(f"history: push to backup remote failed: {push.stderr.strip()}", flush=True)


async def snapshot_loop() -> None:
    """Background task: periodic snapshots. Started from app startup when native."""
    await asyncio.to_thread(ensure_repo)
    while True:
        await asyncio.to_thread(snapshot)
        await asyncio.sleep(SNAPSHOT_INTERVAL)


def versions(relpath: str) -> list[dict]:
    """[{rev, date, size}] newest first; a leading pseudo-entry for the worktree."""
    out = [{"rev": "current", "date": "", "size": _size_now(relpath)}]
    log = _git("log", "--follow", "--format=%H %aI", "--", relpath).stdout.split()
    for rev, date in zip(log[::2], log[1::2]):
        show = _git("show", f"{rev}:{relpath}")
        if show.returncode == 0:
            out.append({"rev": rev, "date": date, "size": len(show.stdout.encode())})
    return out


def _size_now(relpath: str) -> int:
    try:
        return os.path.getsize(os.path.join(NOTES_DIR, relpath))
    except OSError:
        return 0


def content(relpath: str, rev: str) -> str:
    if rev == "current":
        try:
            with open(os.path.join(NOTES_DIR, relpath), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""
    res = _git("show", f"{rev}:{relpath}")
    return res.stdout if res.returncode == 0 else ""


def diff_rows(old: str, new: str) -> list[dict]:
    """Side-by-side rows for the history dialog.

    Row shapes: {t:"skip", n} · {t:"ctx", an, bn, text} ·
    {t:"chg", an?, a?, bn?, b?} (one side may be absent for pure add/delete).
    """
    a, b = old.splitlines(), new.splitlines()
    rows: list[dict] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            n = i2 - i1
            if n > COLLAPSE:
                head = EDGE if rows else 0          # no leading context at file start
                tail = EDGE
                for k in range(head):
                    rows.append({"t": "ctx", "an": i1 + k + 1, "bn": j1 + k + 1, "text": a[i1 + k]})
                rows.append({"t": "skip", "n": n - head - tail})
                for k in range(n - tail, n):
                    rows.append({"t": "ctx", "an": i1 + k + 1, "bn": j1 + k + 1, "text": a[i1 + k]})
            else:
                for k in range(n):
                    rows.append({"t": "ctx", "an": i1 + k + 1, "bn": j1 + k + 1, "text": a[i1 + k]})
        else:
            for k in range(max(i2 - i1, j2 - j1)):
                row: dict = {"t": "chg"}
                if i1 + k < i2:
                    row["an"], row["a"] = i1 + k + 1, a[i1 + k]
                if j1 + k < j2:
                    row["bn"], row["b"] = j1 + k + 1, b[j1 + k]
                rows.append(row)
    # A trailing skip at EOF needs no tail context either — cosmetic, skip trimming.
    return rows
