# Preparing a Use Case for the AI-Effect Portal

This guide walks through preparing your use case so the export generator can produce an onboarding package (zip) for the AI-Effect portal.

## What You Need

Two things:

1. **`services/`** directory — one subdirectory per pipeline node, each containing a `.proto` file that describes its interface
2. **`connections.json`** — declares how the nodes connect and where they run

That's it. The generator reads these and produces `blueprint.json`, `dockerinfo.json`, and copies the proto files into the export.

## Directory Structure

```
use-cases/danish-node/
├── services/
│   ├── data_ingestion/
│   │   └── proto/
│   │       └── data_ingestion.proto
│   ├── anomaly_detector/
│   │   └── proto/
│   │       └── anomaly_detector.proto
│   └── report_writer/
│       └── proto/
│           └── report_writer.proto
├── connections.json
├── docker-compose.yml          ← optional, for running services
└── export/                     ← generated output goes here
```

### Rules

- Each subdirectory under `services/` becomes a pipeline node
- Each must have a `proto/` subdirectory with exactly one `.proto` file
- The directory name (e.g., `data_ingestion`) is used as the node identifier — keep it `snake_case`
- Everything else in the directory (source code, Dockerfiles, data) is ignored by the generator

## Step 1: Write Proto Files

Each proto file describes the gRPC interface of one pipeline node. The generator extracts the `rpc` definitions from it.

Minimal example — `services/data_ingestion/proto/data_ingestion.proto`:

```protobuf
syntax = "proto3";
package data_ingestion;

service DataIngestion {
  rpc FetchData(FetchDataRequest) returns (FetchDataResponse);
}

message FetchDataRequest {
  string source_url = 1;
}

message FetchDataResponse {
  string status = 1;
}
```

The generator only looks at `rpc` lines. The message definitions are included in the export for the platform to display in the visual designer, but they don't affect the pipeline topology.

### Proto files for existing/external services

If your service already has a REST API (not gRPC), you still need a proto file — it acts as an interface description for the platform. Write it to match what the service actually does. See the TEF services in `portugal-node-integrated/services/` for examples of proto files describing REST APIs.

### Multiple RPCs in one proto

If a proto file has multiple RPCs but your pipeline only uses some of them, the generator will include only the ones referenced in `connections.json`. For example, if `knowledge_store.proto` has 7 RPCs but only `ApplyFunction` is used in the pipeline, only that one appears in the export.

### Same service backing multiple nodes

If one Docker service handles multiple pipeline roles (e.g., a synthetic data service that both trains models and generates data), create separate directories with the same proto file:

```
services/
├── model_trainer/
│   └── proto/model_trainer.proto      ← copy of synthetic_data.proto
└── data_generator/
    └── proto/data_generator.proto     ← same file, same proto
```

Each becomes its own node in the blueprint. The `connections.json` `service_mapping` maps both to the same Docker service (see Step 2).

## Step 2: Write connections.json

This file defines three things:
- **service_mapping** — where each node runs (hostname, port)
- **connections** — how nodes are wired together

```json
{
  "pipeline": {
    "name": "Danish Node Pipeline",
    "service_mapping": {
      "data_ingestion":   {"ip_address": "data-ingestion",   "port": 8080},
      "anomaly_detector": {"ip_address": "anomaly-detector",  "port": 8080},
      "report_writer":    {"ip_address": "report-writer",     "port": 8080}
    },
    "connections": [
      {
        "from_service": "data-ingestion",
        "from_method": "FetchData",
        "to_service": "anomaly-detector",
        "to_method": "DetectAnomalies"
      },
      {
        "from_service": "anomaly-detector",
        "from_method": "DetectAnomalies",
        "to_service": "report-writer",
        "to_method": "WriteReport"
      }
    ]
  }
}
```

### service_mapping (required)

One entry per directory in `services/`. The key is the directory name (snake_case).

| Field | Description | Example |
|-------|-------------|---------|
| `ip_address` | Hostname the orchestrator uses to reach this service | `"data-ingestion"` or `"192.168.1.50"` |
| `port` | Port the service listens on (as seen by the orchestrator) | `8080` |

What to put in `ip_address` and `port` depends on your deployment:

