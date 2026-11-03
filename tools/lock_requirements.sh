#!/usr/bin/env bash
# Re-lock the serverless job environments against the env-6 base (requirements/serverless-env6-base.txt).
# Base packages that a top-level pin must change are dropped from the constraint set (listed per env).
set -euo pipefail
cd "$(dirname "$0")/.."
lock () {  # $1 = name, $2 = space-separated base packages released from the constraint
  local c; c=$(mktemp)
  grep -v '^#' requirements/serverless-env6-base.txt | grep -v -E "^($(echo "$2" | tr ' ' '|'))==" > "$c"
  uv pip compile "requirements/$1.in" -c "$c" --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
     --index-url https://pypi.org/simple --no-header --annotation-style line -o "requirements/$1.lock.txt" -q
  sed -i "1i # LOCKED by tools/lock_requirements.sh from requirements/$1.in against serverless env-6 base (Python 3.12, linux x86_64).\n# Released from base constraint: ${2:-none}. Do not edit by hand." "requirements/$1.lock.txt"
  sed -i -E -e 's|, -c /tmp/[^ ,]+||' -e 's|-c /tmp/[^ ,]+, ||' -e 's|[[:space:]]+# via -c /tmp/[^ ]+$||' "requirements/$1.lock.txt"
  rm -f "$c"
}
lock job-ml-env6 "statsmodels"
lock canari-pilot-env6 "${CANARI_RELEASE:-numpy statsmodels matplotlib tqdm pytest optuna pandas fire ray pytagi}"
