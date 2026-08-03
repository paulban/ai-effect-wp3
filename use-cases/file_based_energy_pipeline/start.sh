#!/bin/bash
set -e

cd "$(dirname "$0")"

docker network create ai-effect-services 2>/dev/null || true

mkdir -p data

echo "Building and starting file-based energy pipeline..."
docker compose up --build -d

echo ""
echo "Services:"
echo "  input-provider:   http://localhost:18081"
echo "  data-generator:   http://localhost:18082"
echo "  data-analyzer:    http://localhost:18083"
echo "  report-generator: http://localhost:18084"
echo ""
echo "Check logs: docker compose logs -f"