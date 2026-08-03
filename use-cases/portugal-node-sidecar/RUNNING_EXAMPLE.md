# Running Example — Sidecar Approach

A complete run-through following [RUNNING.md](RUNNING.md), tested 2026-02-24.

## Step 1: Create a Test Directory

```bash
mkdir -p use-cases-testing/portugal-node-test1
cp -r /home/amir/code/aieffect/amir/mocks/tef-services/data_provision use-cases-testing/portugal-node-test1/
cp -r /home/amir/code/aieffect/amir/mocks/tef-services/knowledge_store use-cases-testing/portugal-node-test1/
cp -r /home/amir/code/aieffect/amir/mocks/tef-services/synthetic_data_generation use-cases-testing/portugal-node-test1/
cp -r use-cases/portugal-node-sidecar/* use-cases-testing/portugal-node-test1/
cd use-cases-testing/portugal-node-test1
```

## Step 2: Copy Test Data

```bash
cp synthetic_data_generation/real_data.csv data/real_data.csv
```

## Step 3: Start Portugal Node Services

```bash
$ docker compose -f docker-compose-tef.yml up -d --build
 Container tef-clickhouse  Created
 Container tef-data-provision  Created
 Container tef-knowledge-store  Created
 Container tef-synthetic-data  Created
 Container tef-clickhouse  Started
 Container tef-clickhouse  Healthy
 Container tef-data-provision  Started
 Container tef-knowledge-store  Started
 Container tef-synthetic-data  Started
```

```bash
$ docker ps --filter "name=tef-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
NAMES                 STATUS                    PORTS
tef-data-provision    Up 10 seconds (healthy)   0.0.0.0:8001->600/tcp
tef-clickhouse        Up 16 seconds (healthy)   0.0.0.0:8123->8123/tcp, 0.0.0.0:9000->9000/tcp
tef-knowledge-store   Up 16 seconds             0.0.0.0:8002->8000/tcp
tef-synthetic-data    Up 16 seconds             0.0.0.0:8003->600/tcp
```

## Step 4: Start Orchestrator

```bash
$ cd ../../orchestrator && docker compose up -d
 Container orchestrator-redis-1    Started
 Container orchestrator-api-1      Started
 Container orchestrator-worker-1   Started
 Container orchestrator-worker-2   Started
 Container orchestrator-worker-3   Started
```

## Step 5: Start Sidecar Adapters

```bash
$ cd ../use-cases-testing/portugal-node-test1
$ ./start.sh
Starting sidecar adapters...
 Container sidecar-adapters-data-provision-adapter-1    Created
 Container sidecar-adapters-knowledge-store-adapter-1   Created
 Container sidecar-adapters-synthetic-data-adapter-1    Created
 Container sidecar-adapters-data-provision-adapter-1    Started
 Container sidecar-adapters-knowledge-store-adapter-1   Started
 Container sidecar-adapters-synthetic-data-adapter-1    Started
```

```bash
$ curl -s http://localhost:18103/health | jq .
{"status":"ok"}
$ curl -s http://localhost:18101/health | jq .
{"status":"ok"}
$ curl -s http://localhost:18102/health | jq .
{"status":"ok"}
```

## Step 6: Submit Workflow

```bash
$ ./submit-workflow.sh
Submitting Portugal node workflow (sidecar) to orchestrator at http://localhost:18000...
Workflow submitted!
Workflow ID: wf-ddb11ab1cdcf

Pipeline: LoadData -> ApplyFeatures -> TrainModel -> GenerateData

To check status:
  curl http://localhost:18000/workflows/wf-ddb11ab1cdcf | jq .

To watch worker logs:
  cd ../../orchestrator && docker compose logs -f worker
```

## Step 7: Monitor & View Results

```bash
$ curl -s http://localhost:18000/workflows/wf-ddb11ab1cdcf/tasks | jq '.tasks[] | {node_key, status}'
```

```json
{ "node_key": "data_loader:LoadData", "status": "completed" }
{ "node_key": "feature_engineer:ApplyFeatures", "status": "completed" }
{ "node_key": "model_trainer:TrainModel", "status": "completed" }
{ "node_key": "data_generator:GenerateData", "status": "completed" }
```

## Step 8: Verify Results

```bash
$ TASK_ID=$(curl -s http://localhost:18000/workflows/wf-ddb11ab1cdcf/tasks | jq -r '.tasks[] | select(.node_key == "data_generator:GenerateData") | .task_id')
$ curl -s "http://localhost:18102/control/data/$TASK_ID" | head -5
```

```csv
timestamp,RelativeHumidity_ref0_D0,Temperature_ref0_D0,WindDirection_ref0_D0,WindDirection:100_ref0_D0,WindSpeed_ref0_D0,WindSpeed:100_ref0_D0,Wind_MWh,hour,example
2020-09-20T00:00:00.000000+0000,80.85419,13.980739,174.73935,181.4875,10.746051,14.135067,307.349,11.481978,1
2020-09-20T00:30:00.000000+0000,81.0488,13.88326,175.83163,181.89548,10.679454,14.17596,309.2877,11.260116,1
2020-09-20T01:00:00.000000+0000,80.650215,13.934692,174.27887,182.85445,10.848622,14.08507,306.79764,11.523536,1
2020-09-20T01:30:00.000000+0000,80.6428,13.958521,171.56674,186.02371,10.750385,14.441452,305.67374,11.760746,1
```

Synthetic wind energy data generated successfully via sidecar adapters.
