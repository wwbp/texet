#!/bin/sh
set -eu

echo "Running migrations..."
alembic upgrade head

exec "$@"
