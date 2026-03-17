#!/bin/bash
# Delft Node – Synthetic Power Grid workflow via AI-Effect Orchestrator
 
set +e

# Ensure jq is available (winget installs it as 'jqlang')
if ! command -v jq &>/dev/null && command -v jqlang &>/dev/null; then
    jq() { jqlang "$@"; }
fi

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
 
test_endpoint() {
    local name=$1
    local url=$2
    echo "Testing: $name"
    echo "URL: $url"
 
    RESPONSE=$(curl -s -w "\n%{http_code}" "$url" 2>&1)
    HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
    BODY=$(echo "$RESPONSE" | sed '$d')
 
    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        echo "Status: $HTTP_CODE"
        echo "Response:"
        echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
        return 0
    else
        echo "Status: $HTTP_CODE (ERROR)"
        echo "Response:"
        echo "$BODY"
        return 1
    fi
}
 
echo "Testing: Orchestrator Health"
echo "URL: $ORCHESTRATOR_URL/health"
if ! test_endpoint "Orchestrator" "$ORCHESTRATOR_URL/health"; then
    echo "Start the orchestrator first:  cd orchestrator && docker compose up -d"
    exit 1
fi
echo ""
 
echo "Testing: Synthetic Power Grid Service"
echo "URL: $SERVICE_URL/health"
if ! test_endpoint "Synthetic Power Grid" "$SERVICE_URL/health"; then
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
 
echo "Testing: Submit Workflow"
echo "URL: $ORCHESTRATOR_URL/workflows"
echo "Method: POST"
echo ""
 
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$ORCHESTRATOR_URL/workflows" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" 2>&1)
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')
 
echo "Status: $HTTP_CODE"
if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
    echo "Response:"
    echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
else
    echo "ERROR Response:"
    echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
    exit 1
fi
echo ""
 
WORKFLOW_ID=$(echo "$BODY" | jq -r '.workflow_id' 2>/dev/null)
 
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
    echo "Polling workflow status (attempt $((POLL_COUNT + 1))/$MAX_POLLS)..."
    
    STATUS_RESPONSE=$(curl -s -w "\n%{http_code}" "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID" 2>&1)
    HTTP_CODE=$(echo "$STATUS_RESPONSE" | tail -n1)
    BODY=$(echo "$STATUS_RESPONSE" | sed '$d')
    
    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        STATUS=$(echo "$BODY" | jq -r '.status' 2>/dev/null)
        
        case "$STATUS" in
            completed|COMPLETED)
                echo ""
                echo "Workflow completed!"
                echo ""
                echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
                break
                ;;
            failed|FAILED|error|ERROR)
                echo ""
                echo "Workflow failed!"
                echo ""
                echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
                exit 1
                ;;
            running|RUNNING|pending|PENDING)
                echo "Status: $STATUS"
                ;;
            *)
                echo "Unknown status: $STATUS"
                ;;
        esac
    else
        echo "HTTP Error $HTTP_CODE while polling workflow status:"
        echo "$BODY"
        exit 1
    fi
    
    sleep $POLL_INTERVAL
    POLL_COUNT=$((POLL_COUNT + 1))
done
 
if [ $POLL_COUNT -ge $MAX_POLLS ]; then
    echo ""
    echo "Timeout after $((MAX_POLLS * POLL_INTERVAL)) seconds"
    echo "Last status check:"
    echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
    exit 1
fi
echo ""
 
# STEP 4: Retrieve results
echo "=========================================="
echo "STEP 4: Retrieving workflow results"
echo "=========================================="
echo ""
 
echo "Testing: Get Workflow Tasks"
echo "URL: $ORCHESTRATOR_URL/workflows/$WORKFLOW_ID/tasks"
echo ""
 
TASKS_RESPONSE=$(curl -s -w "\n%{http_code}" "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID/tasks" 2>&1)
HTTP_CODE=$(echo "$TASKS_RESPONSE" | tail -n1)
BODY=$(echo "$TASKS_RESPONSE" | sed '$d')
 
echo "Status: $HTTP_CODE"
if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
    echo "Tasks:"
    echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
else
    echo "ERROR retrieving tasks:"
    echo "$BODY"
    exit 1
fi
echo ""
 
# Summary
echo "=========================================="
echo "Workflow complete"
echo "=========================================="
echo ""
echo "Workflow ID:  $WORKFLOW_ID"
echo "Status:       $STATUS"
echo "Orchestrator: $ORCHESTRATOR_URL"
echo "Service:      $SERVICE_URL"
echo ""
 