# AI-Effect Orchestrator Platform

A workflow orchestration platform for AI-Effect microservice pipelines. The orchestrator coordinates service execution via REST API and supports both gRPC (protobuf) and HTTP Control Interface communication patterns.

## Overview

The platform consists of:

- **Orchestrator** - REST API + worker processes that execute workflows
- **Service Templates** - Reference implementations for building compatible services
- **Use Cases** - Example pipelines and real-world integrations

## Project Structure

```
ai-effect-wp3/
├── orchestrator/                  # Workflow orchestrator
│   ├── src/
│   │   ├── api/                   # REST API (FastAPI)
│   │   ├── models/                # Data models
│   │   └── services/              # Core services
│   ├── docker-compose.yml         # Orchestrator deployment
│   └── tests/                     # Unit, integration, e2e tests
├── use-cases/
│   ├── templates/                 # Service templates
│   │   ├── file_based/            # HTTP control + file storage
│   │   └── protobuf_based/        # HTTP control + gRPC data
│   ├── file_based_energy_pipeline/    # Example pipeline
│   ├── protobuf_based_energy_pipeline/ # Example with gRPC
│   ├── portugal-node-sidecar/      # TEF integration (sidecar adapters)
│   ├── portugal-node-integrated/   # TEF integration (embedded adapters)
│   └── germany-node/             # VILLASnode chronics generation
├── scripts/                       # Build and generation tools
└── use-cases-testing/            # Generated deployment packages
```

## Quick Start

### Using the convenience script

The fastest way to get started — starts the shared network, orchestrator, and a use case:

```bash
./start.sh file_based_energy_pipeline
```

Then submit a workflow:
```bash
cd use-cases/file_based_energy_pipeline && ./submit-workflow.sh
```

Stop everything:
```bash
./stop.sh file_based_energy_pipeline
```

### Manual setup

#### 1. Create the shared network

All services and orchestrator workers communicate over the `ai-effect-services` Docker network:

```bash
docker network create ai-effect-services
```

#### 2. Start the Orchestrator

```bash
cd orchestrator
docker compose up -d
```

This starts:
- **Redis** - State management (port 16379)
- **API** - REST endpoint (port 18000)
- **Workers** - 3 worker replicas for task execution (joined to `ai-effect-services` network)

#### 3. Start a use case

```bash
cd use-cases/file_based_energy_pipeline
./start.sh
```

The start scripts auto-create the network if it doesn't exist.

#### 4. Submit a Workflow

```bash
./submit-workflow.sh
```

Or manually:
```bash
curl -s -X POST http://localhost:18000/workflows \
  -H "Content-Type: application/json" \
  -d '{
    "blueprint": {...},
    "dockerinfo": {...},
    "inputs": [{"protocol": "inline", "uri": "...", "format": "json"}]
  }' | jq .
```

#### 5. Monitor Progress

```bash
# Check workflow status
curl -s http://localhost:18000/workflows/{workflow_id} | jq .

# Check individual tasks
curl -s http://localhost:18000/workflows/{workflow_id}/tasks | jq .
```

## Networking

All services and orchestrator workers share a single Docker network called `ai-effect-services`. This allows:

- Orchestrator workers to reach services by **Docker DNS name** (e.g., `data-generator:8080`)
- Services to communicate directly with each other when needed (e.g., gRPC data exchange)
- No reliance on `host.docker.internal` or host-mapped ports for inter-service communication

Each use case's `docker-compose.yml` declares this as an external network:
```yaml
networks:
  default:
    name: ai-effect-services
    external: true
```

The network is auto-created by `start.sh` scripts. To verify all containers are connected:
```bash
docker network inspect ai-effect-services --format '{{range .Containers}}{{.Name}} {{end}}'
```

Services must implement the AI-Effect Control Interface. This is the only
contract the orchestrator depends on:

```
POST /control/execute        - Execute an operation
GET  /control/status/{id}    - Check task status
GET  /control/output/{id}    - Get task output
GET  /health                 - Health check
```

These are the endpoints the orchestrator actually calls. `/control/execute`
starts a task; `/control/status` and `/control/output` are polled for
asynchronous tasks; `/health` is for monitoring. The orchestrator never
transfers payloads itself — `/control/output` returns only a small
**DataReference** (`{protocol, uri, format}`) that points at the data.

**Serving the data is a service-side, data-plane concern — not part of the
orchestrator contract.** The `uri` in a DataReference may point anywhere the
producing service likes: its own HTTP endpoint, an object store, a presigned
URL, a gRPC address, or inline bytes. The orchestrator neither calls that URI
nor cares what is behind it; the *downstream service* resolves the reference.

