#!/usr/bin/env bash
#
# Start the benchmark service.
#
# The compose file lives at the use case's root, not beside this script: the
# build context is use-cases/ so the shared control-plane package can be copied
# into the image. This script used to name a docker-compose-all.yml in the
# service directory, which the split into per-use-case packages removed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USE_CASE_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"

docker network create ai-effect-services >/dev/null 2>&1 || true

cd "$USE_CASE_DIR"
docker compose up -d --build

echo
echo "benchmark-runner is up. The orchestrator's workers reach it at"
echo "benchmark-runner:8080 on the ai-effect-services network, and this"
echo "machine at http://localhost:8004 - where result references point."
echo "Submit a workflow with services/benchmarking/run_workflow.sh."
