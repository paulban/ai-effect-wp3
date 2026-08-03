# Running the Portugal Node Pipeline — Sidecar Approach

Step-by-step instructions for running the pipeline using sidecar adapters alongside unmodified Portugal node services.

## Prerequisites

- Docker and Docker Compose installed
- Ports available: 8001-8003 (services), 8123 (ClickHouse), 18000 (orchestrator), 18101-18103 (sidecar adapters)
- Portugal node services source code (`data_provision/`, `knowledge_store/`, `synthetic_data_generation/`)

## Step 1: Create a Test Directory

Create a working directory in `use-cases-testing/` and copy the Portugal node service source into it:

```bash
mkdir -p use-cases-testing/portugal-node-test1
cp -r <path-to-portugal-node-services>/data_provision use-cases-testing/portugal-node-test1/
cp -r <path-to-portugal-node-services>/knowledge_store use-cases-testing/portugal-node-test1/
cp -r <path-to-portugal-node-services>/synthetic_data_generation use-cases-testing/portugal-node-test1/
```

Then copy the variant files on top — this adds the `common/` module, sidecar adapters, config files, and scripts:

```bash
cp -r use-cases/portugal-node-sidecar/* use-cases-testing/portugal-node-test1/
```

All remaining steps are run from inside this test directory:

```bash
cd use-cases-testing/portugal-node-test1
```

## Step 2: Copy Test Data

The `data_provision` service needs a CSV data file. If the services include `real_data.csv` (e.g. inside `synthetic_data_generation/`), copy it to the `data/` directory:

```bash
cp synthetic_data_generation/real_data.csv data/real_data.csv
```

## Step 3: Start Portugal Node Services

```bash
docker compose -f docker-compose-tef.yml up -d --build
```

Verify services are running:
```bash
docker ps --filter "name=tef-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

## Step 4: Start Orchestrator

```bash
cd ../../orchestrator
docker compose up -d
```

## Step 5: Start Sidecar Adapters

```bash
./start.sh
```

Verify adapters:
```bash
curl -s http://localhost:18103/health | jq .
curl -s http://localhost:18101/health | jq .
curl -s http://localhost:18102/health | jq .
```

All should return `{"status":"ok"}`.

## Step 6: Submit Workflow

```bash
./submit-workflow.sh
```

Save the returned `workflow_id`.

## Step 7: Monitor & View Results

```bash
# Check workflow status
curl -s http://localhost:18000/workflows/<workflow_id> | jq .

# Check individual tasks
curl -s http://localhost:18000/workflows/<workflow_id>/tasks | jq '.tasks[] | {node_key, status, error}'

# Watch worker logs
cd ../../orchestrator && docker compose logs -f worker
```

Expected progression:
1. `data_loader:LoadData` — completed
2. `feature_engineer:ApplyFeatures` — completed
3. `model_trainer:TrainModel` — running, then completed
4. `data_generator:GenerateData` — completed

## Step 8: Verify Results

```bash
# Get GenerateData task_id
TASK_ID=$(curl -s http://localhost:18000/workflows/<workflow_id>/tasks | jq -r '.tasks[] | select(.node_key == "data_generator:GenerateData") | .task_id')

# Fetch generated data via sidecar adapter
curl -s "http://localhost:18102/control/data/$TASK_ID" | head -5
```

## Cleanup

```bash
# Stop sidecar adapters
./stop.sh

# Stop services
docker compose -f docker-compose-tef.yml down

# Stop orchestrator
cd ../../orchestrator && docker compose down
```

Or use the convenience script from the project root:
```bash
./stop.sh portugal-node-sidecar
```

## Troubleshooting

### Network Connectivity Issues

Verify containers are on the shared network:
```bash
docker network inspect ai-effect-services --format '{{range .Containers}}{{.Name}} {{end}}'
```

### File Not Found in Sidecar Container

Sidecar adapters run in separate containers and do not have access to files in the service containers. Mount required data files in the sidecar docker-compose:

```yaml
volumes:
  - ./data/real_data.csv:/app/real_data.csv:ro
```

### Container Crash Loop (Python 3.9 Type Hints)

If a container fails with `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`, install `eval_type_backport` in the container.

### Code Changes Not Reflected in Container

```bash
# Force rebuild without cache
docker compose -f docker-compose-tef.yml build --no-cache <service-name>

# Recreate container
docker compose -f docker-compose-tef.yml up -d --force-recreate <service-name>
```
