# TEF Synthetic Data Pipeline — Sidecar Approach

Standalone adapter containers that run alongside the TEF services. The sidecar adapters expose the AI-Effect control interface and forward requests to the unmodified TEF service APIs.

## When to Use

- Cannot or do not want to modify TEF service code
- Isolation between AI-Effect interface and business logic required
- Containerized deployment preferred

## Architecture

```
Orchestrator Workers
        │
        ▼
┌─────────────────┐     ┌─────────────────┐
│  Sidecar Adapter │────▶│   TEF Service    │
│  (port 8080)     │     │  (original API)  │
└─────────────────┘     └─────────────────┘
```

Each sidecar adapter:
- Joins the `ai-effect-services` Docker network
- Implements the AI-Effect control interface (`/control/execute`, `/control/status`, `/control/output`)
- Calls the corresponding TEF service API to perform operations

## Pipeline

```
LoadData → ApplyFeatures → TrainModel → GenerateData
```

## Directory Structure

```
portugal-node-sidecar/
├── common/                    # Shared adapter modules
├── sidecar-adapters/          # Adapter container services
│   ├── docker-compose.yml
│   ├── data_provision/
│   ├── knowledge_store/
│   └── synthetic_data/
├── data/
│   └── real_data.csv          # Test data (not included, see RUNNING.md)
├── blueprint.json             # Pipeline topology
├── dockerinfo.json            # Sidecar adapter endpoints
├── docker-compose-tef.yml     # TEF services deployment
├── start.sh                   # Start sidecar adapters
├── stop.sh                    # Stop sidecar adapters
└── submit-workflow.sh         # Submit workflow to orchestrator
```

## Adapter Port Mapping

| Adapter | External Port | Internal Port | TEF Service |
|---------|--------------|---------------|-------------|
| data-provision-adapter | 18103 | 8080 | Data Provision (8001) |
| knowledge-store-adapter | 18101 | 8080 | Knowledge Store (8002) |
| synthetic-data-adapter | 18102 | 8080 | Synthetic Data (8003) |

## Common Module

The `common/` directory contains shared adapter code:

- **task_manager.py** — Thread-safe task state manager for tracking async operations
- **control_interface.py** — FastAPI control endpoints and application creation
- **tef_operations.py** — Pre-built TEF operation handlers (LoadData, ApplyFeatures, TrainModel, GenerateData)

## Control Interface

All adapters expose the standard AI-Effect control interface:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/control/execute` | POST | Start an operation |
| `/control/status/{task_id}` | GET | Check task status |
| `/control/output/{task_id}` | GET | Retrieve task output (returns a DataReference) |
| `/control/data/{task_id}` | GET | Serve raw data — optional, data-plane convenience (not called by the orchestrator) |
| `/health` | GET | Health check |

The orchestrator only calls `execute`, `status`, `output`, and `health`.
`/control/data` is a service-to-service endpoint: it is one possible target for
an `http` DataReference, but a service may serve data any other way instead.

## Running

See [RUNNING.md](RUNNING.md) for step-by-step instructions.

## Related

- [portugal-node-integrated](../portugal-node-integrated/) — Integrated approach (embed control interface directly in TEF services)
