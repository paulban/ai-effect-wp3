#!/usr/bin/env bash
# Run a human-AI interaction session through the orchestrator. Kept at the use
# case root for convenience; the script itself lives beside the service.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/services/human_ai_interaction_testing/run_workflow.sh" "$@"
