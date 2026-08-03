# Running the Protobuf-Based Energy Pipeline

Step-by-step instructions for running the protobuf-based energy pipeline with the AI-Effect orchestrator.

This pipeline demonstrates:
- Orchestrator controls execution **order** via HTTP
- Services exchange **data** directly via gRPC/protobuf

## Prerequisites

- Docker and Docker Compose installed
- Ports available: 18081-18084 (HTTP), 50051-50054 (gRPC), 18000 (orchestrator)

## Step 1: Create Docker Network

All services and orchestrator workers communicate over a shared Docker network. The start scripts auto-create it, but you can also create it manually:

```bash
docker network create ai-effect-services
```

## Step 2: Start the Orchestrator

```bash
cd orchestrator
docker compose up -d
```

Verify:
```bash
curl -s http://localhost:18000/health | jq .
```

## Step 3: Start Pipeline Services

```bash
cd use-cases/protobuf_based_energy_pipeline
./start.sh
```

This starts 4 containers with both HTTP control and gRPC data interfaces:
- `input-provider` (HTTP: 18081)
- `data-generator` (HTTP: 18082, gRPC: 50052)
- `data-analyzer` (HTTP: 18083, gRPC: 50053)
- `report-generator` (HTTP: 18084, gRPC: 50054)

Verify all containers are on the shared network:
```bash
docker network inspect ai-effect-services --format '{{range .Containers}}{{.Name}} {{end}}'
```

## Step 4: Submit Workflow

```bash
./submit-workflow.sh
```

Save the returned `workflow_id`.

## Step 5: Monitor Workflow

```bash
# Check workflow status
curl -s http://localhost:18000/workflows/<workflow_id> | jq .

# Check individual tasks
curl -s http://localhost:18000/workflows/<workflow_id>/tasks | jq '.tasks[] | {node_key, status, error}'
```

Expected progression:
1. `input-provider:GetConfiguration` - complete
2. `data-generator:GenerateData` - complete
3. `data-analyzer:AnalyzeData` - complete
4. `report-generator:GenerateReport` - complete

Data flows between services via gRPC:
```
input-provider --[grpc]--> data-generator --[grpc]--> data-analyzer --[grpc]--> report-generator
```

## Step 6: View Results

Since data flows via gRPC between services (not files), results are visible in the service and worker logs:

```bash
# View the final report (logged by report-generator)
docker compose logs report-generator

# View all service logs
docker compose logs

# View orchestrator worker logs
cd ../../orchestrator && docker compose logs worker
```

The report-generator service logs the full report including:
- Executive Summary (record count, anomalies, efficiency)
- Household Analysis
- Anomaly Breakdown
- Efficiency Distribution
- Recommendations

## Cleanup

```bash
cd use-cases/protobuf_based_energy_pipeline
./stop.sh
```

Or use the convenience script from the project root:
```bash
./stop.sh protobuf_based_energy_pipeline
```
