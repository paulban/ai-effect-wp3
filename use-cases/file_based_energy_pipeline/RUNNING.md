# Running the File-Based Energy Pipeline

Step-by-step instructions for running the file-based energy pipeline with the AI-Effect orchestrator.

## Prerequisites

- Docker and Docker Compose installed
- Ports available: 18081-18084 (pipeline services), 18000 (orchestrator)

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
cd use-cases/file_based_energy_pipeline
./start.sh
```

This starts 4 containers:
- `input-provider` (port 18081)
- `data-generator` (port 18082)
- `data-analyzer` (port 18083)
- `report-generator` (port 18084)

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

## Step 6: View Results

```bash
# Check generated data files
docker compose exec data-generator ls /app/data/

# View worker logs
cd ../../orchestrator && docker compose logs -f worker
```

## Cleanup

```bash
cd use-cases/file_based_energy_pipeline
./stop.sh
```

Or use the convenience script from the project root:
```bash
./stop.sh file_based_energy_pipeline
```
