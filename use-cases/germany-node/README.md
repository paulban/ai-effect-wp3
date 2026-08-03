# Germany Node VILLASnode Chronics Pipeline

Converts pandapower timeseries data into Grid2Op chronics format using VILLASnode's `create_chronics` hook, orchestrated as a 3-node AI-Effect pipeline.

## Overview

The pipeline uses RWTH Aachen's VILLASnode signal processing framework to transform pandapower CSV timeseries (load and static generator profiles) into Grid2Op-compatible chronics files. VILLASnode runs as a long-lived container controlled via REST API.

### Pipeline DAG

```
data_provider → villas_chronics → output_formatter
                     │
              (REST API call)
                     │
              villas-node (long-running container)
```

### Services

| Service | Port | Type | Description |
|---------|------|------|-------------|
| data-provider | 18091 | Sequential | Copies pandapower data to shared volume |
| villas-chronics | 18092 | Concurrent | Pushes config to VILLASnode, polls for output |
| output-formatter | 18093 | Sequential | Validates chronics CSVs, writes summary |
| villas-node | — | Long-running | VILLASnode signal processing (REST API on port 80) |

## Prerequisites

- Docker and Docker Compose
- Orchestrator running (see `orchestrator/` directory)
- Ports available: 18091-18093 (adapter services), 18000 (orchestrator)

## Directory Structure

```
germany-node/
├── common/                         # Shared handler modules
│   ├── __init__.py
│   ├── sequential.py               # Sequential handler (instant ops)
│   └── concurrent.py               # Concurrent handler (async ops)
├── services/
│   ├── data_provider/
│   │   ├── service.py              # execute_ProvideData
│   │   ├── proto/data_provider.proto
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── villas_chronics/
│   │   ├── service.py              # execute_GenerateChronics (async)
│   │   ├── proto/villas_chronics.proto
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── output_formatter/
│       ├── service.py              # execute_FormatOutput
│       ├── proto/output_formatter.proto
│       ├── Dockerfile
│       └── requirements.txt
├── chronics.conf.template          # VILLASnode config template
├── connections.json                # Pipeline connections for export generator
├── docker-compose.yml              # 4 services + shared volume
├── export/                         # Generated blueprint + dockerinfo
│   ├── blueprint.json
│   └── dockerinfo.json
├── start.sh                       # Start services
├── stop.sh                        # Stop services
├── submit-workflow.sh             # Submit workflow to orchestrator
├── data/                           # Input data (mounted read-only)
│   ├── loads/                      # Load CSV files (*_converted.csv)
│   ├── sgens/                      # Static generator CSV files
│   ├── ppnet_DW00.1.382280-VE_202412.json  # Pandapower grid model
│   └── convert_csv.py             # Utility to convert raw pandapower CSVs
└── output/                         # Pipeline output (chronics)
```

## Architecture

### VILLASnode Communication

VILLASnode starts idle (no config) as a Docker Compose service. The `villas_chronics` adapter:

1. Reads the manifest written by `data_provider` (file paths, trigger files)
2. Loads `chronics.conf.template` and substitutes workflow-specific paths
3. Writes the rendered config to the shared volume
4. Pushes the config file path to VILLASnode via `POST /api/v2/restart`
5. Polls the output directory until all 5 chronics CSV files appear

### Shared Volume Data Flow

```
data_provider writes:
  /shared/{workflow_id}/
  ├── loads/           ← *_converted.csv files
  ├── sgens/           ← *_converted.csv files
  ├── ppnet_*.json     ← grid model
  └── manifest.json    ← paths + trigger files

villas_chronics writes:
  /shared/{workflow_id}/
  ├── villas_config.conf     ← rendered VILLASnode config
  └── chronics_output/       ← VILLASnode writes here
      ├── load_p.csv
      ├── load_q.csv
      ├── prod_p.csv
      ├── prod_q.csv
      └── prod_v.csv

output_formatter writes:
  /shared/{workflow_id}/
  └── final_output/
      ├── load_p.csv ... prod_v.csv  ← validated copies
      └── summary.json               ← validation report
```

### Config Template

The `chronics.conf.template` uses `{{PLACEHOLDER}}` syntax. The adapter substitutes:

| Placeholder | Source |
|------------|--------|
| `{{LOAD_URI}}` | First load file from manifest (`trigger_load`) |
| `{{SGEN_URI}}` | First sgen file from manifest (`trigger_sgen`) |
| `{{LOADS_DIR}}` | Manifest `loads_dir` |
| `{{SGENS_DIR}}` | Manifest `sgens_dir` |
| `{{GRID_FILE}}` | Manifest `grid_file` |
| `{{OUTPUT_DIR}}` | `{input_dir}/chronics_output` |
| `{{ROUND_DECIMALS}}` | Request parameter (default: 3) |
| `{{COMPRESS}}` | Request parameter (default: false) |

### Async Task Handling

The `villas_chronics` service uses the concurrent handler pattern:

```
Orchestrator                   villas_chronics              VILLASnode
     │                              │                           │
     │── POST /control/execute ────▶│                           │
     │◀── {status: "running"} ──────│                           │
     │                              │── POST /api/v2/restart ──▶│
     │                              │                           │ (processing)
     │── GET /control/status ──────▶│                           │
     │◀── {progress: 20} ──────────│── poll output files ──────▶│
     │                              │                           │
     │── GET /control/status ──────▶│                           │
     │◀── {status: "complete"} ────│                           │
     │                              │                           │
     │── GET /control/output ──────▶│                           │
     │◀── {output: {...}} ─────────│                           │
```

## Common Module

The `common/` directory provides the orchestrator control interface, shared by all three adapter services:

- **`sequential.py`** — For instant operations (data_provider, output_formatter). Returns `"complete"` immediately.
- **`concurrent.py`** — For async operations (villas_chronics). Returns `"running"`, supports progress polling via `/control/status/{task_id}`.

## Running the Pipeline

See `RUNNING.md` for step-by-step instructions.

## VILLASnode create_chronics Hook

The hook (compiled into the VILLASnode image) performs:

1. **Input**: Globs `Load*.csv` and `SGen*.csv` from `loads_dir`/`sgens_dir`
2. **Grid mapping**: Parses pandapower JSON for `load index → bus ID` and `sgen index → bus ID`
3. **Output**: 5 semicolon-separated CSV files: `load_p.csv`, `load_q.csv`, `prod_p.csv`, `prod_q.csv`, `prod_v.csv`
4. **Options**: `round_decimals`, `compress` (bz2), `voltage` (default 20.0), `negate_sgens`

The `uris` field in the config triggers VILLASnode to start signal processing. The hook then independently globs the full directories — `uris` is just the entry point, not the complete file list.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | HTTP server port |
| `DATA_DIR` | `/data` | Input data directory (data_provider) |
| `SHARED_DIR` | `/shared` | Shared volume mount point |
| `GRID_FILE` | `ppnet_DW00.1.382280-VE_202412.json` | Grid model filename |
| `VILLAS_NODE_URL` | `http://villas-node:80` | VILLASnode REST API URL |
| `CONFIG_TEMPLATE` | `/app/chronics.conf.template` | Config template path |
| `POLL_INTERVAL` | `2.0` | Seconds between output file checks |
| `POLL_TIMEOUT` | `120.0` | Maximum seconds to wait for output |
