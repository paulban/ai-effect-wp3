#!/bin/bash
set -e

cd "$(dirname "$0")"

docker network create ai-effect-services 2>/dev/null || true

echo "Building and starting TEF services with integrated adapters..."
docker compose up -d --build

echo ""
echo "Services:"
echo "  data-provision:     http://localhost:8001/health"
echo "  knowledge-store:    http://localhost:8002/health"
echo "  synthetic-data:     http://localhost:8003/health"
echo ""
echo "Check logs: docker compose logs -f"
