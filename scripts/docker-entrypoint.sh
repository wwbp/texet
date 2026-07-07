#!/bin/sh
set -eu

# Only the api service runs migrations. Worker containers set SKIP_MIGRATIONS
# so they don't race the api on `alembic upgrade head` at startup.
if [ "${SKIP_MIGRATIONS:-}" = "true" ]; then
    echo "Skipping migrations (SKIP_MIGRATIONS=true)."
else
    echo "Running migrations..."
    alembic upgrade head
fi

exec "$@"
