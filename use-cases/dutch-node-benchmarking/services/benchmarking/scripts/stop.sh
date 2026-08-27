#!/usr/bin/env bash
#
# Stop the benchmark service. The artifact volume is kept: a completed benchmark
# whose result nobody has fetched yet must survive a restart.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USE_CASE_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Only interpolated, never used by `down` — but the compose file requires it.
export NODE_PUBLIC_BASE_URL="${NODE_PUBLIC_BASE_URL:-http://localhost:8444}"

cd "$USE_CASE_DIR"
docker compose down
