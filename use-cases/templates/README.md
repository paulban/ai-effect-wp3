# Service Templates

Reference implementations for building services that integrate with the AI-Effect orchestration platform.

## Templates

| Category | Template | Directory | Use When |
|----------|----------|-----------|----------|
| File-based | Sequential | `file_based/sequential/` | Quick operations, data via shared storage |
| File-based | Concurrent | `file_based/concurrent/` | Long-running, data via shared storage |
| Protobuf | Sequential | `protobuf_based/sequential/` | Quick operations, data via direct gRPC |
| Protobuf | Concurrent | `protobuf_based/concurrent/` | Long-running, data via direct gRPC |

## Choosing a Template

### Data Exchange Pattern

- **File-based**: Services read/write to shared storage (S3, NFS, HTTP). Best for large datasets.
- **Protobuf-based**: Services exchange data directly via gRPC. Best for structured messages with type safety.

### Operation Duration

- **Sequential**: Operations complete in < 30 seconds. Simple request-response.
- **Concurrent**: Long-running operations. Returns immediately, processes in background with progress tracking.

## Architecture: Control vs Data Plane

All templates separate control and data planes:

```
┌─────────────┐                    ┌─────────────┐
│             │                    │             │
│ Orchestrator│                    │  Services   │
│             │                    │             │
└──────┬──────┘                    └──────┬──────┘
       │                                  │
       │  HTTP (control plane)            │
       │  - /control/execute              │
       │  - /control/status               │
       │  - /control/output               │
       ├──────────────────────────────────┤
       │                                  │
       │  Data plane (service-to-service) │
       │  - File-based: S3/HTTP/NFS       │
       │  - Protobuf: Direct gRPC         │
       │                                  │
```

### File-based Data Exchange

```
Service A                          Service B
    │                                  │
    │ Write to S3/HTTP                 │
    ├──────────────► [Storage] ────────┤ Read from S3/HTTP
    │                                  │
    │ Return URI reference             │
    └──────► Orchestrator ─────────────┘ Pass URI to B
```

### Protobuf-based Data Exchange

```
Service A                          Service B
    │                                  │
    │ Cache result                     │
    │                                  │
    │ Return gRPC endpoint             │
    └──────► Orchestrator ─────────────┘ Pass gRPC endpoint to B
                                       │
                                       │ gRPC GetLastResult()
              Service A ◄──────────────┤ Direct fetch
```

## Structure

Each template contains:

```
<template>/
├── handler.py        # Orchestrator interface (don't modify)
├── service.py        # Your methods go here
├── Dockerfile
├── requirements.txt
└── README.md
```

Protobuf templates also include:
```
├── proto/            # Your .proto files
│   └── example.proto
```

## Ports

### File-based Templates
- **HTTP** (8080): Control interface only

### Protobuf-based Templates
- **HTTP** (8080): Control interface for orchestrator
- **gRPC** (50051): Data interface for service-to-service communication

## Control Interface

The orchestrator drives services through these HTTP endpoints. This is the
mandatory contract — implementing them is what makes a service drivable:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/control/execute` | POST | Start task execution |
| `/control/status/{task_id}` | GET | Poll task status (concurrent only) |
| `/control/output/{task_id}` | GET | Get task output (concurrent only) |
| `/health` | GET | Health check (monitoring) |

`/control/output` returns a **DataReference** (`{protocol, uri, format}`), i.e.
a pointer to the result — not the result itself. The orchestrator passes that
pointer to the next task and never fetches the data.

### Serving data is optional (data plane)

How a service serves its data is **not** part of the orchestrator contract.
The templates provide a convenience endpoint for the common HTTP case:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/control/data/{task_id}` | GET | Serve raw payload bytes — **optional convenience** |

A service may skip this entirely and return a DataReference with
`protocol: inline` (bytes embedded), `protocol: grpc` (its own gRPC endpoint),
or an `http`/`s3` `uri` pointing anywhere. The orchestrator behaves identically
in all cases.

## Sequential vs Concurrent

### Sequential

```
Orchestrator                    Service
     |                              |
     |--- POST /control/execute --->|
     |                              | (process: 1-30 sec)
     |<-- {status: "complete"} -----|
```

- Single request-response cycle
- No background processing
- Simpler implementation

### Concurrent

```
Orchestrator                    Service
     |                              |
     |--- POST /control/execute --->|
     |<-- {status: "running"} ------|
     |                              |
     |                    [background thread]
     |                              |
     |--- GET /control/status ----->|
     |<-- {progress: 50} -----------|
     |                              |
     |--- GET /control/status ----->|
     |<-- {status: "complete"} -----|
     |                              |
     |--- GET /control/output ----->|
     |<-- {output: {...}} ----------|
```

- Returns immediately, processes in background
- Progress tracking via polling
- Thread-safe task management

## Execute Request

```json
{
  "method": "ProcessData",
  "workflow_id": "wf-123",
  "task_id": "task-456",
  "inputs": [
    {"protocol": "grpc", "uri": "upstream-service:50051", "format": "ConfigResponse"}
  ],
  "parameters": {"threshold": 0.5}
}
```

## DataReference

Data location passed between tasks:

### File-based Protocols

| Protocol | Description | Example URI |
|----------|-------------|-------------|
| `file` | Shared filesystem | `/data/input.csv` |
| `s3` | S3-compatible storage | `s3://bucket/key` |
| `http` | HTTP endpoint | `http://storage/data` |

### Protobuf-based Protocol

| Protocol | Description | Example URI |
|----------|-------------|-------------|
| `grpc` | Direct gRPC endpoint | `service-name:50051` |

The `format` field indicates the protobuf message type (e.g., `ConfigResponse`).

### Format Types

| Format | Description |
|--------|-------------|
| `json` | JSON data |
| `csv` | CSV data |
| `parquet` | Parquet file |
| Message name | Protobuf message type (for gRPC) |
