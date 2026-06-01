#!/bin/bash
set -e

docker network create ai-effect-services >/dev/null 2>&1 || true
docker compose -f docker-compose-all.yml up -d --build
