#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Stopping TEF services with integrated adapters..."
docker compose down

echo "Done."
