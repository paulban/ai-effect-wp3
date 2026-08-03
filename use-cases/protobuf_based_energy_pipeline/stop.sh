#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Stopping protobuf-based energy pipeline..."
docker compose down

echo "Done."
