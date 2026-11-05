#!/usr/bin/env bash
# Install the repository-owned overlay for default `docker compose` invocations.
# Explicit `-f compose.yml` skips defaults: use erp-controller-compose.sh instead.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_file=$(realpath "$script_dir/../deployment/erp-controller.override.yml")
target_file=/deploy/erp/compose.override.yml
if [[ -e "$target_file" || -L "$target_file" ]]; then
  if [[ ! -L "$target_file" || "$(readlink -f "$target_file")" != "$source_file" ]]; then
    echo "Refusing to replace an existing ERP override" >&2
    exit 1
  fi
else
  ln -s -- "$source_file" "$target_file"
fi
docker compose --project-directory /deploy/erp config --quiet
echo "ERP default Compose now includes the repository-managed Controller overlay"
