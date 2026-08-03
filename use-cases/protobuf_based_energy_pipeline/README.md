# Protobuf-Based Energy Pipeline

A 4-service pipeline demonstrating gRPC/protobuf data exchange between AI-Effect services, with HTTP Control Interface for orchestrator coordination.

## Overview

This pipeline demonstrates the separation of control and data planes:
- **Control plane (HTTP)**: The orchestrator calls each service's HTTP Control Interface to trigger execution in topological order
- **Data plane (gRPC)**: Services exchange data directly with each other via gRPC/protobuf, without going through the orchestrator

### Pipeline DAG

```
input-provider → data-generator → data-analyzer → report-generator
     HTTP            gRPC              gRPC              gRPC
```

### Services

| Service | HTTP Port | gRPC Port | Description |
|---------|-----------|-----------|-------------|
| input-provider | 18181 | — | Provides initial configuration |
| data-generator | 18182 | 50152 | Generates energy data |
| data-analyzer | 18183 | 50153 | Analyzes generated data |
| report-generator | 18184 | 50154 | Produces final report |

## Prerequisites

- Docker and Docker Compose
- Ports available: 18181-18184 (HTTP), 50152-50154 (gRPC), 18000 (orchestrator)

## Directory Structure

```
protobuf_based_energy_pipeline/
├── services/
│   ├── input_provider/
│   │   ├── service.py
│   │   ├── handler.py
│   │   ├── proto/
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── data_generator/
│   ├── data_analyzer/
│   └── report_generator/
├── connections.json                # Pipeline topology
├── docker-compose.yml
├── start.sh                        # Start services
├── stop.sh                         # Stop services
└── submit-workflow.sh              # Submit workflow to orchestrator
```

## Quick Start

```bash
./start.sh
./submit-workflow.sh
```

## Architecture

### Two Communication Channels

Each service exposes two interfaces:

1. **HTTP Control Interface** (port 8080) — Called by orchestrator workers to trigger execution and poll status
2. **gRPC Data Interface** (port 50051) — Called by upstream services to push/pull data directly

The orchestrator only uses HTTP. When it tells service B to execute, service B uses gRPC to fetch input data from service A. This keeps the orchestrator lightweight while enabling efficient binary data transfer between services.

### Control Interface

All services expose the standard AI-Effect Control Interface:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/control/execute` | POST | Execute an operation |
| `/control/status/{task_id}` | GET | Check task status |
| `/control/output/{task_id}` | GET | Retrieve task output |
| `/health` | GET | Health check |

## Running the Pipeline

See `RUNNING.md` for step-by-step instructions.
