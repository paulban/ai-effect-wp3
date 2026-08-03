#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Stopping Portugal node sidecar adapters..."
docker compose -f sidecar-adapters/docker-compose.yml down

echo "Done."
