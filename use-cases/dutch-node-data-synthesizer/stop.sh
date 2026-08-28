#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Stopping Dutch node data synthesizer pipeline..."
docker compose down

echo "Done."
