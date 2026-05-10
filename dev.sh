#!/usr/bin/env bash
# Local dev runner — venv-based replacement for the docker stack.
# Same port (1851) and same data dir (./data, copied from the docker volume).
# Live reload via gunicorn --reload (single worker so APScheduler doesn't double-fire).

set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
    echo "Creating .venv..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip setuptools wheel
    .venv/bin/pip install -r requirements-dev.txt
fi

if [[ -f .env.local ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env.local
    set +a
fi

export DATABASE_URL="${DATABASE_URL:-sqlite:///$(pwd)/data/newspaparr.db}"
export FLASK_APP="${FLASK_APP:-wsgi:app}"
export PYTHONUNBUFFERED=1

mkdir -p data/logs

exec .venv/bin/gunicorn \
    --bind 0.0.0.0:1851 \
    --workers 1 \
    --threads 2 \
    --timeout 600 \
    --reload \
    --access-logfile - \
    --error-logfile - \
    wsgi:app
