#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USE_CASE="${1:-}"

AVAILABLE_USE_CASES=(
  "file_based_energy_pipeline"
  "protobuf_based_energy_pipeline"
  "germany-node"
  "portugal-node-sidecar"
  "portugal-node-integrated"
)

usage() {
  echo "Usage: $0 <use-case>"
  echo ""
  echo "Available use cases:"
  for uc in "${AVAILABLE_USE_CASES[@]}"; do
    echo "  - $uc"
  done
  echo ""
  echo "Example: $0 file_based_energy_pipeline"
}

if [ -z "$USE_CASE" ]; then
  usage
  exit 1
fi

# Validate use case
USE_CASE_DIR="$SCRIPT_DIR/use-cases/$USE_CASE"
if [ ! -d "$USE_CASE_DIR" ]; then
  echo "Error: Use case '$USE_CASE' not found at $USE_CASE_DIR"
  echo ""
  usage
  exit 1
fi

echo "=== AI-Effect Orchestrator Platform ==="
echo ""

# 1. Create shared Docker network
echo "Creating ai-effect-services network..."
docker network create ai-effect-services 2>/dev/null || true

# 2. Start orchestrator
echo "Starting orchestrator..."
docker compose -f "$SCRIPT_DIR/orchestrator/docker-compose.yml" up -d --build

# 3. Start use case services
echo "Starting use case: $USE_CASE..."
bash "$USE_CASE_DIR/start.sh"

# 4. Wait for orchestrator health
echo ""
echo "Waiting for orchestrator API..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:18000/health > /dev/null 2>&1; then
    echo "Orchestrator API is ready."
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "Warning: Orchestrator API did not become healthy within 30s."
    echo "Check logs: docker compose -f $SCRIPT_DIR/orchestrator/docker-compose.yml logs"
  fi
  sleep 1
done

# 5. Verify network
echo ""
echo "Containers on ai-effect-services network:"
docker network inspect ai-effect-services --format '{{range .Containers}}  - {{.Name}}{{println}}{{end}}' 2>/dev/null || true

echo ""
echo "=== Ready ==="
echo ""
echo "Next steps:"
echo "  Submit workflow:  cd use-cases/$USE_CASE && ./submit-workflow.sh"
echo "  Check API:        curl http://localhost:18000/health"
echo "  View logs:        docker compose -f orchestrator/docker-compose.yml logs -f worker"
echo "  Stop everything:  ./stop.sh $USE_CASE"