| Scenario | ip_address | port | Example |
|----------|------------|------|---------|
| Same Docker network (typical) | Docker service name | Internal container port | `"data-ingestion"`, `8080` |
| Service on a remote machine | Real IP or hostname | Host-mapped port | `"10.0.1.50"`, `18201` |
| Service on same host, different network | `localhost` or host IP | Host-mapped port | `"localhost"`, `18201` |

**Same service, multiple nodes** — point multiple entries to the same address:
```json
"model_trainer":  {"ip_address": "synthetic-data", "port": 600},
"data_generator": {"ip_address": "synthetic-data", "port": 600}
```

**Remote machines** — use real IPs instead of Docker DNS names:
```json
"data_ingestion":   {"ip_address": "10.0.1.10", "port": 8080},
"anomaly_detector": {"ip_address": "10.0.1.20", "port": 8080}
```

### connections (required)

Each entry wires one operation's output to another operation's input.

| Field | Description |
|-------|-------------|
| `from_service` | Source service — must match an `ip_address` value in service_mapping, or a directory name |
| `from_method` | RPC method name that produces the output (must exist in the source's proto file) |
| `to_service` | Target service — same matching rules |
| `to_method` | RPC method name that consumes the input (must exist in the target's proto file) |

The generator uses connections to:
1. Wire `connected_to` in the blueprint (which node feeds which)
2. Auto-detect node types: no incoming connections = **DataSource**, no outgoing = **DataSink**, both = **MLModel**
3. Filter RPCs: only methods referenced in connections are included in the export

## Step 3: Run the Generator

From the repo root:

```bash
python3 scripts/onboarding-export-generator.py \
    use-cases/danish-node \
    use-cases/danish-node/export \
    --overwrite --zip
```

The `--zip` flag produces a portal-ready ZIP in one step. By default it is written next to the output directory as `<output_dir>.zip` (here: `use-cases/danish-node/export.zip`). Pass a path after `--zip` to choose a custom location, e.g. `--zip /tmp/danish-node.zip`.

Omit `--zip` if you only want the export folder without packaging.

This creates:

```
use-cases/danish-node/export/
├── blueprint.json              ← pipeline topology for the portal
├── dockerinfo.json             ← service addresses for the orchestrator
├── generation_metadata.json    ← use case metadata
└── microservice/
    ├── data_ingestion1.proto
    ├── anomaly_detector1.proto
    └── report_writer1.proto

use-cases/danish-node/export.zip  ← upload this to the portal
```

## Step 4: Upload to the Portal

Open the portal at https://portal.renewenergy.io, go to **Solutions → Import ZIP**, and upload the file created in Step 3. The portal reads `blueprint.json` for the pipeline graph and `dockerinfo.json` to know how to reach each service.

If you skipped `--zip` in Step 3, you can package the export folder manually:

```bash
cd use-cases/danish-node/export
zip -r ../danish-node.zip blueprint.json dockerinfo.json microservice/
```

## Quick Reference: Existing Use Cases

| Use Case | Services | Notes |
|----------|----------|-------|
| `file_based_energy_pipeline` | 4 nodes, all on port 8080 | Simple linear pipeline, good starting reference |
| `protobuf_based_energy_pipeline` | 4 nodes with `pb-` prefix | Same pipeline with gRPC data exchange, prefixed to avoid DNS conflicts |
| `germany-node` | 3 nodes | Includes VILLASnode integration |
| `portugal-node-integrated` | 4 nodes, non-standard ports | REST API services with proto wrappers, two nodes share one Docker service |
| `portugal-node-sidecar` | 4 nodes via adapters | Same pipeline but services accessed through sidecar adapter containers |

## Troubleshooting

**"Service directory 'X' not found in service_mapping"**
Every directory under `services/` must have a matching key in `service_mapping`. Add the missing entry to `connections.json`.

**"connections.json not found"**
The file is required. Create it with at least `service_mapping` and `connections`.

**"Warning: Connection references unknown services"**
The `from_service` or `to_service` value in a connection doesn't match any known service. Check that it matches either an `ip_address` value or a directory name from `services/`.

**Node shows all RPCs instead of just the connected ones**
Make sure `from_method` and `to_method` are set in every connection entry. Without them, the generator can't filter and includes all RPCs from the proto.

**Wrong node_type (DataSource/DataSink/MLModel)**
Node type is auto-detected from connections. A node with no incoming connections is DataSource, no outgoing is DataSink. If the type is wrong, check that connections reference the correct service names.
