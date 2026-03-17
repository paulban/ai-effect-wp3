#!/bin/bash
# Delft Node – Synthetic Power Grid workflow via AI-Effect Orchestrator

set -e

ORCHESTRATOR_URL="http://localhost:18000"
SERVICE_URL="http://localhost:8003"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
POLL_INTERVAL=3
MAX_POLLS=60

echo "=========================================="
echo "Delft Node – Synthetic Power Grid Pipeline"
echo "=========================================="
echo ""

# STEP 0: Pre-flight checks
echo "=========================================="
echo "STEP 0: Pre-flight checks"
echo "=========================================="
echo ""

echo -n "Orchestrator ($ORCHESTRATOR_URL)... "
if curl -sf "$ORCHESTRATOR_URL/health" > /dev/null 2>&1; then
    echo "OK"
else
    echo "UNREACHABLE"
    echo "Start the orchestrator first:  cd orchestrator && docker compose up -d"
    exit 1
fi

echo -n "Synthetic Power Grid service ($SERVICE_URL)... "
if curl -sf "$SERVICE_URL/health" > /dev/null 2>&1; then
    echo "OK"
else
    echo "UNREACHABLE"
    echo "Start the service first:  cd use-cases/delft_node && docker compose -f docker-compose-all.yml up -d --build"
    exit 1
fi
echo ""

# STEP 1: Prepare input
echo "=========================================="
echo "STEP 1: Preparing grid configuration"
echo "=========================================="
echo ""

INPUT_JSON='{"num_levels": 3, "seed": 42, "loading_level": "M"}'
INPUT_B64=$(echo -n "$INPUT_JSON" | base64 -w0 2>/dev/null || echo -n "$INPUT_JSON" | base64)

echo "Grid parameters:"
echo "  num_levels:    3"
echo "  seed:          42"
echo "  loading_level: M"
echo ""
echo "Base64-encoded input: ${INPUT_B64:0:40}..."
echo ""

# STEP 2: Submit workflow
echo "=========================================="
echo "STEP 2: Submitting workflow to orchestrator"
echo "=========================================="
echo ""

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

echo "POST $ORCHESTRATOR_URL/workflows"
echo ""

RESPONSE=$(curl -s -X POST "$ORCHESTRATOR_URL/workflows" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"
echo ""

WORKFLOW_ID=$(echo "$RESPONSE" | jq -r '.workflow_id' 2>/dev/null)

if [ -z "$WORKFLOW_ID" ] || [ "$WORKFLOW_ID" = "null" ]; then
    echo "Error: Failed to get workflow_id from response"
    exit 1
fi

echo "Workflow ID: $WORKFLOW_ID"
echo ""

# STEP 3: Poll for completion
echo "=========================================="
echo "STEP 3: Waiting for workflow completion"
echo "=========================================="
echo ""

POLL_COUNT=0
while [ $POLL_COUNT -lt $MAX_POLLS ]; do
    STATUS_RESPONSE=$(curl -s "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID")
    STATUS=$(echo "$STATUS_RESPONSE" | jq -r '.status' 2>/dev/null)

    case "$STATUS" in
        completed|COMPLETED)
            echo ""
            echo "Workflow completed!"
            echo ""
            echo "$STATUS_RESPONSE" | jq '.' 2>/dev/null || echo "$STATUS_RESPONSE"
            break
            ;;
        failed|FAILED|error|ERROR)
            echo ""
            echo "Workflow failed!"
            echo ""
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
    echo ""
    echo "Timeout after $((MAX_POLLS * POLL_INTERVAL)) seconds"
    echo "Last status: $STATUS"
    echo "$STATUS_RESPONSE" | jq '.' 2>/dev/null || echo "$STATUS_RESPONSE"
    exit 1
fi
echo ""

# STEP 4: Retrieve results
echo "=========================================="
echo "STEP 4: Retrieving workflow results"
echo "=========================================="
echo ""

TASKS_RESPONSE=$(curl -s "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID/tasks")
echo "Tasks:"
echo "$TASKS_RESPONSE" | jq '.' 2>/dev/null || echo "$TASKS_RESPONSE"
echo ""

# Summary
echo "=========================================="
echo "Workflow complete"
echo "=========================================="
echo ""
echo "Workflow ID:  $WORKFLOW_ID"
echo "Status:       completed"
echo "Orchestrator: $ORCHESTRATOR_URL"
echo "Service:      $SERVICE_URL"
echo ""
