# Running Example — Integrated Approach

A complete run-through following [RUNNING.md](RUNNING.md), tested 2026-02-24.

## Step 1: Create a Test Directory

```bash
mkdir -p use-cases-testing/portugal-node-test1
cp -r /home/amir/code/aieffect/amir/mocks/tef-services/data_provision use-cases-testing/portugal-node-test1/
cp -r /home/amir/code/aieffect/amir/mocks/tef-services/knowledge_store use-cases-testing/portugal-node-test1/
cp -r /home/amir/code/aieffect/amir/mocks/tef-services/synthetic_data_generation use-cases-testing/portugal-node-test1/
cp -r use-cases/portugal-node-integrated/* use-cases-testing/portugal-node-test1/
cd use-cases-testing/portugal-node-test1
```

## Step 2: Copy Test Data

```bash
cp synthetic_data_generation/real_data.csv data/real_data.csv
```

## Step 3: Start Services

```bash
$ ./start.sh
Building and starting TEF services with integrated adapters...
 Container tef-clickhouse  Created
 Container tef-knowledge-store  Created
 Container tef-synthetic-data  Created
 Container tef-data-provision  Created
 Container tef-clickhouse  Started
 Container tef-clickhouse  Healthy
 Container tef-data-provision  Started
 Container tef-knowledge-store  Started
 Container tef-synthetic-data  Started

Services:
  data-provision:     http://localhost:8001/health
  knowledge-store:    http://localhost:8002/health
  synthetic-data:     http://localhost:8003/health

Check logs: docker compose logs -f
```

```bash
$ docker ps --filter "name=tef-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
NAMES                 STATUS                    PORTS
tef-data-provision    Up 10 seconds (healthy)   0.0.0.0:8001->600/tcp
tef-synthetic-data    Up 16 seconds             0.0.0.0:8003->600/tcp
tef-clickhouse        Up 16 seconds (healthy)   0.0.0.0:8123->8123/tcp, 0.0.0.0:9000->9000/tcp
tef-knowledge-store   Up 16 seconds             0.0.0.0:8002->8000/tcp
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

## Step 5: Submit Workflow

```bash
$ cd ../use-cases-testing/portugal-node-test1
$ ./submit-workflow.sh
Submitting Portugal node workflow (integrated) to orchestrator at http://localhost:18000...
Workflow submitted!
Workflow ID: wf-138dfd6fdf25

Pipeline: LoadData -> ApplyFeatures -> TrainModel -> GenerateData

To check status:
  curl http://localhost:18000/workflows/wf-138dfd6fdf25 | jq .

To watch worker logs:
  cd ../../orchestrator && docker compose logs -f worker
```

## Step 6: Monitor & View Results

```bash
$ curl -s http://localhost:18000/workflows/wf-138dfd6fdf25/tasks | jq '.tasks[] | {node_key, status}'
```

```json
{ "node_key": "data_loader:LoadData", "status": "completed" }
{ "node_key": "feature_engineer:ApplyFeatures", "status": "completed" }
{ "node_key": "model_trainer:TrainModel", "status": "completed" }
{ "node_key": "data_generator:GenerateData", "status": "completed" }
```

## Step 7: Verify Results

```bash
$ TASK_ID=$(curl -s http://localhost:18000/workflows/wf-138dfd6fdf25/tasks | jq -r '.tasks[] | select(.node_key == "data_generator:GenerateData") | .task_id')
$ curl -s "http://localhost:8003/control/data/$TASK_ID" | head -5
```

```csv
timestamp,RelativeHumidity_ref0_D0,Temperature_ref0_D0,WindDirection_ref0_D0,WindDirection:100_ref0_D0,WindSpeed_ref0_D0,WindSpeed:100_ref0_D0,Wind_MWh,hour,example
2020-09-20T00:00:00.000000+0000,81.48113,13.81179,173.39241,201.7293,11.450495,14.6168785,290.36664,12.100169,1
2020-09-20T00:30:00.000000+0000,81.481255,13.754776,172.69603,197.2528,11.4626,14.963556,290.61145,12.400293,1
2020-09-20T01:00:00.000000+0000,81.415375,13.765625,173.28244,199.3378,11.53276,14.919239,288.43506,12.431031,1
2020-09-20T01:30:00.000000+0000,81.40804,13.777561,175.33281,199.02481,11.568141,14.938282,289.32367,12.4184675,1
```

Synthetic wind energy data generated successfully.
