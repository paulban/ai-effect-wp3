#!/bin/bash
set -e

cd "$(dirname "$0")"

docker network create ai-effect-services 2>/dev/null || true

echo "Building and starting protobuf-based energy pipeline..."
docker compose up --build -d

echo ""
echo "Services:"
echo "  input-provider:   http://localhost:18181"
echo "  data-generator:   http://localhost:18182 (gRPC: 50152)"
echo "  data-analyzer:    http://localhost:18183 (gRPC: 50153)"
echo "  report-generator: http://localhost:18184 (gRPC: 50154)"
echo ""
echo "Check logs: docker compose logs -f"
