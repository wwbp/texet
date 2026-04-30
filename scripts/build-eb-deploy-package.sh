#!/usr/bin/env bash
set -euo pipefail

output_path="${1:-deploy-eb.zip}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

cd "${repo_root}"
git rev-parse --is-inside-work-tree >/dev/null

include_paths=(
  Dockerfile
  pyproject.toml
  uv.lock
  alembic.ini
  app
  alembic
  scripts
)

optional_paths=(
  .platform
  .ebextensions
  Procfile
)

for path in "${optional_paths[@]}"; do
  if git cat-file -e "HEAD:${path}" 2>/dev/null; then
    include_paths+=("${path}")
  fi
done

rm -f "${output_path}"
git archive --format=zip --output "${output_path}" HEAD "${include_paths[@]}"
du -h "${output_path}"
