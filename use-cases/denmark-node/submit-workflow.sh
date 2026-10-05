#!/bin/bash
# Submit the Danish Node workflow to the AI-EFFECT orchestrator.
#
# Prerequisites:
#   1. Orchestrator running (ai-effect-wp3/orchestrator: docker compose up -d)
#   2. ./start.sh has built and started config-assistant, topology-generator,
#      draw-topology, web-ui, and Ollama.
#   3. ./generate-export.sh has produced current export/blueprint.json and
#      export/dockerinfo.json.
set -e

cd "$(dirname "$0")"

API_URL="${API_URL:-http://localhost:18000}"
EXPORT_DIR="${EXPORT_DIR:-export}"

if [ ! -f "$EXPORT_DIR/blueprint.json" ]; then
    echo "No $EXPORT_DIR/blueprint.json — run ./generate-export.sh first." >&2
    exit 1
fi

BLUEPRINT=$(cat "$EXPORT_DIR/blueprint.json")
DOCKERINFO=$(cat "$EXPORT_DIR/dockerinfo.json")

# Initial input for the config assistant, sent inline as base64 JSON. Set
# PROMPT to send free text instead, parsed by the Ollama-backed config
# assistant into a run config, e.g.:
#   PROMPT="Use the CHP at 310-D as supply, feed the dumpload at 716-D." ./submit-workflow.sh
if [ -n "${PROMPT:-}" ]; then
    INPUTS_JSON=$(jq -n --arg prompt "$PROMPT" '$prompt')
else
    INPUTS_JSON='{
  "run_name": "chp_dumploads",
  "supplies": ["310-D::CHP"],
  "loads": ["716-D::Dumpload", "330-D::Heat dumpload"],
  "allow_hx": true,
  "allow_bypass": false,
  "max_extra_pipes": 1,
  "load_topology": "any"
}'
fi
INPUTS_B64=$(echo -n "$INPUTS_JSON" | base64 -w 0)

PAYLOAD=$(jq -n --argjson blueprint "$BLUEPRINT" --argjson dockerinfo "$DOCKERINFO" --arg inputs_b64 "$INPUTS_B64" \
  '{"blueprint": $blueprint, "dockerinfo": $dockerinfo, "inputs": [{"protocol": "inline", "uri": $inputs_b64, "format": "json"}]}')

echo "Submitting Danish Node workflow to orchestrator at $API_URL..."
RESPONSE=$(curl -s -X POST "$API_URL/workflows" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

WORKFLOW_ID=$(echo "$RESPONSE" | jq -r .workflow_id)

if [ "$WORKFLOW_ID" = "null" ] || [ -z "$WORKFLOW_ID" ]; then
    echo "Failed to submit workflow:"
    echo "$RESPONSE" | jq .
    exit 1
fi

echo "Workflow submitted!"
echo "Workflow ID: $WORKFLOW_ID"
echo ""
echo "Pipeline: BuildRunConfig -> GenerateTopology -> RenderTopologies"
echo ""
echo "Check status:"
echo "  curl -s $API_URL/workflows/$WORKFLOW_ID | jq ."
echo "  curl -s $API_URL/workflows/$WORKFLOW_ID/tasks | jq ."
echo ""

echo "Waiting for topology generation and rendering..."
# Prompt-based configs go through Ollama before topology generation and rendering.
WAIT_ITERATIONS="${WAIT_ITERATIONS:-30}"
for _ in $(seq 1 "$WAIT_ITERATIONS"); do
  TASKS=$(curl --fail --silent --show-error "$API_URL/workflows/$WORKFLOW_ID/tasks")
  STATUS=$(echo "$TASKS" | jq -r '[.tasks[].status] | if any(. == "failed") then "failed" elif all(. == "completed") then "completed" else "running" end')
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    break
  fi
  sleep 1
done

if [ "$STATUS" != "completed" ]; then
  echo "Workflow did not complete:" >&2
  echo "$TASKS" | jq . >&2
  exit 1
fi

TOPOLOGY_URI=$(echo "$TASKS" | jq -r '.tasks[] | select(.node_key | startswith("topology_generator1:")) | .output_refs[0].uri')
RENDER_URI=$(echo "$TASKS" | jq -r '.tasks[] | select(.node_key | startswith("draw_topology1:")) | .output_refs[0].uri')
TOPOLOGY_FILE="output/${WORKFLOW_ID}_topology.json"
RENDER_FILE="output/${WORKFLOW_ID}_rendered_topologies.json"
mkdir -p output
docker exec dk-topology-generator cat "$TOPOLOGY_URI" > "$TOPOLOGY_FILE"
docker exec dk-draw-topology cat "$RENDER_URI" > "$RENDER_FILE"

echo "Workflow completed. Topology: $TOPOLOGY_FILE"
jq '{topology_count, generated_by, networks: [.networks[] | {id, signature, supplies, loads, pipes}]}' "$TOPOLOGY_FILE"
echo "Rendered topology references: $RENDER_FILE"
jq '{topology_count, drawing_count, generated_by, drawings}' "$RENDER_FILE"
