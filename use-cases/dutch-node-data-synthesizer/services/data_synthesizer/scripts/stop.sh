#!/usr/bin/env bash
#
# Stop the synthetic grid service. The artifact volume is kept: a completed
# synthesis whose result nobody has fetched yet must survive a restart.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USE_CASE_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"

cd "$USE_CASE_DIR"
docker compose down
