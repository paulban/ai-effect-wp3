#!/bin/bash
set -e

cd "$(dirname "$0")"

docker network create ai-effect-services 2>/dev/null || true

echo "Building and starting Portugal node sidecar adapters..."
docker compose -f sidecar-adapters/docker-compose.yml up -d --build

echo ""
echo "Services:"
echo "  knowledge-store-adapter:  http://localhost:18101/health"
echo "  synthetic-data-adapter:   http://localhost:18102/health"
echo "  data-provision-adapter:   http://localhost:18103/health"
echo ""
echo "Check logs: docker compose -f sidecar-adapters/docker-compose.yml logs -f"
