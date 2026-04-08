#!/bin/bash
# Delft Node benchmark workflow submission

set -e

# Ensure jq is available (winget installs it as 'jqlang')
if ! command -v jq &>/dev/null && command -v jqlang &>/dev/null; then
    jq() { jqlang "$@"; }
fi

ORCHESTRATOR_URL="http://localhost:18000"
SERVICE_URL="http://localhost:8004"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
POLL_INTERVAL=3
MAX_POLLS=120

ALGO_FILE="$SCRIPT_DIR/algorithms/algorithm_template.py"
ALGO_SOURCE=$(cat "$ALGO_FILE")
ALGO_B64=$(echo -n "$ALGO_SOURCE" | base64 -w0 2>/dev/null || echo -n "$ALGO_SOURCE" | base64)

INPUT_JSON=$(cat <<EOF
{
  "benchmark": {
    "max_steps": 100,
    "kpis": ["survival", "violations", "latency"],
    "scenarios": [
      {
        "env_name": "l2rpn_case14_sandbox",
        "time_series_ids": [0]
      }
    ]
  },
  "algorithm": {
    "source_b64": "$ALGO_B64"
  }
}
EOF
)

INPUT_B64=$(echo -n "$INPUT_JSON" | base64 -w0 2>/dev/null || echo -n "$INPUT_JSON" | base64)

echo -n "Orchestrator ($ORCHESTRATOR_URL)... "
if curl -sf "$ORCHESTRATOR_URL/health" > /dev/null 2>&1; then
  echo "OK"
else
  echo "UNREACHABLE"
  exit 1
fi

echo -n "Benchmark service ($SERVICE_URL)... "
if curl -sf "$SERVICE_URL/health" > /dev/null 2>&1; then
  echo "OK"
else
  echo "UNREACHABLE"
  exit 1
fi

BLUEPRINT=$(cat "$SCRIPT_DIR/blueprint.json")
DOCKERINFO=$(cat "$SCRIPT_DIR/dockerinfo.json")

PAYLOAD=$(cat <<EOF
{
  "blueprint": $BLUEPRINT,
  "dockerinfo": $DOCKERINFO,
  "inputs": [{
    "protocol": "inline",
    "uri": "$INPUT_B64",
    "format": "json"
  }]
}
EOF
)

RESPONSE=$(curl -s -X POST "$ORCHESTRATOR_URL/workflows" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"
WORKFLOW_ID=$(echo "$RESPONSE" | jq -r '.workflow_id' 2>/dev/null)

if [ -z "$WORKFLOW_ID" ] || [ "$WORKFLOW_ID" = "null" ]; then
  echo "Failed to create workflow"
  exit 1
fi

echo "Workflow ID: $WORKFLOW_ID"

POLL_COUNT=0
while [ $POLL_COUNT -lt $MAX_POLLS ]; do
  STATUS_RESPONSE=$(curl -s "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID")
  STATUS=$(echo "$STATUS_RESPONSE" | jq -r '.status' 2>/dev/null)

  case "$STATUS" in
    completed|COMPLETED)
      echo "Workflow completed"
      echo "$STATUS_RESPONSE" | jq '.' 2>/dev/null || echo "$STATUS_RESPONSE"
      break
      ;;
    failed|FAILED|error|ERROR)
      echo "Workflow failed"
      echo "$STATUS_RESPONSE" | jq '.' 2>/dev/null || echo "$STATUS_RESPONSE"
      exit 1
      ;;
    *)
      echo -n "."
      sleep $POLL_INTERVAL
      POLL_COUNT=$((POLL_COUNT + 1))
      ;;
  esac
done

if [ $POLL_COUNT -ge $MAX_POLLS ]; then
  echo "Timed out waiting for workflow completion"
  exit 1
fi

echo ""
echo "Task outputs:"
TASKS_RESPONSE=$(curl -s "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID/tasks")
echo "$TASKS_RESPONSE" | jq '.' 2>/dev/null || echo "$TASKS_RESPONSE"
