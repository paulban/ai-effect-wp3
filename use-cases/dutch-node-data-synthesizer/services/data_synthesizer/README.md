# Dutch Node — Synthetic Power Grid

Generates a synthetic transmission grid with the Chung-Lu-Chain method and
returns it as a fetchable artifact: a node-link graph plus the same network as a
pandapower snapshot, which is the form a consumer actually wants.

**Spec:** `.claude/specs/dutch-node-target-architecture-spec.md` (FR-24 to FR-28)

---

## How a run works

```
orchestrator ──POST /control/execute──▶ synthetic-data:8080
                    ConfigureAndSynthesize      │
                                                │ task id, immediately (FR-24)
                                      configure the grid (synchronous —
                                      a bad request fails now, not in a
                                      minute's time)
                                                │
                                      generate on a background thread:
                                      topology → bus types → capacity →
                                      loads → dispatch → transmission
                                                │
              ◀── /control/status, progress ────┤
              ◀── /control/output ──────────────┘
                  DataReference: http, the artifact URL

caller ──GET http://localhost:8003/control/data/{job_id}──▶ the grid
```

The two logical steps — configure, then synthesize — are backed by one process,
so the exported package declares the single operation `ConfigureAndSynthesize`.
The configuration is passed to the synthesis inline rather than through a
reference to this service's own endpoint, because both steps run here.

## What is deliberately absent

- **No dependency on the node's proxy.** The service is reachable two ways: on
  the `ai-effect-services` network at the address `export/dockerinfo.json`
  names, `synthetic-data:8080`, which is the path the orchestrator's workers
  take; and on `http://localhost:8003`, published on loopback for this machine.
  See "Running it" — the host port is a development convenience that FR-19 and
  FR-27 would not permit in a deployment.
- **No gRPC server.** `data_synthesizer.proto` is still built and its message
  types are still used internally to shape the configuration, but nothing
  serves them. The results data plane is the HTTP artifact endpoint.
- **No inline execution.** Synthesis runs on a background thread so the
  orchestrator's request is not held open for its duration.

## Running it

```bash
export SERVICE_API_KEY=$(openssl rand -hex 32)   # optional; open if unset

cd orchestrator && docker compose up -d          # api, redis, 3 workers
cd ../use-cases/dutch-node-data-synthesizer && docker compose up -d --build
```

`scripts/start.sh` and `scripts/stop.sh` wrap those two lines.

### How the service is reached

Two ways, and the distinction matters when something breaks.

**The orchestrator's workers** dial it on the `ai-effect-services` network at the
address `export/dockerinfo.json` names, `synthetic-data:8080`. No host port is
involved. This is the path all real work takes, and it is what `run_workflow.sh`
probes from inside a worker container — a probe from the host would not exercise
it.

**This machine** reaches it at `http://localhost:8003`, published on loopback by
`docker-compose.yml`. Result references point here, so a finished grid is
fetchable with nothing else running.

That host port is a development convenience and it contradicts FR-19 and FR-27,
which exist so the node exposes exactly one port however many services it grows.
For a real deployment, put the proxy back in front:

```bash
SYNTHESIZER_SELF_URL=https://node.example.org/svc/synth docker compose up -d
```

and drop the `ports:` block. Nothing else changes — the orchestrator never used
the host port.

| Variable | Default | Purpose |
|---|---|---|
| `SYNTHESIZER_HOST_PORT` | `8003` | Loopback port the service is published on |
| `SYNTHESIZER_SELF_URL` | `http://localhost:8003` | Base URL result references are built from |
| `SERVICE_API_KEY` | unset | Bearer token on `/control/*`; open if unset |
| `SYNTHESIZER_MAX_CONCURRENT_JOBS` | `1` | Simultaneous syntheses |
| `SYNTHESIZER_CPU_LIMIT` / `SYNTHESIZER_MEMORY_LIMIT` | `2.0` / `4G` | Resource ceiling |

## Run a synthesis through the orchestrator

```bash
SERVICE_API_KEY=<same key> ./run_workflow.sh
```

The script submits the exported blueprint, waits for the workflow, and fetches
the artifact the result reference points at — the full path a vendor walks. It
checks first that the orchestrator is up, that its workers are running, and that
the service answers at its `dockerinfo.json` address *from inside a worker*,
because that is the call that has to succeed and a probe from the host would
prove nothing.

| Variable | Default | Purpose |
|---|---|---|
| `ORCHESTRATOR_URL` | `http://localhost:18000` | Where to submit |
| `ORCHESTRATOR_API_KEY` | unset | Bearer token on the orchestrator API |
| `SERVICE_API_KEY` | unset | Forwarded to the service as `services_api_key`. Must match the container's, or the worker's call is rejected with 401 |
| `OUTPUT_DIR` | `./results` | Where the fetched grid is written |

## Input payload

Inline base64 JSON. Every field is optional; the service's defaults produce a
three-level, 180-node grid.

```json
{
  "level_specs": [
    {"n": 20,  "avg_k": 3.0, "diam": 6,  "dist_type": "dgln", "max_k": 15},
    {"n": 60,  "avg_k": 2.2, "diam": 10, "dist_type": "dgln", "max_k": 10},
    {"n": 100, "avg_k": 2.0, "diam": 15, "dist_type": "dgln", "max_k": 10}
  ],
  "connection_specs": {
    "(0, 1)": {"type": "k-stars", "c": 0.174, "gamma": 4.15},
    "(1, 2)": {"type": "k-stars", "c": 0.150, "gamma": 4.15}
  },
  "seed": 42,
  "loading_level": "M",
  "ref_sys_id": 1
}
```

`loading_level` is `L`, `M` or `H`. Keys the service does not recognise are
ignored silently, so a misspelled field yields a default grid rather than an
error — check the run's printed parameters against what you meant to send.

## Output shape

JSON, served at `/control/data/{job_id}`:

| Field | Contents |
|---|---|
| `status` | `success` |
| `nodes`, `edges` | Counts of the generated topology |
| `seed`, `loading_level`, `ref_sys_id` | Echoed configuration |
| `benchmark_env_name` | Name of the grid2op environment this grid stands for |
| `graph_data` | networkx node-link form, with the physics attributes attached |
| `pandapower` | The same network via `pp.to_json()`, ready for `pp.from_json()` |

The result reference's `format` is `json` — the encoding, which is what the
orchestrator validates — and the logical name `GridData` travels in the
reference's `metadata.data_format`.

## Key files

- `main.py` — entrypoint; builds the job runner and starts the shared app
- `synth/synth_operations.py` — `ConfigureAndSynthesize` and the generation
  pipeline, plus the networkx → pandapower conversion
- `synth/proto_runtime.py` — generates the protobuf stubs if the image did not
- `../../export/{blueprint,dockerinfo}.json` — orchestrator workflow metadata,
  generated by `scripts/onboarding-export-generator.py`
- `run_workflow.sh` — end-to-end orchestration run script
- `test_pipeline.py` — a manual two-step smoke script against a directly
  reachable service. Its hardcoded `http://localhost:8080` still needs changing
  to `http://localhost:8003` to match the published port

## Tests

The generation pipeline has no automated tests of its own. The shared job
lifecycle it depends on — background execution, progress, capacity, artifact
references, and the per-workflow keying that stops two runs overwriting each
other — is covered by the benchmark service's suite, which exercises the same
`use-cases/common/` code:

```bash
python -m pytest use-cases/dutch-node-benchmarking/services/benchmarking/tests/ -q
```
