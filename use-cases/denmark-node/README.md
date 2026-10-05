# Danish Node — Configuration and Topology (AI-EFFECT WP3)

Reference for one of the Danish Node use case on the AI-EFFECT
orchestration platform ([`AI-EFFECT/ai-effect-wp3`](https://github.com/AI-EFFECT/ai-effect-wp3)).

This use case runs three orchestrated services. The configuration assistant
turns user input into a normalized run configuration, the topology generator
enumerates feasible SYSLAB networks, and the renderer creates SVGs for topology
review. Ollama runs locally to parse free-text requests, while the web UI
submits workflows and exposes the topology confirmation step. Services can also be run without web UI

See [integration status](INTEGRATION_STATUS.md) for the demonstrated Solution Studio
flow and the remaining confirmation and data-plane limitations.

## Pipeline

```
config_assistant → topology_generator → draw_topology
  BuildRunConfig    GenerateTopology    RenderTopologies
  (DataSource)      (MLModel)           (DataSink)
```

| Service | Role | Behaviour | Main implementation |
|---|---|---|---|
| `config_assistant` | DataSource | Validates and normalizes supplies, loads, and routing options | `service.py` |
| `topology_generator` | MLModel | Enumerates feasible SYSLAB district-heating topologies | `dh_network_generator.py` |
| `draw_topology` | DataSink | Renders topology candidates to SVG for review | `service.py` |
| `web_ui` | User gateway | Submits prompts and records topology confirmation | `app.py` |

Node types (`DataSource` / `MLModel` / `DataSink`) are **auto-detected** by the
export generator from `connections.json` — a node with no incoming connections is
a DataSource, one with no outgoing connections is a DataSink. They are not
declared anywhere by hand.

## Layout

```
denmark-node/
├── common/                     # vendored from ai-effect-wp3 use-cases/common/
│   ├── concurrent.py           # service control-interface implementation
│   ├── sequential.py
├── services/
│   ├── config_assistant/       # BuildRunConfig
│   ├── topology_generator/     # GenerateTopology
│   ├── draw_topology/          # RenderTopologies and SVG renderer
│   └── web_ui/                 # prompt submission and topology confirmation
├── connections.json            # service_mapping + wiring (input to the generator)
├── docker-compose.yml
├── data/                       # bind-mount point for input datasets
├── output/                     # final reports land here
├── export/                     # GENERATED — blueprint.json, dockerinfo.json, protos
├── generate-export.sh
├── start.sh / stop.sh
└── submit-workflow.sh
```

## Contract

The orchestrator only ever calls four endpoints, provided by
`common/concurrent.py`:

| Endpoint | Purpose |
|---|---|
| `POST /control/execute` | Start an operation (`method`, `workflow_id`, `task_id`, `inputs`, `parameters`) |
| `GET /control/status/{task_id}` | Poll `status` + `progress` |
| `GET /control/output/{task_id}` | Fetch the resulting **DataReference** |
| `GET /health` | Monitoring |

`/control/output` returns a *pointer*, never the payload:
`{"protocol": "file", "uri": "/data/<workflow_id>/<task_id>.json", "format": "json"}`.
The orchestrator forwards that pointer to the next task and never reads it.

**Data plane.** These services exchange payloads as JSON files on the shared
Docker volume `denmark-node-data`, mounted at `/data` in every pipeline service.
Each service resolves `inline` (base64 JSON) and `file` inputs, then publishes
JSON output as a file DataReference. This is a Denmark Node convention, not a
platform requirement.

## Running

**1. Start the orchestrator** (from your `ai-effect-wp3` checkout):

```bash
cd ai-effect-wp3/orchestrator
docker compose up -d          # Redis :16379, API :18000, 3 workers
```

**2. Start the Danish Node services:**

```bash
cd denmark-node
./start.sh                    # creates the ai-effect-services network if missing
```

Host ports `18101`–`18104` are exposed for debugging only. The orchestrator
reaches the services by Docker DNS name on internal port `8080`, which is what
`export/dockerinfo.json` declares.

**3. Generate the onboarding package:**

```bash
WP3_REPO=/path/to/ai-effect-wp3 ./generate-export.sh
```

This runs `scripts/onboarding-export-generator.py`, which reads `services/*/proto/`
and `connections.json` and writes `export/blueprint.json`, `export/dockerinfo.json`,
`export/generation_metadata.json`, `export/microservice/*.proto` and `export.zip`.

**4. Submit a workflow:**

```bash
./submit-workflow.sh
curl -s http://localhost:18000/workflows/<workflow_id>/tasks | jq .
```

The topology output is stored as a DataReference on the shared volume.

**5. Onboard to the portal:** upload `export.zip` at
[portal.renewenergy.io](https://portal.renewenergy.io) → **Solutions → Import ZIP**.

## Important notes

- **Pass a real directory name to the generator, not `.`** — it derives the
  pipeline name and image tags from the last path component, and `Path(".").name`
  is empty, producing `"name": ""` and images `-<service>:latest`.
  `generate-export.sh` uses `$(pwd)` for this reason.
- **Keep the compose project named `denmark-node`.** The generator writes
  `image: denmark-node-<service>:latest` into `blueprint.json`, and Compose tags
  images `<project>-<service>`. The `name: denmark-node` key at the top of
  `docker-compose.yml` pins this regardless of the folder name.
- **Directory names under `services/` are the node identifiers** and must stay
  `snake_case`; the generator appends `1` to form `container_name`
  (`config_assistant` → `config_assistant1`).
- **Only RPCs referenced in `connections.json` reach the export.**
- **Build context is the node root**, not the service folder, so each Dockerfile
  can `COPY common/`. Hence `docker build -f services/<name>/Dockerfile .`

## Adding another service

1. `mkdir -p services/<snake_name>/proto` and write `<snake_name>.proto` with one
   `rpc` per operation.
2. Copy `handler.py`, `Dockerfile`, `requirements.txt` from a sibling service and
   change the paths in the Dockerfile.
3. Write `service.py` with an `execute_<MethodName>` per RPC you intend to wire.
4. Add the entry to `service_mapping` **and** the wiring to `connections` in
   `connections.json`.
5. Add the service to `docker-compose.yml` (kebab-case name matching `ip_address`).
6. Re-run `./generate-export.sh`.
