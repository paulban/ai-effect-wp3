# Running the Germany Node VILLASnode Pipeline

Step-by-step instructions for running the chronics generation pipeline with the AI-Effect orchestrator.

## Prerequisites

- Docker and Docker Compose installed
- Orchestrator running (see `orchestrator/` directory)
- Ports available: 18091-18093 (adapter services), 18000 (orchestrator)
- Input data present in `data/` directory (load and sgen CSVs + grid model JSON)

## Step 1: Create Docker Network

The orchestrator and services communicate over a shared Docker network. The `start.sh` scripts auto-create it, but you can also create it manually:

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

## Step 3: Start Germany Node Services

```bash
cd use-cases/germany-node
docker compose up -d --build
```

This starts 4 containers:
- `germany-node-data-provider-1` (port 18091)
- `germany-node-villas-chronics-1` (port 18092)
- `germany-node-villas-node-1` (VILLASnode, idle)
- `germany-node-output-formatter-1` (port 18093)

Verify:
```bash
docker compose ps
```

## Step 4: Verify Health

```bash
curl -s http://localhost:18091/health | jq .
curl -s http://localhost:18092/health | jq .
curl -s http://localhost:18093/health | jq .
```

All should return `{"status":"ok"}`.

VILLASnode does not have a health endpoint — verify it's running with:
```bash
docker compose logs villas-node
```

## Step 5: Generate Export Files (if needed)

If `export/blueprint.json` and `export/dockerinfo.json` don't exist yet:

```bash
cd ../..  # back to ai-effect-wp3 root
python scripts/onboarding-export-generator.py \
  use-cases/germany-node \
  use-cases/germany-node/export
```

This generates dockerinfo with Docker DNS service names and internal port 8080, which works on the shared `ai-effect-services` network.

For local testing without the shared network, add `--local` to use `host.docker.internal` with host port mappings instead.

## Step 6: Submit Workflow

```bash
cd use-cases/germany-node
./submit-workflow.sh
```

Or manually:

```bash
curl -s -X POST http://localhost:18000/workflows \
  -H "Content-Type: application/json" \
  -d "{
  \"blueprint\": $(cat export/blueprint.json),
  \"dockerinfo\": $(cat export/dockerinfo.json)
}" | jq .
```

Save the returned `workflow_id`.

## Step 7: Monitor Workflow

```bash
# Check workflow status
curl -s http://localhost:18000/workflows/<workflow_id> | jq .

# Check individual tasks
curl -s http://localhost:18000/workflows/<workflow_id>/tasks | jq '.tasks[] | {node_key, status, error}'
```

Expected progression:
1. `data_provider:ProvideData` — complete (copies data to shared volume)
2. `villas_chronics:GenerateChronics` — running → complete (VILLASnode processing)
3. `output_formatter:FormatOutput` — complete (validates output)

## Step 8: Verify Results

```bash
# Check output files on shared volume
docker compose exec data-provider ls /shared/<workflow_id>/final_output/

# Check validation summary
docker compose exec data-provider cat /shared/<workflow_id>/final_output/summary.json | jq .
```

Expected output files:
- `load_p.csv` — Active power load profiles
- `load_q.csv` — Reactive power load profiles
- `prod_p.csv` — Active power generation profiles
- `prod_q.csv` — Reactive power generation profiles
- `prod_v.csv` — Voltage setpoints for generators
- `summary.json` — Validation report

## Testing Individual Services

### Test data_provider

```bash
curl -s -X POST http://localhost:18091/control/execute \
  -H "Content-Type: application/json" \
  -d '{"method":"ProvideData","workflow_id":"test-001","task_id":"t1","inputs":[]}' | jq .
```

Verify:
```bash
docker compose exec data-provider ls /shared/test-001/
docker compose exec data-provider cat /shared/test-001/manifest.json | python3 -m json.tool
```

### Test villas_chronics

```bash
curl -s -X POST http://localhost:18092/control/execute \
  -H "Content-Type: application/json" \
  -d '{"method":"GenerateChronics","workflow_id":"test-001","task_id":"t2",
       "inputs":[{"protocol":"file","uri":"/shared/test-001","format":"json"}]}' | jq .
```

This returns `{"status":"running","task_id":"t2"}`. Poll for completion:
```bash
curl -s http://localhost:18092/control/status/t2 | jq .
```

Once complete, get output:
```bash
curl -s http://localhost:18092/control/output/t2 | jq .
```

### Test output_formatter

```bash
curl -s -X POST http://localhost:18093/control/execute \
  -H "Content-Type: application/json" \
  -d '{"method":"FormatOutput","workflow_id":"test-001","task_id":"t3",
       "inputs":[{"protocol":"file","uri":"/shared/test-001/chronics_output","format":"csv"}]}' | jq .
```

## Troubleshooting

### VILLASnode Container Exits

If VILLASnode exits immediately, check the command in docker-compose.yml. It should be:
```yaml
command: node
```

Running `node` with no config starts VILLASnode idle with its REST API active. Commands like `node --help` will exit after printing help.

### VILLASnode Config Errors

Check VILLASnode logs for config parsing errors:
```bash
docker compose logs villas-node --tail 50
```

Common issues:
- **`Load index missing in grid mapping`** — The `uris` field should only list one load and one sgen trigger file, not all files. The hook globs directories independently.
- **`Failed to decode request payload`** — VILLASnode expects `{"config": "/path/to/file.conf"}`, not inline config content.

### Timeout Waiting for Output

If villas_chronics times out waiting for output files:
1. Check VILLASnode logs: `docker compose logs villas-node`
2. Verify input data exists: `docker compose exec villas-chronics ls /shared/<workflow_id>/loads/`
3. Increase timeout: set `POLL_TIMEOUT` environment variable in docker-compose.yml

### Previous Workflow Blocking

VILLASnode is single-tenant — `POST /api/v2/restart` kills the previous config. If a previous workflow's task is still polling, wait for its timeout before submitting a new workflow.

### Network Connectivity

Verify all containers are on the shared network:
```bash
docker network inspect ai-effect-services --format '{{range .Containers}}{{.Name}} {{end}}'
```

### Viewing Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f villas-chronics

# Orchestrator workers
cd ../../orchestrator && docker compose logs -f worker
```

## Cleanup

```bash
# Stop services
cd use-cases/germany-node
docker compose down

# Stop orchestrator
cd ../../orchestrator
docker compose down

# Remove shared volume data
docker volume rm germany-node_shared-data
```
