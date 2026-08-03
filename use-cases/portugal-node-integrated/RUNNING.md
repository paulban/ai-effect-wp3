# Running the Portugal Node Pipeline — Integrated Approach

Step-by-step instructions for running the pipeline with the control interface embedded directly in the Portugal node services.

## Prerequisites

- Docker and Docker Compose installed
- Ports available: 8001-8003 (services), 8123 (ClickHouse), 18000 (orchestrator)
- Portugal node services source code (`data_provision/`, `knowledge_store/`, `synthetic_data_generation/`)

## Step 1: Create a Test Directory

Create a working directory in `use-cases-testing/` and copy the Portugal node service source into it:

```bash
mkdir -p use-cases-testing/portugal-node-test1
cp -r <path-to-portugal-node-services>/data_provision use-cases-testing/portugal-node-test1/
cp -r <path-to-portugal-node-services>/knowledge_store use-cases-testing/portugal-node-test1/
cp -r <path-to-portugal-node-services>/synthetic_data_generation use-cases-testing/portugal-node-test1/
```

Then copy the variant files on top — this adds the `common/` module, config files, scripts, and overwrites the Dockerfiles and `main.py` files with pre-modified versions that include the AI-Effect control interface:

```bash
cp -r use-cases/portugal-node-integrated/* use-cases-testing/portugal-node-test1/
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

## Step 3: Start Services

```bash
./start.sh
```

Verify services:
```bash
docker ps --filter "name=tef-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

Expected:
```
NAMES                STATUS         PORTS
tef-synthetic-data   Up X minutes   0.0.0.0:8003->600/tcp
tef-knowledge-store  Up X minutes   0.0.0.0:8002->8000/tcp
tef-data-provision   Up X minutes   0.0.0.0:8001->600/tcp
tef-clickhouse       Up X minutes   0.0.0.0:8123->8123/tcp
```

## Step 4: Start Orchestrator

```bash
cd ../../orchestrator
docker compose up -d
```

## Step 5: Submit Workflow

```bash
./submit-workflow.sh
```

Save the returned `workflow_id`.

## Step 6: Monitor & View Results

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

## Step 7: Verify Results

```bash
# Get GenerateData task_id
TASK_ID=$(curl -s http://localhost:18000/workflows/<workflow_id>/tasks | jq -r '.tasks[] | select(.node_key == "data_generator:GenerateData") | .task_id')

# Fetch generated data
curl -s "http://localhost:8003/control/data/$TASK_ID" | head -5
```

## Cleanup

```bash
# Stop services
./stop.sh

# Stop orchestrator
cd ../../orchestrator && docker compose down
```

Or use the convenience script from the project root:
```bash
./stop.sh portugal-node-integrated
```

## Troubleshooting

### Container Crash Loop (Python 3.9 Type Hints)

If a container fails with `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`:

The `str | None` syntax requires Python 3.10+. The common module includes `from __future__ import annotations` for compatibility, but Pydantic still evaluates type hints at runtime.

The `synthetic_data_generation/Dockerfile` already includes `eval_type_backport` for this reason. If you need Python 3.9 for the other services too, add:

```dockerfile
RUN pip install eval_type_backport
```

### Code Changes Not Reflected in Container

```bash
# Force rebuild without cache
docker compose build --no-cache <service-name>

# Recreate container
docker compose up -d --force-recreate <service-name>
```

### Network Connectivity Issues

Verify containers are on the shared network:
```bash
docker network inspect ai-effect-services --format '{{range .Containers}}{{.Name}} {{end}}'
```

### HTTP 404 on /control/execute

Verify the endpoint path is correct:
```bash
curl -s http://localhost:8001/openapi.json | jq '.paths | keys'
```

If endpoints show `/control/control/execute` (doubled prefix), the router prefix is applied twice. The `create_control_router()` function returns a router without prefix — add the prefix only when including the router:

```python
app.include_router(create_control_router(handlers), prefix="/control")
```
