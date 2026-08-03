#!/bin/bash
set -e

cd "$(dirname "$0")"

docker network create ai-effect-services 2>/dev/null || true

echo "Building and starting Germany node pipeline..."
docker compose up -d --build

echo ""
echo "Services:"
echo "  data-provider:    http://localhost:18091/health"
echo "  villas-chronics:  http://localhost:18092/health"
echo "  output-formatter: http://localhost:18093/health"
echo "  villas-node:      (internal, no exposed port)"
echo ""
echo "Check logs: docker compose logs -f"
