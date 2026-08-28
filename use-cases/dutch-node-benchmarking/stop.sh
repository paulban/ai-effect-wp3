#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Stopping Dutch node benchmarking pipeline..."
docker compose down

echo "Done."
