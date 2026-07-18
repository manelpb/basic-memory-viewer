"""Markdown + basic-memory helpers: wikilinks, titles, snippets, date grouping."""
import re
from datetime import datetime, date
from urllib.parse import quote

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "typographer": False}).enable(
    ["table", "strikethrough"]
)

_WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def _wikilink_sub(m: re.Match) -> str:
    target = m.group(1).strip()
    alias = (m.group(2) or m.group(1)).strip()
    return f"[{alias}](/go?to={quote(target)})"


def render_markdown(content: str) -> str:
    """basic-memory wikilinks -> resolver links, then CommonMark -> HTML."""
    if not content:
        return ""
    content = _WIKILINK.sub(_wikilink_sub, content)
    html = _md.render(content)
    # tag resolver links so CSS can style them like the mock's [[wikilinks]]
    html = html.replace('<a href="/go?to=', '<a class="wl" href="/go?to=')
    return html


def prettify_title(title: str) -> str:
    """Slug-ish titles (project-litellm-gateway) -> readable; leave real titles alone."""
    if not title:
        return "Untitled"
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", title):
        return title.replace("_", " ").replace("-", " ")
    return title


def category_of(permalink: str) -> str:
    """basic-memory permalinks are <project>/<category>/<slug>; the folder is the type."""
    parts = (permalink or "").split("/")
    return parts[1] if len(parts) >= 3 else ""


def clean_title(title: str, category: str = "") -> str:
    """Prettify a slug title and drop a redundant leading category word (shown as a chip)."""
    t = prettify_title(title)
    if category and t.lower().startswith(category.lower() + " "):
        t = t[len(category) + 1:]
    return (t[:1].upper() + t[1:]) if t else t


def snippet(text: str, n: int = 165) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def parse_date(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def day_label(dt: datetime) -> str:
    if dt is None:
        return "Earlier"
    today = date.today()
    d = dt.date()
    if d == today:
        return "Today"
    if (today - d).days == 1:
        return "Yesterday"
    if dt.year == today.year:
        return dt.strftime("%b %-d")
    return dt.strftime("%b %-d, %Y")


def short_date(dt: datetime) -> str:
    if dt is None:
        return ""
    today = date.today()
    if dt.year == today.year:
        return dt.strftime("%b %-d")
    return dt.strftime("%b %Y")


# note "type" -> chip class the CSS understands (project/reference/feedback/incident/other)
_KNOWN = {"project", "reference", "feedback", "incident"}


def chip_class(t: str) -> str:
    t = (t or "").lower()
    return t if t in _KNOWN else "note"
