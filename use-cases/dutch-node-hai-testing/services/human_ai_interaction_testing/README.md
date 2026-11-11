# HAI Session Service (`hai-session-runner`)

Runs one human-AI interaction session per `RunHAISession` request, against an
InteractiveAI stack that is already up. For the overall flow and setup, see
the [use-case README](../../README.md). The spec is
`.claude/specs/hai-orchestrated-session-spec.md`.

## What a request selects

As in benchmarking, a request names its inputs and never carries code or paths.
Both come from directories the node operator fills:

| Input | Selected from | Mounted at |
|---|---|---|
| Algorithm: a `.py` file implementing `build_agent(env, context)` | `algorithms/` | `/app/algorithms` (ro) |
| Scenario: a grid2op environment directory, in the simulator's format | `scenarios/` | `/app/scenarios` (ro), and in the simulator at `/code/Ressources/hai_scenarios` (ro) |

The algorithm contract is benchmarking's: `build_agent` returns an object
with `act(observation) -> grid2op action`. The same file works in both
services. `context` holds `use_case` (`"hai_session"`), `scenario`,
`session_duration_seconds` and `scenario_directory`.

## Input payload

One inline base64 JSON input:

```json
{
  "algorithm": "algorithm_template.py",
  "scenario": {
    "env_name": "env_ICAPS_input_data_test",
    "scenario_name": "2035-01-01_0",
    "scenario_first_step": 0,
    "env_seed": 0,
    "step_duration_s": 0.5
  },
  "session_duration_seconds": 86400,
  "kpis": []
}
```

| Field | Required | Default | Rule |
|---|---|---|---|
| `algorithm` | no | `algorithm_template.py` | `.py` file in `algorithms/` |
| `scenario.env_name` | yes | | Directory in `scenarios/` with `chronics/`. Built-in grid2op names are not supported |
| `scenario.scenario_name` | yes | | Chronic folder holding `prod_p`, `load_p`, `prod_v`, `load_q`, `prod_p_forecasted`, `load_p_forecasted`, `load_q_forecasted` |
| `scenario.scenario_first_step` | no | `0` | int ≥ 0; the step the participant takes over from |
| `scenario.env_seed` | no | `0` | int ≥ 0 |
| `scenario.step_duration_s` | no | `0.5` | 0 < x ≤ 60 |
| `session_duration_seconds` | yes | | 60 to 2 592 000 (30 days) |
| `kpis` | no | `[]` | Reserved. Accepted and recorded as a warning; nothing is computed yet |

Unknown keys are rejected. Every check runs before a job starts, so
`/control/execute` itself answers `failed` with the reason.

