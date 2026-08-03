#!/bin/bash
set -e

API_URL="${API_URL:-http://localhost:18000}"

echo "Submitting workflow to orchestrator at $API_URL..."
RESPONSE=$(curl -s -X POST "$API_URL/workflows" \
  -H "Content-Type: application/json" \
  -d '{
  "blueprint": {
    "name": "File Based Energy Pipeline",
    "pipeline_id": "file-energy",
    "creation_date": "2025-01-01",
    "type": "pipeline-topology/v2",
    "version": "2.0",
    "nodes": [
      {
        "container_name": "input-provider",
        "proto_uri": "input_provider.proto",
        "image": "input-provider:latest",
        "node_type": "MLModel",
        "operation_signature_list": [{
          "operation_signature": {
            "operation_name": "GetConfiguration",
            "input_message_name": "Request",
            "output_message_name": "Response"
          },
          "connected_to": [{
            "container_name": "data-generator",
            "operation_signature": {
              "operation_name": "GenerateData"
            }
          }]
        }]
      },
      {
        "container_name": "data-generator",
        "proto_uri": "data_generator.proto",
        "image": "data-generator:latest",
        "node_type": "MLModel",
        "operation_signature_list": [{
          "operation_signature": {
            "operation_name": "GenerateData",
            "input_message_name": "Request",
            "output_message_name": "Response"
          },
          "connected_to": [{
            "container_name": "data-analyzer",
            "operation_signature": {
              "operation_name": "AnalyzeData"
            }
          }]
        }]
      },
      {
        "container_name": "data-analyzer",
        "proto_uri": "data_analyzer.proto",
        "image": "data-analyzer:latest",
        "node_type": "MLModel",
        "operation_signature_list": [{
          "operation_signature": {
            "operation_name": "AnalyzeData",
            "input_message_name": "Request",
            "output_message_name": "Response"
          },
          "connected_to": [{
            "container_name": "report-generator",
            "operation_signature": {
              "operation_name": "GenerateReport"
            }
          }]
        }]
      },
      {
        "container_name": "report-generator",
        "proto_uri": "report_generator.proto",
        "image": "report-generator:latest",
        "node_type": "MLModel",
        "operation_signature_list": [{
          "operation_signature": {
            "operation_name": "GenerateReport",
            "input_message_name": "Request",
            "output_message_name": "Response"
          },
          "connected_to": []
        }]
      }
    ]
  },
  "dockerinfo": {
    "docker_info_list": [
      {"container_name": "input-provider", "ip_address": "input-provider", "port": "8080"},
      {"container_name": "data-generator", "ip_address": "data-generator", "port": "8080"},
      {"container_name": "data-analyzer", "ip_address": "data-analyzer", "port": "8080"},
      {"container_name": "report-generator", "ip_address": "report-generator", "port": "8080"}
    ]
  }
}')

WORKFLOW_ID=$(echo "$RESPONSE" | jq -r .workflow_id)

if [ "$WORKFLOW_ID" = "null" ] || [ -z "$WORKFLOW_ID" ]; then
    echo "Failed to submit workflow:"
    echo "$RESPONSE" | jq .
    exit 1
fi

echo "Workflow submitted!"
echo "Workflow ID: $WORKFLOW_ID"
echo ""
echo "Workers will automatically process this workflow."
echo ""
echo "To check status:"
echo "  curl $API_URL/workflows/$WORKFLOW_ID | jq ."
echo ""
echo "To watch worker logs:"
echo "  cd ../../orchestrator && docker compose logs -f worker"
