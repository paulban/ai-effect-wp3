# Dutch Node — Human-AI Interaction Testing

A human-in-the-loop test of an AI algorithm as a power-grid operator's
assistant. The orchestrator starts a session with a config. A participant then
plays one episode in [InteractiveAI](https://github.com/ainetus/InteractiveAI),
with the algorithm's recommendations on screen. When the session's time window
ends, the service returns one JSON result.

**Spec:** `.claude/specs/hai-orchestrated-session-spec.md`
**Service details:** [`services/human_ai_interaction_testing/README.md`](services/human_ai_interaction_testing/README.md)

---

## How a session works

```
orchestrator ──/control/execute RunHAISession──▶ hai-session-runner
                                                    │ validate the request
                                                    │ preflight: simulator, gateway, test account
                                                    │ load the algorithm → serve it as the agent
                                                    │ write the simulator's CONFIG.toml
                                                    ▼
          participant links + instructions  ◀── /control/intermediate/{job_id}
                                                    │
participant ──▶ PowerGrid simulator (:5122)         │      cab_recommendation
             └─▶ InteractiveAI frontend (:3200) ──────▶  POST /api/v1/recommendation
                                                    │      (RL_AGENT_API_URL → hai-session-runner)
                                                    │
                                    deadline ──▶ fetch cabhistoric traces of the window
                                                    ▼
orchestrator ◀── /control/output → /control/data/{job_id}   (HAISessionResult JSON)
```

1. The orchestrator submits a request that names an **algorithm** in
   `algorithms/`, a **scenario** in `scenarios/`, and the
   **session duration**, which is the participant's time window.
2. The service makes the algorithm the recommendation agent of the running
   InteractiveAI stack. It also writes the simulator's `CONFIG.toml`, which the
   simulator reads on the next "Load simulation".
3. The participant's links, test credentials, deadline and instructions are
   available at `GET /control/intermediate/{job_id}`. This is the placeholder
   until the orchestrator relays intermediate results.
4. The participant logs into the simulator, loads and starts the scenario,
   decides on each event in InteractiveAI, and presses Resume in the simulator.
   At the end they log out of InteractiveAI and answer the questionnaire.
5. At the deadline the service collects InteractiveAI's traces of the session
   window into one JSON artifact, and the job completes.

Only one session runs at a time. A second request is refused with the running
session's deadline.

## Running it

**Once per machine**, bring up InteractiveAI. The script builds and starts the
backend, frontend and simulator from the submodule, then wires them to this
service:

```bash
git submodule update --init --recursive
docker network create ai-effect-services   # if it does not exist
use-cases/dutch-node-hai-testing/scripts/setup_interactiveai.sh
```

Then start the session service and the orchestrator:

```bash
export HAI_INTERACTIVEAI_COMMIT=<printed by the setup script>   # optional
cd orchestrator && docker compose up -d
cd ../use-cases/dutch-node-hai-testing && docker compose up -d --build
```

Put at least one scenario in `services/human_ai_interaction_testing/scenarios/`
(see the README there; the simulator's own test environment is a quick start).
Then run a session through the orchestrator:

```bash
SESSION_DURATION_SECONDS=900 ./submit-workflow.sh
```

The script prints the participant instructions as soon as the session is live,
waits for the deadline, and fetches the result.

Re-run `setup_interactiveai.sh` after recreating `frontend`,
`cab_recommendation` or the simulator, because their connection to
`ai-effect-services` does not survive a recreate.

## InteractiveAI is used unmodified

The submodule stays at an upstream commit. Everything this use case needs is
done from outside InteractiveAI:

| Need | How, without changing InteractiveAI |
|---|---|
| Use the selected algorithm | `RL_AGENT_API_URL` points `cab_recommendation` at this service's agent endpoint |
| Select the scenario | The simulator's `config/CONFIG.toml` is a bind mount this service rewrites (`simulator.override.yml`) |
| Reach the stack | `docker network connect ai-effect-services` for `frontend`, `cab_recommendation` and the simulator |
| Collect results | The cabhistoric traces API, with the test account |

That approach has limits. Each row below needs an InteractiveAI change to lift
it (spec section 6.3):

| ID | Limitation today | InteractiveAI change needed |
|---|---|---|
| IAI-1 | Survey answers and the session export only download in the browser, so `survey` is `null` in the result | Post both to a configurable URL at logout / survey completion |
| IAI-2 | No "participant finished" signal, so a session always runs to its deadline | Emit a trace at logout and survey completion |
| IAI-3 | Applying a recommendation does not resume the simulator, so the participant needs the simulator UI to press Resume | Frontend calls `/continue_simulation` after an apply, or the simulator resumes on receiving an action |
| IAI-4 | Traces have no user or session id, so the result may include other activity on the test account in the window | Record and filter traces by user / session |
| IAI-5 | No max-steps setting, so the scenario's chronic defines the episode length | A `max_steps` key in CONFIG.toml |
| IAI-6 | `load_assistant` hardcodes `XD_silly_repo`, so the simulator's local assistant cannot be chosen | Import from `assistant_path` |
| IAI-7 | The simulator dashboard lets the participant change scenario and settings | A locked study mode |
| IAI-8 | Agents are configured only by env vars at container start | Runtime agent registration (not needed here: the agent URL is fixed) |

## Layout

| Path | Contents |
|---|---|
| `docker-compose.yml` | The `hai-session-runner` service |
| `simulator.override.yml` | Mounts for InteractiveAI's simulator (CONFIG.toml, scenarios) |
| `scripts/setup_interactiveai.sh` | One-time InteractiveAI setup |
| `submit-workflow.sh` | Runs a session through the orchestrator |
| `connections.json`, `export/` | Onboarding package (`hai-session-runner`, `RunHAISession`) |
| `runtime/` | Git-ignored: the shared CONFIG.toml and the backend's env file |
| `services/human_ai_interaction_testing/` | Service code, tests, algorithms, scenarios, InteractiveAI submodule |

The export was regenerated for the new container and operation names, so
**the package on the portal must be uploaded again**:

```bash
python scripts/onboarding-export-generator.py use-cases/dutch-node-hai-testing \
  use-cases/dutch-node-hai-testing/export --overwrite --zip
```
