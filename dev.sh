#!/usr/bin/env bash
# Local dev server with autoreload. Edits under app/ (incl. templates) apply live —
# no manual restart. Templates/static are re-read per request; --reload covers .py.
#
#   ./dev.sh                 # http://localhost:8200
#   PORT=9000 ./dev.sh       # override port
#
# Config (MCP_URL, BM_PROJECT, APP_TITLE, APP_USER) comes from .env — copy
# .env.example to .env first. A real env var still wins over .env if set.
set -euo pipefail
cd "$(dirname "$0")"
exec uvicorn app.main:app --reload --reload-dir app --port "${PORT:-8200}"