## Endpoints

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /control/execute` | orchestrator | Starts a session; returns the job id immediately |
| `GET /control/status/{job_id}` | orchestrator | `running` (progress = elapsed share of the window, ≤ 99), `complete`, `failed` |
| `GET /control/intermediate/{job_id}` | placeholder | Participant links, credentials, deadline, instructions. `409` before go-live, `404` if unknown |
| `GET /control/output/{job_id}` | orchestrator | DataReference to the result |
| `GET /control/data/{job_id}` | anyone with the reference | The result JSON |
| `POST /api/v1/recommendation` | `cab_recommendation` | The algorithm's recommendation for the posted observation; `[]` outside a session |
| `GET /api/v1/health` | setup script | Liveness plus the active session id |
| `GET /health` | Docker | Liveness |

`/control/*` (including `intermediate`) requires `SERVICE_API_KEY` when it is
set. `/api/v1/recommendation` requires `HAI_AGENT_TOKEN` when that is set. The
setup script gives `cab_recommendation` the same token.

`/control/status` cannot carry the intermediate object, because the shared
control plane's response model drops extra fields. The orchestrator does not
read intermediate results yet. Until it does, the object is also logged at
go-live and stored in the result under `session.intermediate`.

## Session lifecycle

| Step | What happens | On failure |
|---|---|---|
| Preflight | `GET` the simulator, `GET /cabrecommendation/api/v1/health` via the gateway, obtain a test-account token; 3 tries each | Job `failed`, naming the check and URL |
| Activate | Build a grid2op env exactly as the simulator does, import the algorithm, call `build_agent`, install the agent | Job `failed` with the exception |
| Configure | Rewrite the shared CONFIG.toml in place (all 12 keys) | Job `failed` |
| Go live | Fix the deadline, publish the intermediate object | |
| Wait | Until the deadline, progress every 30 s. Nothing else ends a session | |
| Collect | Remove the agent; `GET /cabhistoric/api/v1/traces?use_case=PowerGrid&date_start&date_end` (window ± 15 min), 4 tries with 5/15/45 s back-off | Job `failed`, with the query URL so the traces can be fetched by hand |

The agent is removed and the slot freed on every exit path.

### Agent endpoint behaviour

`cab_recommendation` forwards the simulator's context, and the wrapper reads
`context.data.observation` (the simulator's `obs.to_json()`). It loads the
observation into its own environment, calls `agent.act`, and answers with one
recommendation:
`{"title": "Do nothing" | "Recommended action", "description": str(action), "use_case": "PowerGrid", "agent_type": "IA", "actions": [action.to_json()], "kpis": {}}`.
The frontend applies `actions[0]`, and the simulator rebuilds the action with
`action_space().from_json`.

The endpoint never fails a session. It answers `[]` when there is no active
session, the observation is missing, `act` raises, or `act` takes longer than
`HAI_AGENT_ACT_TIMEOUT_SECONDS`. The last three are recorded in the result's
warnings, keeping the first 50.

**Limitation:** an observation rebuilt from JSON is not attached to the
participant's live environment. Algorithms that call `observation.simulate`
get the wrapper environment's forecasts, not the participant's.

## Result (`HAISessionResult`)

| Field | Contents |
|---|---|
| `metadata` | `service`, `task_id`, `algorithm`, `interactiveai_commit`, `grid2op_version`, `trace_query` (URL and window), `warnings` |
| `config` | The request with defaults filled in |
| `session` | `started_at`, `deadline`, `ended_at` (UTC, `Z`), and the `intermediate` object |
| `traces` | cabhistoric traces of the window, unchanged |
| `kpis` | `null` (reserved) |
| `survey` | `null`: InteractiveAI only downloads survey answers in the browser |
| `not_collected` | Why survey, session export and KPIs are absent |

`metadata.warnings` always says that traces are filtered by time and use case
only. Any other use of the test account during the window shows up in the
result.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SERVICE_API_KEY` | unset | Bearer on `/control/*` |
| `HAI_HOST_PORT` / `HAI_SELF_URL` | `8005` / `http://localhost:8005` | Loopback port; base of result references |
| `HAI_AGENT_TOKEN` | unset | Bearer on the agent endpoint; must match `cab_recommendation`'s `RL_AGENT_API_TOKEN` |
| `HAI_AGENT_ACT_TIMEOUT_SECONDS` | `20` | Per-recommendation budget (cab_recommendation waits 30 s) |
| `HAI_CAB_INTERNAL_URL` | `http://frontend:80` | Gateway as the service reaches it |
| `HAI_SIMULATOR_INTERNAL_URL` | `http://PowerGrid-simulator-app:5000` | Simulator as the service reaches it (preflight only) |
| `HAI_FRONTEND_PUBLIC_URL` / `HAI_SIMULATOR_PUBLIC_URL` | `http://localhost:3200` / `http://localhost:5122` | Links for the participant |
| `HAI_SIMULATOR_CAB_SERVER` | `http://host.docker.internal:3200/` | Server the participant picks in the simulator login |
| `HAI_CAB_USERNAME` / `HAI_CAB_PASSWORD` | `powergrid_user` / `test` | Test account |
| `HAI_TRACE_WINDOW_MARGIN_SECONDS` | `900` | Trace window widening on each side |
| `HAI_INTERACTIVEAI_COMMIT` | unset | Recorded in results |

## Dependencies

The wrapper must read the simulator's JSON, so it runs the simulator's grid2op
stack: `Grid2Op==1.9.8`, `LightSim2Grid==0.7.5`, `pandapower==2.13.1`. Two
non-obvious pins come with it:
- `pandas<2.3`: pandas 3 breaks pandapower 2.13's grid loading.
- `setuptools<81`: grid2op 1.9.8 imports `pkg_resources`.

## Tests

```bash
cd use-cases/dutch-node-hai-testing/services/human_ai_interaction_testing
python -m pytest tests/ -q
```

The tests cover the acceptance criteria and scenarios in spec section 9:
validation, CONFIG.toml written in place, the InteractiveAI client (token,
preflight, trace window, retries), the agent endpoint (token, empty answers,
timeouts, warnings), and the session lifecycle through the real control plane
(happy path, busy refusal, intermediate result, preflight / build / collection
failures, interruption).

The grid2op round-trip tests use the simulator's own
`env_ICAPS_input_data_test` from the submodule, and are skipped when grid2op or
lightsim2grid is not installed. To run everything with the exact image
dependencies:

```bash
cd use-cases
docker run --rm -v "$PWD:/uc" -w /uc/dutch-node-hai-testing/services/human_ai_interaction_testing \
  ghcr.io/ai-effect/dutch-node-hai-testing:latest sh -c "pip install -q pytest && python -m pytest tests/ -q"
```

**Not covered by the tests** (needs the full stack and a participant): the
browser round trip, the shape of a real `cab_recommendation` request, and the
timezone of stored trace dates (open questions 2 and 3 in the spec).
