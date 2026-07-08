#!/bin/bash
set -euo pipefail

# Migrations: the api owns them. Separate worker containers (ECS) set
# SKIP_MIGRATIONS=true so they don't race the api on `alembic upgrade head`.
if [ "${SKIP_MIGRATIONS:-}" = "true" ]; then
    echo "Skipping migrations (SKIP_MIGRATIONS=true)."
else
    echo "Running migrations..."
    alembic upgrade head
fi

# RUN_WORKER_INLINE: run the reply worker in the same container as the web
# server. Used on single-container deployments (Elastic Beanstalk bot-prod)
# where there is no separate worker service. On ECS/local the api and worker
# run as separate services, so this stays unset there.
if [ "${RUN_WORKER_INLINE:-}" = "true" ]; then
    echo "Starting reply worker alongside the web server (RUN_WORKER_INLINE=true)..."
    python -m app.worker &
    worker_pid=$!
    "$@" &
    web_pid=$!
    # If either process exits, tear down the other and exit non-zero so the
    # platform restarts the whole container (rather than limping on with one).
    wait -n "$worker_pid" "$web_pid"
    echo "A managed process exited; shutting the container down for restart."
    kill "$worker_pid" "$web_pid" 2>/dev/null || true
    wait 2>/dev/null || true
    exit 1
fi

exec "$@"
