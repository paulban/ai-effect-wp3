# Dutch Node — Synthetic Power Grid

Generates a synthetic transmission grid with the Chung-Lu-Chain method, using
the [powergrid_synth](https://github.com/TU-Delft-AI-Energy-Lab/grid-synthesizer)
package, and returns it as a single pandapower JSON file that
`pandapower.from_json()` opens directly. The synthesis metadata travels inside
that file.

**Spec:** `.claude/specs/dutch-node-target-architecture-spec.md` (FR-24 to FR-28)

---

## How a run works

```
orchestrator ──POST /control/execute──▶ synthetic-data:8080
                    ConfigureAndSynthesize      │
                                                │ job id, immediately (FR-24)
                                      read and normalise the parameters
                                      (synchronous — a bad request fails
                                      now, not in a minute's time)
                                                │
                                      on a background thread, powergrid_synth's
                                      synthesize(mode="synthetic"):
                                      topology → bus types → capacity →
                                      loads → dispatch → transmission →
                                      pandapower JSON export
                                                │
              ◀── /control/status, progress ────┤
              ◀── /control/output ──────────────┘
                  DataReference: http, the artifact URL (FR-05, FR-25)

caller ──GET {SELF_URL}/control/data/{job_id}──▶ the pandapower JSON
```

The job is run by `BatchJobRunner` from `use-cases/common/batch_jobs.py`. It
caps simultaneous syntheses (FR-26; a submission beyond the cap fails
immediately), keys each job by `<workflow_id>.<task_id>` so runs do not
overwrite each other, and stores the result in the artifact store on the
`synthesizer_artifacts` volume. A copy is also written to
`results/<job_id>.json`, bind-mounted from the container to this directory.

The service declares the single operation `ConfigureAndSynthesize`. Every
function named `execute_<Method>` in `service.py` is exposed as an operation,
so helpers there are private (leading underscore).

## Running it

```bash
export SERVICE_API_KEY=$(openssl rand -hex 32)   # optional; open if unset

cd orchestrator && docker compose up -d          # api, redis, 3 workers
cd ../use-cases/dutch-node-data-synthesizer && docker compose up -d --build
```

`scripts/start.sh` and `scripts/stop.sh` wrap those two lines.

The orchestrator's workers dial the service on the `ai-effect-services` network
at the address `export/dockerinfo.json` names, `synthetic-data:8080`. The
compose file also publishes it on loopback at `http://localhost:8003`; result
references point there by default, so a finished grid is fetchable from this
machine. That host port is a development convenience that FR-19 and FR-27 do
not permit in a deployment, where `SYNTHESIZER_SELF_URL` names the node's proxy
route instead.

| Variable | Default | Purpose |
|---|---|---|
| `SYNTHESIZER_HOST_PORT` | `8003` | Loopback port the service is published on |
| `SYNTHESIZER_SELF_URL` | `http://localhost:8003` | Base URL result references are built from; set it to the proxy route in a deployment |
| `SERVICE_API_KEY` | unset | Bearer token on `/control/*`; open if unset |
| `SYNTHESIZER_MAX_CONCURRENT_JOBS` | `1` | Simultaneous syntheses |
| `SYNTHESIZER_CPU_LIMIT` / `SYNTHESIZER_MEMORY_LIMIT` | `2.0` / `4G` | Resource ceiling |

## Run a synthesis through the orchestrator

```bash
SERVICE_API_KEY=<same key> ./run_workflow.sh
```

The script submits the exported blueprint, waits for the workflow, fetches the
grid the result reference points at into `$OUTPUT_DIR/<workflow_id>.json`, and
prints a summary from its metadata. It checks first that the orchestrator is up, that its workers are
running, and that the service answers at its `dockerinfo.json` address *from
inside a worker*, because that is the call that has to succeed.

| Variable | Default | Purpose |
|---|---|---|
| `ORCHESTRATOR_URL` | `http://localhost:18000` | Where to submit |
| `ORCHESTRATOR_API_KEY` | unset | Bearer token on the orchestrator API |
| `SERVICE_API_KEY` | unset | Forwarded to the service as `services_api_key`. Must match the container's, or the worker's call is rejected with 401 |
| `OUTPUT_DIR` | `./results` | Where the grid is written |

## Input payload

Inline base64 JSON (or an `http(s)` reference returning it). Every field is
optional; the defaults produce a three-level, 180-node grid before the largest
connected component is kept.

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
  "ref_sys_id": 1,
  "synthetic_grid_name": "synthetic-grid-v0"
}
```

`loading_level` is `L`, `M` or `H`. `synthetic_grid_name` becomes `net.name`.
Keys the service does not recognise are ignored silently, so a misspelled field
yields a default grid rather than an error.

## Output shape

A pandapower network in JSON form, as `pp.to_json()` writes it, served at
`/control/data/{job_id}`:

```python
import pandapower as pp
net = pp.from_json("results/<job_id>.json")
net.name                    # the synthetic grid name
net["synthesis_metadata"]   # see below
```

The network is produced by powergrid_synth's own export (`GridExporter`, which
uses `nx_to_pandapower`). The synthesis metadata is at
`net["synthesis_metadata"]` (in the raw JSON: `_object.synthesis_metadata`):

| Field | Contents |
|---|---|
| `synthetic_grid_name` | Name of the grid, also `net.name` (default `synthetic-grid-v0`) |
| `seed`, `loading_level`, `ref_sys_id` | Echoed configuration |
| `level_specs`, `connection_specs` | Echoed topology parameters |
| `voltage_by_level_kv` | Nominal voltage assigned to each hierarchy level |
| `buses`, `lines`, `transformers` | Element counts of the network |
| `powergrid_synth_version`, `pandapower_version` | Versions that produced the file |
| `power_flow_converged` | Whether an AC power flow converged on the result |

Open the file with a pandapower version close to `pandapower_version`; much
older versions load it but fail in `runpp`.

The result reference's `format` is `json`, the encoding the orchestrator
validates; the logical name `GridData` travels in `metadata.data_format`.

## Key files

- `service.py` — the service: `ConfigureAndSynthesize`, parameter handling,
  the call into powergrid_synth, and the batch job submission
- `handler.py` — re-exports the shared control plane from `use-cases/common/`
- `proto/data_synthesizer.proto` — declares the `ConfigureAndSynthesize`
  operation for the onboarding export. The service itself speaks HTTP, not gRPC
- `../../export/` — orchestrator workflow metadata, generated with
  `python scripts/onboarding-export-generator.py use-cases/dutch-node-data-synthesizer <out>`
  from the proto and `../../connections.json`. The generator names the container
  `data_synthesizer1`; the committed export has it renamed to `data_synthesizer`
- `run_workflow.sh` — end-to-end orchestration run script

## Tests

The service has no automated tests of its own. The shared job lifecycle it
depends on (background execution, progress, capacity, artifact references,
per-workflow job ids) is covered by the benchmark service's suite, which
exercises the same `use-cases/common/` code:

```bash
python -m pytest use-cases/dutch-node-benchmarking/services/benchmarking/tests/ -q
```
