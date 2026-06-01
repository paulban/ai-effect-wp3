#!/bin/bash
# Delft Node benchmark workflow submission

set -e

# Resolve optional JSON CLI tooling
JQ_BIN=""
if command -v jq &>/dev/null; then
  JQ_BIN="jq"
elif command -v jqlang &>/dev/null; then
  JQ_BIN="jqlang"
fi

json_pretty_print() {
  local payload="$1"
  if [ -n "$JQ_BIN" ]; then
    echo "$payload" | "$JQ_BIN" '.' 2>/dev/null || echo "$payload"
    return
  fi

  if command -v python3 &>/dev/null; then
    printf "%s" "$payload" | python3 -c 'import json,sys
s=sys.stdin.read()
try:
    print(json.dumps(json.loads(s), indent=2))
except Exception:
    print(s)'
    return
  fi

  if command -v python &>/dev/null; then
    printf "%s" "$payload" | python -c 'import json,sys
s=sys.stdin.read()
try:
    print(json.dumps(json.loads(s), indent=2))
except Exception:
    print(s)'
    return
  fi

  echo "$payload"
}

json_get_field() {
  local payload="$1"
  local field="$2"

  if [ -n "$JQ_BIN" ]; then
    echo "$payload" | "$JQ_BIN" -r ".$field // empty" 2>/dev/null
    return
  fi

  if command -v python3 &>/dev/null; then
    printf "%s" "$payload" | python3 -c "import json,sys; d=json.load(sys.stdin); v=d.get('$field',''); print(v if isinstance(v,(str,int,float,bool)) else '')" 2>/dev/null
    return
  fi

  if command -v python &>/dev/null; then
    printf "%s" "$payload" | python -c "import json,sys; d=json.load(sys.stdin); v=d.get('$field',''); print(v if isinstance(v,(str,int,float,bool)) else '')" 2>/dev/null
    return
  fi

  echo "$payload" | sed -n "s/.*\"$field\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -n 1
}

ORCHESTRATOR_URL="http://localhost:18000"
SYNTH_SERVICE_URL="http://localhost:8003"
SERVICE_URL="http://localhost:8004"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
POLL_INTERVAL=3
MAX_POLLS=120

ALGO_FILE="$SCRIPT_DIR/algorithms/algorithm_template.py"
ALGO_SOURCE=$(cat "$ALGO_FILE")
ALGO_B64=$(echo -n "$ALGO_SOURCE" | base64 -w0 2>/dev/null || echo -n "$ALGO_SOURCE" | base64)

INPUT_JSON=$(cat <<EOF
{
  "seed": 42,
  "loading_level": "M",
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

echo -n "Synthetic data service ($SYNTH_SERVICE_URL)... "
if curl -sf "$SYNTH_SERVICE_URL/health" > /dev/null 2>&1; then
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

json_pretty_print "$RESPONSE"
WORKFLOW_ID=$(json_get_field "$RESPONSE" "workflow_id")

if [ -z "$WORKFLOW_ID" ] || [ "$WORKFLOW_ID" = "null" ]; then
  echo "Failed to create workflow"
  exit 1
fi

echo "Workflow ID: $WORKFLOW_ID"

POLL_COUNT=0
while [ $POLL_COUNT -lt $MAX_POLLS ]; do
  STATUS_RESPONSE=$(curl -s "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID")
  STATUS=$(json_get_field "$STATUS_RESPONSE" "status")

  case "$STATUS" in
    completed|COMPLETED)
      echo "Workflow completed"
      json_pretty_print "$STATUS_RESPONSE"
      break
      ;;
    failed|FAILED|error|ERROR)
      echo "Workflow failed"
      json_pretty_print "$STATUS_RESPONSE"
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
json_pretty_print "$TASKS_RESPONSE"
