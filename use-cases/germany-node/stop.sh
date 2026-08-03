#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Stopping Germany node pipeline..."
docker compose down

echo "Done."
