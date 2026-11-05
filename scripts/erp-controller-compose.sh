#!/usr/bin/env bash
# Use this wrapper for future ERP recreations so Controller configuration stays
# mounted. Images, routing, ERP data, and all other settings remain in ERP Compose.
set -euo pipefail
# The host's Compose release can panic with concurrent map writes while
# recreating services that share dependents. Serialize convergence by default.
export COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-1}"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec docker compose --project-name erp --project-directory /deploy/erp \
  -f /deploy/erp/compose.yml \
  -f "$script_dir/../deployment/erp-controller.override.yml" "$@"
