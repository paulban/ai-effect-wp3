#!/bin/bash
set -e

cd "$(dirname "$0")"

docker network create ai-effect-services 2>/dev/null || true

echo "Building and starting Dutch node benchmarking pipeline..."
docker compose up --build -d

echo ""
echo "Services:"
echo "  benchmarking:   http://localhost:8004"
echo ""
echo "Check logs: docker compose logs -f"
