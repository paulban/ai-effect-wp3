#!/bin/bash
set -e

cd "$(dirname "$0")"

docker network create ai-effect-services 2>/dev/null || true

echo "Building and starting Dutch node data synthesizer pipeline..."
docker compose up --build -d

echo ""
echo "Services:"
echo "  data_synthesizer:   http://localhost:8003"
echo ""
echo "Check logs: docker compose logs -f"
