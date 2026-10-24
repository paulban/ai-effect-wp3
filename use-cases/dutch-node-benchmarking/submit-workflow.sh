#!/usr/bin/env bash
# Run a benchmark through the orchestrator. Kept at the use case root for
# convenience; the script itself lives beside the service.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/services/benchmarking/run_workflow.sh" "$@"
