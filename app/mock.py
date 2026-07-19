"""Canned in-process data for demos and screenshots — no basic-memory needed.

Enable with MOCK_DATA=1; `mcp.session()` then yields this module's `call`
instead of opening a real MCP session. Dates are relative to now so the
recent feed always looks alive.
"""
from datetime import datetime, timedelta

_now = datetime.now


def _ago(days=0, hours=0):
    return (_now() - timedelta(days=days, hours=hours)).isoformat()


PROJECTS = ["platform", "research", "home-lab"]

# permalink -> note. Shape mirrors what the real tools return: recent_activity
# entities carry {permalink, title, created_at}; read_note adds frontmatter+content.
NOTES = {
    "platform/project/project-gateway-rollout": {
        "title": "project-gateway-rollout",
        "created_at": _ago(hours=2),
        "description": "API gateway canary at 25%; p99 steady at 41ms. Full rollout blocked on the retry-storm fix.",
        "tags": ["gateway", "rollout"],
        "content": (
            "Canary has been at 25% for three days with p99 steady at 41ms.\n\n"
            "**Blocked on:** the retry-storm fix in [[project-retry-budget]] — without it a\n"
            "single upstream blip amplifies 6x through the mesh.\n\n"
            "| Stage | Traffic | p99 |\n|-|-|-|\n| Canary | 25% | 41ms |\n| Baseline | 75% | 44ms |\n"
        ),
    },
    "platform/incident/incident-cache-stampede": {
        "title": "incident-cache-stampede",
        "created_at": _ago(days=1, hours=3),
        "description": "Cold-start cache stampede took the catalog API to 30s p99; fixed with jittered TTLs + request coalescing.",
        "tags": ["incident", "cache"],
        "content": (
            "Deploy restarted all catalog pods at once; every instance missed cache for the\n"
            "same hot keys and hammered the database in lockstep.\n\n"
            "**Fix:** jittered TTLs (±20%) plus request coalescing at the client. Follow-up in\n"
            "[[project-gateway-rollout]] to stagger pod restarts by default.\n"
        ),
    },
    "platform/feedback/feedback-migrations-in-pr": {
        "title": "feedback-migrations-in-pr",
        "created_at": _ago(days=1, hours=6),
        "description": "Ship schema migrations in the same PR as the code that needs them; separate PRs drift and deploy out of order.",
        "tags": ["feedback"],
        "content": (
            "Schema migrations belong in the same PR as the code that depends on them.\n\n"
            "**Why:** two separate PRs deploy out of order under auto-merge, and the app hits\n"
            "columns that don't exist yet. Happened twice during the catalog split.\n"
        ),
    },
    "platform/reference/reference-oncall-runbook": {
        "title": "reference-oncall-runbook",
        "created_at": _ago(days=3),
        "description": "Links to the on-call runbook, escalation ladder, and the dashboard folder that actually matters.",
        "tags": ["reference", "oncall"],
        "content": (
            "- Runbook: internal wiki → *Platform / On-call*\n"
            "- Escalation: page `platform-secondary` after 15 min\n"
            "- Dashboards: Grafana folder **Platform → Golden Signals**\n"
        ),
    },
    "research/project/project-embedding-eval": {
        "title": "project-embedding-eval",
        "created_at": _ago(hours=5),
        "description": "Small local embedding model wins on our corpus: 92% recall@10 at a third of the latency of the hosted API.",
        "tags": ["embeddings", "eval"],
        "content": (
            "Evaluated three embedding models on the internal notes corpus (8k docs).\n\n"
            "| Model | recall@10 | p50 latency |\n|-|-|-|\n"
            "| local small | 92% | 11ms |\n| hosted large | 94% | 38ms |\n| hosted small | 89% | 24ms |\n\n"
            "Local small is the sweet spot — see [[reference-eval-harness]] for the setup.\n"
        ),
    },
    "research/reference/reference-eval-harness": {
        "title": "reference-eval-harness",
        "created_at": _ago(days=2),
        "description": "Where the retrieval eval harness lives, how to add a corpus, and how the recall@k numbers are computed.",
        "tags": ["reference"],
        "content": (
            "Harness lives in the `retrieval-eval` repo. Add a corpus as JSONL under\n"
            "`corpora/`, then `make eval CORPUS=<name>`. recall@k uses graded relevance\n"
            "labels from the sampling sheet.\n"
        ),
    },
    "home-lab/project/project-nas-migration": {
        "title": "project-nas-migration",
        "created_at": _ago(days=1),
        "description": "Moved bulk storage to the new NAS; ZFS mirror healthy, nightly snapshots + weekly off-site sync in place.",
        "tags": ["nas", "storage"],
        "content": (
            "Bulk storage now on the new NAS: 2×12TB ZFS mirror, weekly scrub.\n\n"
            "Snapshots nightly at 03:00, off-site sync Sundays. Old NAS stays read-only\n"
            "for a month as a fallback.\n"
        ),
    },
    "home-lab/feedback/feedback-label-cables": {
        "title": "feedback-label-cables",
        "created_at": _ago(days=4),
        "description": "Label both ends of every cable at install time; tracing unlabeled runs through the rack wastes an evening each time.",
        "tags": ["feedback"],
        "content": (
            "Label both ends of every cable when it goes in, not later. Tracing an unlabeled\n"
            "run through the rack costs an evening every single time.\n"
        ),
    },
}


def _entity(p):
    n = NOTES[p]
    return {"permalink": p, "title": n["title"], "created_at": n["created_at"]}


async def call(name, **args):
    """Mock counterpart of mcp.session()'s `call` — same tools, canned answers."""
    if name == "list_memory_projects":
        return {"projects": [{"name": p} for p in PROJECTS]}
    if name == "recent_activity":
        proj = args.get("project")
        return {"results": [_entity(p) for p in NOTES if not proj or p.startswith(proj + "/")]}
    if name == "search_notes":
        q = (args.get("query") or "").lower()
        hits = [p for p, n in NOTES.items()
                if q in n["title"].lower() or q in n["description"].lower() or q in n["content"].lower()]
        return {"results": [
            {"permalink": p, "title": NOTES[p]["title"], "content": NOTES[p]["description"]}
            for p in hits[: args.get("page_size", 10)]
        ]}
    if name == "read_note":
        n = NOTES.get(args.get("identifier"))
        if not n:
            return {}
        return {
            "title": n["title"],
            "content": n["content"],
            "frontmatter": {
                "title": n["title"],
                "type": args["identifier"].split("/")[1],
                "description": n["description"],
                "tags": n["tags"],
            },
        }
    return {}