As a convenience, the service templates ship a default HTTP data endpoint:

```
GET  /control/data/{id}      - Serve raw payload bytes (optional convenience)
```

This is **optional**. A service is free to omit it and instead return
`protocol: inline` (bytes embedded in the reference), `protocol: grpc` (its own
gRPC endpoint), or an `http`/`s3` URI pointing at any other location. Nothing
in the orchestrator changes either way.

## Architecture

### Orchestrator Components

```
┌─────────────────────────────────────────────────────────────┐
│                         Orchestrator                         │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────┐    ┌─────────┐    ┌─────────────────────────┐  │
│  │   API   │───▶│  Redis  │◀───│   Workers (x3)          │  │
│  │  :8000  │    │  :6379  │    │   - Execute tasks       │  │
│  └─────────┘    └─────────┘    │   - Call services       │  │
│                                │   - Update state        │  │
│                                └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                         Services                             │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐               │
│  │ Service A │  │ Service B │  │ Service C │               │
│  │  :8080    │  │  :8080    │  │  :8080    │               │
│  └───────────┘  └───────────┘  └───────────┘               │
└─────────────────────────────────────────────────────────────┘
```

### Communication Patterns

**HTTP Control Interface** (control plane — orchestrator ↔ service):
- Orchestrator communicates with services via HTTP
- Endpoints the orchestrator calls: `/control/execute`, `/control/status`, `/control/output`
- Carries only commands and small DataReferences, never payloads

