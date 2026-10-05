#!/bin/bash
# Danish Node - stop workflow services.
set -e

cd "$(dirname "$0")"

echo "Stopping Danish Node workflow services..."
docker compose down

echo "Done."
