#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USE_CASE="${1:-}"

echo "=== Stopping AI-Effect Services ==="
echo ""

# Stop use case if specified
if [ -n "$USE_CASE" ]; then
  USE_CASE_DIR="$SCRIPT_DIR/use-cases/$USE_CASE"
  if [ -d "$USE_CASE_DIR" ]; then
    echo "Stopping use case: $USE_CASE..."
    bash "$USE_CASE_DIR/stop.sh"
  else
    echo "Warning: Use case directory not found: $USE_CASE_DIR"
  fi
fi

# Stop orchestrator
echo "Stopping orchestrator..."
docker compose -f "$SCRIPT_DIR/orchestrator/docker-compose.yml" down

# Do NOT remove the ai-effect-services network — other services may still use it

echo ""
echo "Done."
