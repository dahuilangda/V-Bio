#!/usr/bin/env bash
set -euo pipefail

API_URL="${1:-http://127.0.0.1:5000}"
TOKEN="${BOLTZ_API_TOKEN:-${2:-}}"

if [[ -z "${TOKEN}" ]]; then
  echo "Usage: BOLTZ_API_TOKEN=<token> $0 [api_url]"
  echo "   or: $0 [api_url] <token>"
  exit 1
fi

curl -sS -H "X-API-Token: ${TOKEN}" "${API_URL}/workers/capabilities" | python -m json.tool
