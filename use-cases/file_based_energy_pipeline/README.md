# File-Based Energy Pipeline

A 4-service pipeline demonstrating file-based data exchange between AI-Effect services, orchestrated via the HTTP Control Interface.

## Overview

Services share data through a mounted volume (`./data`). The orchestrator controls execution order — each service reads input files left by the previous service and writes output files for the next.

### Pipeline DAG

```
input-provider → data-generator → data-analyzer → report-generator
```

### Services

| Service | Port | Description |
|---------|------|-------------|
| input-provider | 18081 | Provides initial configuration |
| data-generator | 18082 | Generates energy data files |
| data-analyzer | 18083 | Analyzes generated data |
| report-generator | 18084 | Produces final report |

## Prerequisites

- Docker and Docker Compose
- Ports available: 18081-18084 (services), 18000 (orchestrator)

## Directory Structure

```
file_based_energy_pipeline/
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
├── submit-workflow.sh              # Submit workflow to orchestrator
└── data/                           # Shared volume for file exchange
```

## Quick Start

```bash
./start.sh
./submit-workflow.sh
```

## Architecture

### Data Exchange

All services mount the same `./data` volume. Each service:
1. Reads input files from the shared directory
2. Processes the data
3. Writes output files for the next service

The orchestrator calls each service's `/control/execute` endpoint in topological order. Services don't communicate directly — the orchestrator mediates all control flow.

### Control Interface

All services expose the standard AI-Effect Control Interface on port 8080 (mapped to host ports 18081-18084):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/control/execute` | POST | Execute an operation |
| `/control/status/{task_id}` | GET | Check task status |
| `/control/output/{task_id}` | GET | Retrieve task output |
| `/health` | GET | Health check |

## Running the Pipeline

See `RUNNING.md` for step-by-step instructions.
