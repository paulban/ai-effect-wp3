# TEF Synthetic Data Pipeline — Integrated Approach

Embeds the AI-Effect control interface directly into the TEF services. The Dockerfiles are pre-modified and ready — you just need to copy in the TEF service source code and add a small code snippet to each `main.py`.

## When to Use

- Minimal additional infrastructure required
- Services are already FastAPI applications
- Direct integration preferred over network hops

## Architecture

```
Orchestrator Workers
        │
        ▼
┌─────────────────────────┐
│   TEF Service            │
│   (original API)         │
│   + /control/* endpoints │
│   (port 600/8000)        │
└─────────────────────────┘
```

Each TEF service:
- Joins the `ai-effect-services` Docker network
- Includes the AI-Effect `common` module
- Exposes both the original TEF API and the AI-Effect control interface

## Pipeline

```
LoadData → ApplyFeatures → TrainModel → GenerateData
```

## Directory Structure

```
portugal-node-integrated/
├── common/                        # AI-Effect adapter modules
├── data/
│   └── real_data.csv              # Test data (not included, see RUNNING.md)
├── data_provision/
│   └── Dockerfile                 # Pre-modified (copy TEF source here)
├── knowledge_store/
│   └── Dockerfile                 # Pre-modified (copy TEF source here)
├── synthetic_data_generation/
│   └── Dockerfile                 # Pre-modified (copy TEF source here)
├── blueprint.json                 # Pipeline topology
├── dockerinfo.json                # Service endpoints (Docker DNS)
├── docker-compose.yml             # All services deployment
├── start.sh                       # Build & start services
├── stop.sh                        # Stop services
└── submit-workflow.sh             # Submit workflow to orchestrator
```

## Common Module

The `common/` directory contains shared adapter code:

- **task_manager.py** — Thread-safe task state manager for tracking async operations
- **control_interface.py** — FastAPI control endpoints and application creation
- **tef_operations.py** — Pre-built TEF operation handlers (LoadData, ApplyFeatures, TrainModel, GenerateData)

## Control Interface

All services expose the standard AI-Effect control interface:

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

- [portugal-node-sidecar](../portugal-node-sidecar/) — Sidecar approach (no TEF code modifications needed)
