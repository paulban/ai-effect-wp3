#!/bin/bash
# Danish Node - build and start workflow services.
set -e

cd "$(dirname "$0")"

docker network create ai-effect-services 2>/dev/null || true

echo "Building and starting Danish Node workflow services..."
docker compose up -d --build --remove-orphans

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:1.5b}"
if ! docker exec dk-ollama ollama show "$OLLAMA_MODEL" >/dev/null 2>&1; then
	echo "Downloading local Ollama model: $OLLAMA_MODEL"
	docker exec dk-ollama ollama pull "$OLLAMA_MODEL"
fi

echo ""
echo "Services (host-mapped for debugging; the orchestrator uses Docker DNS on :8080):"
echo "  config-assistant:       http://localhost:18101/health"
echo "  topology-generator:     http://localhost:18102/health"
echo "  draw-topology:          http://localhost:18103/health"
echo "  web-ui:                 http://localhost:18104"
echo ""
echo "Check logs: docker compose logs -f"
