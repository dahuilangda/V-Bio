#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VBIO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SUPABASE_DIR="${VBIO_DIR}/supabase-lite"
INIT_SQL="${SUPABASE_DIR}/init/init.sql"

if [[ ! -f "${INIT_SQL}" ]]; then
  echo "[db:migrate] init.sql not found: ${INIT_SQL}" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[db:migrate] docker is not installed or not in PATH." >&2
  exit 1
fi

echo "[db:migrate] ensuring supabase-lite services are up..."
cd "${SUPABASE_DIR}"
docker compose up -d db rest >/dev/null

echo "[db:migrate] applying idempotent schema upgrades from init.sql..."
docker compose exec -T db env PGOPTIONS='-c client_min_messages=warning' \
  psql -q -v ON_ERROR_STOP=1 -U postgres -d postgres -f /docker-entrypoint-initdb.d/01-init.sql >/dev/null

echo "[db:migrate] reloading PostgREST schema cache..."
docker compose exec -T db env PGOPTIONS='-c client_min_messages=warning' \
  psql -q -U postgres -d postgres -c "NOTIFY pgrst, 'reload schema';" >/dev/null

echo "[db:migrate] verifying api_tokens permission columns..."
docker compose exec -T db env PGOPTIONS='-c client_min_messages=warning' psql -q -U postgres -d postgres -c \
  "select column_name from information_schema.columns where table_schema='public' and table_name='api_tokens' and column_name in ('project_id','allow_submit','allow_delete','allow_cancel','token_plain') order by column_name;"

echo "[db:migrate] done."