**Data Exchange** (data plane — service ↔ service, orchestrator not involved):
- **HTTP URLs** - Services return URLs to data, downstream services fetch directly (the templates' `/control/data/{id}` is one such URL, but any URL works)
- **gRPC** - Services expose gRPC endpoints for direct data transfer
- **Inline** - Small data embedded as base64 in responses

## Service Templates

Templates for building AI-Effect compatible services:

| Template | Location | Use Case |
|----------|----------|----------|
| File Sequential | `use-cases/templates/file_based/sequential/` | Quick ops, shared storage |
| File Concurrent | `use-cases/templates/file_based/concurrent/` | Long-running, shared storage |
| Protobuf Sequential | `use-cases/templates/protobuf_based/sequential/` | Quick ops, gRPC data |
| Protobuf Concurrent | `use-cases/templates/protobuf_based/concurrent/` | Long-running, gRPC data |

See `use-cases/templates/README.md` for detailed documentation.

## Use Cases

### file_based_energy_pipeline

Four-service pipeline demonstrating file-based data exchange:

```
input_provider → data_generator → data_analyzer → report_generator
```

### protobuf_based_energy_pipeline

Same pipeline with gRPC data exchange between services.

### portugal-node (TEF Integration)

Real-world integration with third-party TEF services for synthetic data generation:

```
LoadData → ApplyFeatures → TrainModel → GenerateData
```

Available in two variants:
- **[portugal-node-sidecar](use-cases/portugal-node-sidecar/)** — Separate adapter containers alongside unmodified TEF services
- **[portugal-node-integrated](use-cases/portugal-node-integrated/)** — Embed control interface directly in TEF services

Both demonstrate HTTP URL reference data passing between services.

### germany-node (VILLASnode Chronics)

Converts pandapower timeseries data into Grid2Op chronics using VILLASnode:

```
ProvideData → GenerateChronics → FormatOutput
                    │
             VILLASnode (REST API)
```

Demonstrates:
- **Long-running sidecar** - VILLASnode controlled via REST API (not orchestrator-managed)
- **Async task handling** - Concurrent handler with progress polling
- **Config template** - Dynamic VILLASnode config generation per workflow
- **Shared volume** - File-based data exchange between services

See `use-cases/germany-node/README.md` for details.

## Configuration Files

### blueprint.json

Defines workflow topology:

```json
{
  "pipeline_id": "my-pipeline",
  "name": "My Pipeline",
  "nodes": [
    {
      "container_name": "my_service",
      "node_type": "DataSource",
      "operation_signature_list": [
        {
          "operation_signature": {
            "operation_name": "LoadData",
            "output_message_name": "LoadDataResponse"
          },
          "connected_to": [
            {
              "container_name": "next_service",
              "operation_signature": {
                "operation_name": "ProcessData"
              }
            }
          ]
        }
      ]
    }
  ]
}
```

### dockerinfo.json

Service network configuration:

```json
{
  "docker_info_list": [
    {
      "container_name": "service_a",
      "ip_address": "service-a",
      "port": "8080"
    }
  ]
}
```

## API Reference

### Submit Workflow

```
POST /workflows
```

Request:
```json
{
  "blueprint": {...},
  "dockerinfo": {...},
  "inputs": [
    {"protocol": "inline", "uri": "<base64>", "format": "json"}
  ],
  "services_api_key": "optional-bearer-token-for-services"
}
```

Response:
```json
{
  "workflow_id": "wf-abc123",
  "status": "running"
}
```

### Get Workflow Status

```
GET /workflows/{workflow_id}
```

### Get Workflow Tasks

```
GET /workflows/{workflow_id}/tasks
```

### Health Check

```
GET /health
```

## Authentication

Both the orchestrator API and services support optional bearer token authentication. Auth is **opt-in** — if no key is configured, everything works as before with no breaking changes.

### Orchestrator API

Set `ORCHESTRATOR_API_KEY` to require a bearer token on all orchestrator API calls (except `/health`):

```bash
ORCHESTRATOR_API_KEY=your-secret-key docker compose up -d
```

All requests must then include:
```
Authorization: Bearer your-secret-key
```

Without the env var set, the orchestrator API is open.

### Services

Set `SERVICE_API_KEY` to require a bearer token on all `/control/*` endpoints:

```bash
SERVICE_API_KEY=your-secret-key docker compose up -d
```

The `/health` endpoint remains unauthenticated for monitoring.

### End-to-end: authenticated workflow

When services require auth, pass `services_api_key` in the workflow submit request. The orchestrator stores it per-workflow and sends it as a bearer token on every service call:

```bash
curl -s -X POST http://localhost:18000/workflows \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-orchestrator-key" \
  -d '{
    "blueprint": {...},
    "dockerinfo": {...},
    "services_api_key": "your-service-key"
  }' | jq .
```

### Key generation

```bash
openssl rand -hex 32
```

### Summary

| What is protected | Env var | Where to set |
|---|---|---|
| Orchestrator API | `ORCHESTRATOR_API_KEY` | `orchestrator/docker-compose.yml` or host env |
| Service `/control/*` endpoints | `SERVICE_API_KEY` | use-case `docker-compose.yml` or host env |
| Orchestrator → Services calls | `services_api_key` in submit request | passed per-workflow by the API caller |

## Development

### Prerequisites

- Docker and Docker Compose
- Python 3.12+

### Running Tests

```bash
cd orchestrator
pytest tests/
```

### Building Services

Use the scripts to generate deployment packages:

**1. Generate build script** — Reads `docker-compose.yml` from a use case directory and generates a `build_and_tag.sh` script that builds all service images and tags them with `:latest` for export compatibility.

```bash
python scripts/build-script-generator.py use-cases/my_pipeline
```

**2. Create platform export** — Scans the `services/` directory for proto files, reads `connections.json` for pipeline topology, and generates a complete onboarding package: `blueprint.json`, `dockerinfo.json`, `generation_metadata.json`, and copies proto files into `microservice/`.

```bash
python scripts/onboarding-export-generator.py \
  use-cases/my_pipeline \
  use-cases-testing/my_pipeline
```

By default, `dockerinfo.json` uses docker-compose service names (which resolve via Docker DNS on the shared `ai-effect-services` network) and internal port 8080 (the HTTP control interface).

Use `--local` when services don't join the shared orchestrator network — it generates dockerinfo with `host.docker.internal` and host port mappings from `docker-compose.yml`, so orchestrator workers can reach services through the host:

```bash
python scripts/onboarding-export-generator.py \
  use-cases/my_pipeline \
  use-cases/my_pipeline/export \
  --local
```

**3. Generate docker-compose** — Reads `blueprint.json` and `dockerinfo.json` from an onboarding package and generates a `docker-compose.yml` with all pipeline services, port mappings, and networking. Optionally includes the orchestrator as a service.

```bash
python scripts/docker-compose-generator.py \
  use-cases-testing/my_pipeline

# Include orchestrator in the deployment
python scripts/docker-compose-generator.py \
  use-cases-testing/my_pipeline \
  --orchestrator-path orchestrator
```

## Third-Party Integration

To integrate existing services with AI-Effect:

1. **Choose approach**: Integrated (embed) or Sidecar (separate container)
2. **Implement Control Interface**: `/control/execute`, `/control/status`, `/control/output`
3. **Handle data references**: Support HTTP/gRPC/inline protocols
4. **Configure networking**: Use Docker DNS for service discovery

See `use-cases/portugal-node-sidecar/` and `use-cases/portugal-node-integrated/` for complete examples.

## License

Developed for AI-Effect consortium partners under project licensing agreements.
