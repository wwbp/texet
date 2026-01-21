#!/bin/sh
set -eu

should_run_migrations=0
case "${1:-}" in
  uvicorn|gunicorn)
    should_run_migrations=1
    ;;
esac

if [ "${RUN_MIGRATIONS:-1}" = "1" ] && [ "$should_run_migrations" = "1" ]; then
  echo "Running migrations..."
  alembic upgrade head
fi

exec "$@"
