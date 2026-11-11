# Orchestrated Human-AI Interaction Session Spec

**Status:** Draft
**Author:** Paul Bannmüller
**Date:** 2026-11-11
**Domain:** Software Feature — WP3 TEF Service Integration (Dutch node)
**Supersedes:** the session design in `human-ai-interaction-testing-spec.md` and the HAI parts of `dutch-node-target-architecture-spec.md` (slot pool, Redis, signed session links, session proxy, survey wrapper)

---

## 1. Overview

The AI-EFFECT orchestrator should be able to run a human-AI interaction (HAI) test the same way it runs a benchmark: submit a config, wait, get one JSON result back. The current HAI service does not do this cleanly. It depends on a modified InteractiveAI branch (`feat/wp3-simulator-config-api`) and on slot, Redis, token and proxy machinery that the use case does not need.

This spec replaces it with `RunHAISession` on a new container `hai-session-runner`, laid out like the benchmarking service. The orchestrator submits a config that names a benchmarking-style algorithm (`build_agent(env, context)`) and a grid2op scenario from local folders. The service makes that algorithm the recommendation agent of an already-running, **unmodified** InteractiveAI stack, and writes the simulator's `CONFIG.toml` for the scenario. It then publishes the participant's links and instructions as an intermediate result, and waits until a deadline while the participant plays one episode. At the deadline it collects InteractiveAI's server-side traces into one JSON artifact and completes.

InteractiveAI code is **not changed**. Wherever that limits the design, section 6.3 records the InteractiveAI change that would remove the limit.

---

## 2. Goals

1. The orchestrator starts an HAI session with one `RunHAISession` call whose input selects the algorithm, the scenario, and the session duration by name and value. It gets a job id back immediately.
2. The selected algorithm is what produces recommendations in the InteractiveAI frontend for the whole session, with no container restart.
3. The simulator loads the selected scenario when the participant clicks "Load simulation".
4. While the session runs, the participant links, test credentials, deadline, and instructions can be fetched at `GET /control/intermediate/{task_id}`.
5. At the deadline, one JSON artifact holding the session metadata and the InteractiveAI traces of the session window is stored, and `/control/output` references it.
6. A second session submitted while one is running is rejected immediately with an error naming the running session and its deadline.
7. The InteractiveAI submodule stays at an unmodified upstream commit. The stack is set up by a script in this repo, not by changing InteractiveAI.

## 3. Non-Goals

- **Any change to InteractiveAI code** (frontend, backend, simulator). Changes that would help are listed in 6.3, not implemented.
- **Collecting survey answers or the frontend session export.** InteractiveAI only downloads these in the participant's browser. `survey` is `null` in the result.
- **Computing KPIs.** `kpis` is accepted in the config and reserved. The result carries raw traces only.
- **Enforcing an episode length.** The prepared scenario folder (its chronics, from `scenario_first_step` on) defines the episode.
- **Driving the simulator.** The participant logs in, loads, starts, and resumes the simulation in the simulator UI. The service never calls simulator endpoints other than a reachability check.
- **Detecting that the participant finished.** The session always runs until the deadline.
- **Parallel sessions.**
- **Per-participant authentication.** Everyone uses the InteractiveAI test account.
- **Orchestrator changes.** The intermediate result has a service-side placeholder endpoint only.
- **Changes to `use-cases/common/`.**
- **A production proxy / single-port deployment.** Development-style host ports, as in benchmarking, are acceptable for this version.

---

## 4. Users & Stakeholders

| Role | Description | Relationship to this spec |
|------|-------------|--------------------------|
| AI vendor | Supplies the algorithm and requests an HAI test via the orchestrator | Primary user (caller) |
| Participant | A human operator who plays one episode in InteractiveAI + simulator and fills the survey | Primary user (end user) |
| Node operator | Runs the setup script once, places algorithms and scenarios in the folders | Primary user (operator) |
| Orchestrator (WP3) | Calls `/control/*`; will later relay the intermediate result | Integration partner |
| Paul Bannmüller | Owner; resolves conflicting requirements | Owner |
| InteractiveAI maintainers (ainetus upstream) | Own the code this spec must not change | Informed (section 6.3) |

---

## 5. Requirements

### 5.1 Functional Requirements

**Service and control plane**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-01 | The service exposes exactly one operation, `RunHAISession`, through `execute_RunHAISession` in `service.py`. It is served by `common.concurrent.create_app` through `handler.run`, as in benchmarking. | Must | Container name `hai-session-runner`, port 8080, image `ghcr.io/ai-effect/dutch-node-hai-testing:latest` |
| FR-02 | Input is a single inline base64 JSON input (same decoding as benchmarking `_decode_inline_json` / `_read_payload`) with the schema in 5.3. | Must | |
| FR-03 | Every input is validated before a job is accepted. An invalid request returns `ExecuteResponse(status="failed", error=<message naming the field>)` and no job starts. | Must | Rules in 5.3 |
| FR-04 | Valid requests run as a background job through `BatchJobRunner` with `MAX_CONCURRENT_JOBS=1`. The job id is returned immediately with status `running`. | Must | `common.batch_jobs` |
| FR-05 | If a session is active, `RunHAISession` returns `failed` with the error `HAI session <job_id> is running until <deadline ISO-8601 UTC>; retry after it completes.` This check happens before `BatchJobRunner.submit`. | Must | One at a time |
| FR-06 | `/control/status/{task_id}` reports `running` with `progress` = elapsed / `session_duration_seconds` × 100, capped at 99 until the result is stored, then `complete`, or `failed` with an error. | Must | Via `report_progress` |
| FR-07 | `/control/output/{task_id}` returns the DataReference to the stored artifact, and `/control/data/{task_id}` serves it. Data format `HAISessionResult`. | Must | Unchanged common behaviour |

**Intermediate result (orchestrator placeholder)**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-08 | The service adds `GET /control/intermediate/{task_id}` through `create_app(extra_routers=[...])`, protected by the same `SERVICE_API_KEY` check as `/control/*`. It returns the intermediate object (5.4) once the session is live, `404` for an unknown task, and `409` with `{"detail": "not ready"}` before the session is live. | Must | `/control/status` is **not** extended: its `response_model=StatusResponse` in `common` would strip extra fields |
| FR-09 | The intermediate object is also logged at INFO when the session goes live, and stored in the final result's `session.intermediate`. | Must | Lets an operator forward links before the orchestrator supports it |
| FR-10 | The code that builds the intermediate object is a single function, `build_intermediate_result(session) -> dict`, plus a docstring `TODO(orchestrator)` that marks where the orchestrator will consume it. | Should | Placeholder |

**Session lifecycle (job body)**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-11 | **Preflight:** before going live, the job checks that (a) the simulator answers `GET http://<HAI_SIMULATOR_INTERNAL_URL>/` with HTTP 200, (b) the InteractiveAI gateway answers `GET <HAI_CAB_INTERNAL_URL>/cabrecommendation/api/v1/health` with 200 (the path the gateway exposes for the recommendation service), and (c) an access token for the test account can be obtained (FR-21). Any failure fails the job with an error naming the failed check and URL. | Must | 3 attempts, 2 s apart, 5 s timeout each |
| FR-12 | **Algorithm activation:** the job builds the wrapper environment (FR-17), imports the selected algorithm file, calls `build_agent(env, context)` with the context in 5.5, and installs the returned agent as the active agent (FR-16). Import or build errors fail the job with the exception message. | Must | |
| FR-13 | **Simulator config:** the job writes `CONFIG.toml` to `HAI_SIMULATOR_CONFIG_PATH` (shared bind mount) with the keys in 5.6, writing **in place** (open with `"w"`, no temp file + rename). | Must | Single-file bind mount: a rename would break the mount |
| FR-14 | **Go live:** the job records `started_at` (UTC, at go-live) and `deadline = started_at + session_duration_seconds`, publishes the intermediate object, and logs it. | Must | |
| FR-15 | **Wait:** the job blocks until the deadline, waking at least every 30 s to report progress. It waits on a `threading.Event` so that tests can shorten the wait and shutdown can interrupt it. No other end condition exists. | Must | Deadline-only |
| FR-16a | **Close:** at the deadline the job deactivates the agent (the wrapper returns `[]` from then on), fetches the traces (FR-20), builds the result (5.7), and returns it to `BatchJobRunner`, which stores it and completes the task. Active-session state is cleared in a `finally` block on every exit path. | Must | |

**Agent wrapper (recommendation endpoint for InteractiveAI)**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-16 | The service serves `POST /api/v1/recommendation` and `GET /api/v1/health` on the same app (extra router). If `HAI_AGENT_TOKEN` is set, the POST requires `Authorization: Bearer <HAI_AGENT_TOKEN>` (401 otherwise). Health is open and returns `{"status": "ok", "active_session": <job_id or null>}`. | Must | `cabrecommendation` calls it as agent 1 (`RL_AGENT_API_URL`) |
| FR-17 | The wrapper environment is built **exactly** as the simulator builds its own (`Simulator.initialize_simulation`): `grid2op.make(<scenario dir>, backend=LightSimBackend(), data_feeding_kwargs={FromHandlers + CSVHandler("prod_p"/"load_p"/"prod_v"/"load_q") + PerfectForecastHandler(... "_forecasted") with h_forecast=[5,10,15,20,25,30]})`. It is then seeded with `env_seed`, set to the chronic `scenario_name` and reset. It is built once per session. | Must | Same action/observation space as the simulator |
| FR-18 | On POST with an active agent, the wrapper reads `body["context"]["data"]["observation"]` (the `obs.to_json()` dict the simulator sends), turns it into an observation of the wrapper env (`obs = env.observation_space(); obs.from_json(<dict>)`), calls `agent.act(obs)`, and returns **one** recommendation: `[{"title": "Do nothing" if action is do-nothing else "Recommended action", "description": str(action)[:2000], "use_case": "PowerGrid", "agent_type": "IA", "actions": [action.to_json()], "kpis": {}}]`. | Must | The frontend applies `actions[0]`, and the simulator does `act.from_json(...)` on it |
| FR-19 | The wrapper returns `200 []` when no session is active, when the observation is missing, or when `agent.act` raises. In the last two cases it logs a WARNING with the top-level keys of the body / the exception, and appends a one-line entry to the session's `warnings`, capped at 50 entries plus a count of the dropped ones. A wrapper failure never fails the session. | Must | `cabrecommendation` treats `[]` as "no recommendation" |
| FR-19a | Each POST is answered within `cabrecommendation`'s agent timeout (default 30 s). If `agent.act` has not returned after `HAI_AGENT_ACT_TIMEOUT_SECONDS` (default 20), the wrapper returns `[]` and records a warning. | Should | |

**Result collection**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-20 | At the deadline the job calls `GET <HAI_CAB_INTERNAL_URL>/cabhistoric/api/v1/traces?use_case=PowerGrid&date_start=<started_at − margin>&date_end=<deadline + margin>` with `Authorization: Bearer <token>`. `margin = HAI_TRACE_WINDOW_MARGIN_SECONDS` (default 900). It stores the returned list unchanged in `traces`. | Must | The API filters only by date and use case (no user/session field) |
| FR-20a | Trace fetching retries 3 times with 5 / 15 / 45 s back-off. If all attempts fail, the job **fails** with an error that includes the exact query URL, so the traces can be fetched manually (they stay in the cabhistoric DB). | Must | |
| FR-21 | Tokens are obtained with `POST <HAI_CAB_INTERNAL_URL>/auth/token`, form `username=<HAI_CAB_USERNAME>&password=<HAI_CAB_PASSWORD>&grant_type=password&client_id=opfab-client` (as in InteractiveAI `resources/getToken.sh`). Defaults: `powergrid_user` / `test`. A fresh token is fetched for preflight and again for collection. | Must | Test account only |

**Setup and deployment**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-22 | A new script `use-cases/dutch-node-hai-testing/scripts/setup_interactiveai.sh` brings up the InteractiveAI stack from the submodule, **following the steps of the reference `local_setup.sh` without calling or vendoring it**: start the backend with `config/dev/cab-standalone/docker-compose.sh`, exporting `RL_AGENT_API_URL=http://hai-session-runner:8080/api/v1/recommendation`, `RL_AGENT_API_TOKEN=$HAI_AGENT_TOKEN` and `RL_AGENT_NAME="HAI algorithm"`; rebuild `frontend` and `cabrecommendation` from source; wait for Keycloak; set the realm `dev` `frontendUrl` through the admin REST API; restart the frontend; wait for auth; run `resources/loadTestConf.sh`; start the simulator (FR-23); connect containers to the network (FR-24); reload the frontend nginx; verify (FR-25). Flags: `--clean` (tear down first), `--wipe` (also volumes), `-h`. | Must | Run once by the node operator; idempotent |
| FR-23 | The simulator is started with `docker compose -f <submodule>/usecases_examples/PowerGrid/docker-compose.local.yml -f use-cases/dutch-node-hai-testing/simulator.override.yml up -d --build --force-recreate app`. The override bind-mounts `runtime/simulator/CONFIG.toml` → `/code/config/CONFIG.toml` (rw) and `services/human_ai_interaction_testing/scenarios/` → `/code/Ressources/hai_scenarios` (ro). | Must | The local compose already mounts `.:/code`; nested mounts override |
| FR-23a | Before the first `compose up`, the setup script creates `runtime/simulator/CONFIG.toml` as a copy of the submodule's `usecases_examples/PowerGrid/config/CONFIG.toml` if it does not exist. Otherwise Docker would create a directory at that path. `runtime/` is git-ignored. | Must | |
| FR-24 | After the stack is up, the setup script runs `docker network connect ai-effect-services <c>` (ignoring "already connected") for `cab_recommendation`, `frontend` and `PowerGrid-simulator-app`. That way `cab_recommendation` reaches `hai-session-runner` by name, and the service reaches `frontend` and the simulator by name. No host port is needed for the agent path. | Must | Must be re-run after any of those containers is recreated |
| FR-25 | The setup script ends by verifying: the gateway `http://localhost:3200/cabrecommendation/api/v1/health` returns 200, `docker exec cab_recommendation` can reach `http://hai-session-runner:8080/api/v1/health` (warning only if the HAI service is not up yet), and the simulator answers on `http://localhost:5122/`. It prints the frontend and simulator URLs with credentials. | Should | |
| FR-26 | `use-cases/dutch-node-hai-testing/docker-compose.yml` is rewritten to one service, `hai-session-runner`, laid out like benchmarking: build context `..`, `SERVICE_API_KEY`, `SELF_URL=${HAI_SELF_URL:-http://localhost:${HAI_HOST_PORT:-8005}}`, `ARTIFACT_DIR=/artifacts`, `MAX_CONCURRENT_JOBS=1`, loopback port `127.0.0.1:${HAI_HOST_PORT:-8005}:8080`, named volume `hai_artifacts`, bind mounts `algorithms/` (ro), `scenarios/` (ro) and `runtime/simulator/CONFIG.toml` (rw), network `ai-effect-services` (external), healthcheck on `/health`, and the environment variables in 5.8. | Must | |
| FR-27 | Removed: `hai/`, `hai-survey-wrapper/`, `proxy/`, `docker-compose-all.yml`, `main.py`, `pyproject.toml` (if unused by the new layout), the old `tests/`, the Redis and slot services, and the `hmisurveys` submodule entry and directory (no longer used). | Must | `git rm`; `.gitmodules` updated |
| FR-28 | `connections.json` and `export/` (`blueprint.json`, `dockerinfo.json`, `generation_metadata.json`, plus a `.proto` if the generator emits one) are regenerated with `scripts/onboarding-export-generator.py` for container `hai-session-runner`, operation `RunHAISession`. The README notes that the portal package must be uploaded again. | Must | Renaming invalidates the uploaded package |
| FR-29 | `services/human_ai_interaction_testing/algorithms/` ships `algorithm_template.py` (identical to benchmarking's: a no-op agent) as the default. `scenarios/` ships only a `README.md` describing the required scenario format (5.3). | Must | |
| FR-30 | `run_workflow.sh` (service dir) and `submit-workflow.sh` (use-case root) mirror benchmarking's: check that the orchestrator and workers are up and that `hai-session-runner:8080` is reachable from a worker, submit the blueprint with a payload, poll `/control/intermediate` until ready and print it, then wait for completion and print a result summary (trace count, window, warnings). | Should | Payload from env vars / a JSON file argument |
| FR-31 | The use-case `README.md` and service `README.md` are rewritten for the new design: setup, inputs, participant flow, result shape, the InteractiveAI limits (6.3). | Must | |

### 5.2 Non-Functional Requirements

| ID | Category | Requirement | Target / Threshold |
|----|----------|-------------|-------------------|
| NFR-01 | Latency | `RunHAISession` returns (accepted or rejected) | < 2 s; validation does no network I/O |
| NFR-02 | Latency | Time from submission to intermediate result available | < 60 s on the dev machine (dominated by grid2op env build + `build_agent`) |
| NFR-03 | Latency | Wrapper overhead per recommendation, excluding `agent.act` | < 1 s |
| NFR-04 | Duration | Supported `session_duration_seconds` | 60 s – 2 592 000 s (30 days) |
| NFR-05 | Security | `/control/*` and `/control/intermediate/*` | Bearer `SERVICE_API_KEY` when set (common behaviour) |
| NFR-06 | Security | Agent endpoint | Bearer `HAI_AGENT_TOKEN` when set; not published on a host port |
| NFR-07 | Security | Inputs | Algorithm and scenario names must be bare names (no `/`, `\`, `..`, not absolute), as in benchmarking `_require_plain_name` |
| NFR-08 | Reliability | Artifacts survive a container restart | Named volume `hai_artifacts` |
| NFR-09 | Compatibility | Wrapper uses the simulator's grid2op stack | `Grid2Op==1.9.8`, `LightSim2Grid==0.7.5`, `numpy<2` (simulator pins `numpy==1.23.5`) |

### 5.3 Input schema (`RunHAISession` payload)

```json
{
  "algorithm": "algorithm_template.py",
  "scenario": {
    "env_name": "env_ICAPS_input_data_test",
    "scenario_name": "2035-01-01_0",
    "scenario_first_step": 0,
    "env_seed": 2118338672,
    "step_duration_s": 0.5
  },
  "session_duration_seconds": 86400,
  "kpis": []
}
```

| Field | Required | Default | Validation |
|---|---|---|---|
| `algorithm` | no | `algorithm_template.py` | Bare name, ends in `.py`, file exists in `ALGORITHM_DIRECTORY` |
| `scenario.env_name` | **yes** | — | Bare name; a directory in `SCENARIO_DIRECTORY` containing `chronics/` |
| `scenario.scenario_name` | **yes** | — | Bare name; directory `<env>/chronics/<scenario_name>` exists and contains, as `.csv` or `.csv.bz2`: `prod_p`, `load_p`, `prod_v`, `load_q`, `prod_p_forecasted`, `load_p_forecasted`, `load_q_forecasted` |
| `scenario.scenario_first_step` | no | `0` | int ≥ 0 |
| `scenario.env_seed` | no | `0` | int ≥ 0 |
| `scenario.step_duration_s` | no | `0.5` | number > 0 and ≤ 60 |
| `session_duration_seconds` | **yes** | — | int, 60 ≤ x ≤ 2 592 000 |
| `kpis` | no | `[]` | list of strings. Any non-empty list is accepted and adds the warning `"kpis requested but not computed in this version: <list>"` |

Unknown top-level or `scenario` keys are rejected with an error listing them. Built-in grid2op environment names are **not** supported: the simulator must find the env at a path.

### 5.4 Intermediate object

```json
{
  "task_id": "<job id>",
  "frontend_url": "<HAI_FRONTEND_PUBLIC_URL>",
  "simulator_url": "<HAI_SIMULATOR_PUBLIC_URL>",
  "credentials": {"username": "<HAI_CAB_USERNAME>", "password": "<HAI_CAB_PASSWORD>"},
  "simulator_server": "<HAI_SIMULATOR_CAB_SERVER>",
  "started_at": "<ISO-8601 UTC>",
  "deadline": "<ISO-8601 UTC>",
  "scenario": {"env_name": "...", "scenario_name": "..."},
  "instructions": ["<step 1>", "..."]
}
```

`instructions` (English, an ordered list of strings, kept as a module constant and formatted with the values above):

1. Open the simulator at `{simulator_url}`. Log in as `{username}` / `{password}` and choose the server `{simulator_server}`.
2. The simulation loads the prepared scenario. Do **not** change the scenario, seed or settings in the simulator dashboard. Click Start.
3. Open InteractiveAI at `{frontend_url}` in a second tab and log in with the same account. Choose the recommendation agent when asked.
4. When the simulator pauses on an event, decide in InteractiveAI (apply a recommendation or act yourself), then click **Resume** in the simulator.
5. Play until the scenario ends, then log out of InteractiveAI and complete the questionnaire that opens.
6. Finish before `{deadline}`. Results are collected automatically at that time.

### 5.5 `build_agent` context

```python
{
  "use_case": "hai_session",
  "scenario": {<the validated scenario block>},
  "session_duration_seconds": int,
  "scenario_directory": "<absolute path of the env dir in the service container>",
}
```

The `env` argument is the wrapper env from FR-17. The agent contract is benchmarking's: the returned object has `act(observation) -> grid2op action`.

### 5.6 Generated `CONFIG.toml`

All keys of the simulator's original file are written. The ones taken from the request are marked:

| Key | Value |
|---|---|
| `titre` | `"Simulator configuration"` |
| `env_name` | `"Ressources/hai_scenarios/<env_name>"` ← request |
| `env_seed` | ← request |
| `scenario_name` | ← request |
| `scenario_first_step` | ← request |
| `stepDuration_s` | ← request (`step_duration_s`) |
| `time_step_forecast` | `3` |
| `duration_step_forecast` | `3` |
| `refresh_frequency_step` | `3` |
| `step_start_security_analysis` | `5` |
| `assistant_path` | `"Ressources/XD_silly_repo"` |
| `assistant_seed` | `1227139268` |

The fixed values are module constants named after the keys, copied from the submodule's `config/CONFIG.toml` at commit `600a1ce`.

### 5.7 Result artifact (`HAISessionResult`)

```json
{
  "metadata": {
    "service": "hai-session-runner",
    "task_id": "...",
    "algorithm": "algorithm_template.py",
    "interactiveai_commit": "<HAI_INTERACTIVEAI_COMMIT or null>",
    "grid2op_version": "1.9.8",
    "trace_query": {"url": "...", "date_start": "...", "date_end": "...", "use_case": "PowerGrid"},
    "warnings": ["..."]
  },
  "config": { "<the validated request, defaults filled in>" },
  "session": {
    "started_at": "...", "deadline": "...", "ended_at": "...",
    "intermediate": { "<5.4 object>" }
  },
  "traces": [ "<cabhistoric trace objects, unchanged>" ],
  "kpis": null,
  "survey": null,
  "not_collected": {
    "survey": "InteractiveAI only downloads survey answers in the participant's browser (see spec 6.3, IAI-1).",
    "session_export": "InteractiveAI only downloads the session export in the participant's browser (IAI-1).",
    "kpis": "Not computed in this version."
  }
}
```

`warnings` always includes `"traces are filtered by time window and use case only; any other use of the test account in that window is included"`.

### 5.8 Environment variables (service)

| Variable | Default | Purpose |
|---|---|---|
| `SERVICE_API_KEY` | unset | Bearer on `/control/*` |
| `SELF_URL` | `http://localhost:8005` | Base of artifact references |
| `ARTIFACT_DIR` | `/artifacts` | Artifact store |
| `MAX_CONCURRENT_JOBS` | `1` | Must stay 1 |
| `ALGORITHM_DIRECTORY` | `/app/algorithms` | |
| `SCENARIO_DIRECTORY` | `/app/scenarios` | |
| `HAI_SIMULATOR_CONFIG_PATH` | `/app/runtime/CONFIG.toml` | Shared CONFIG.toml |
| `HAI_SIMULATOR_ENV_PREFIX` | `Ressources/hai_scenarios` | Path of the scenarios mount as the simulator sees it |
| `HAI_CAB_INTERNAL_URL` | `http://frontend:80` | Gateway as the service reaches it (traces, auth, health) |
| `HAI_SIMULATOR_INTERNAL_URL` | `http://PowerGrid-simulator-app:5000` | Simulator as the service reaches it (preflight only) |
| `HAI_FRONTEND_PUBLIC_URL` | `http://localhost:3200` | Link for the participant |
| `HAI_SIMULATOR_PUBLIC_URL` | `http://localhost:5122` | Link for the participant |
| `HAI_SIMULATOR_CAB_SERVER` | `http://host.docker.internal:3200/` | Server the participant selects in the simulator login |
| `HAI_CAB_USERNAME` / `HAI_CAB_PASSWORD` | `powergrid_user` / `test` | Test account |
| `HAI_AGENT_TOKEN` | unset | Bearer required on the agent endpoint; must equal `RL_AGENT_API_TOKEN` given to `cabrecommendation` |
| `HAI_AGENT_ACT_TIMEOUT_SECONDS` | `20` | FR-19a |
| `HAI_TRACE_WINDOW_MARGIN_SECONDS` | `900` | FR-20 |
| `HAI_INTERACTIVEAI_COMMIT` | unset | Recorded in metadata; the setup script prints the value to export |

---

## 6. Assumptions & Constraints

### 6.1 Assumptions

- **Assumption:** the InteractiveAI stack and simulator are already running (FR-22) before any session is submitted. The service never starts or restarts containers.
- **Assumption:** the simulator rereads `config/CONFIG.toml` on every `/load_simulation` (verified: `Simulator.load_and_edit_config` at `600a1ce`). Writing the file is therefore enough to set the next run's scenario.
- **Assumption:** in the recommendation request that `cabrecommendation` forwards, `context` is the context object the simulator posted (`{"date", "data": {"observation", "topology"}, "use_case"}`), so the observation is at `context.data.observation`. **To be verified against a captured request** during implementation (see 12, gotchas).
- **Assumption:** grid2op 1.9.8 observations support `from_json` on the dict produced by `to_json`. If not, the implementer uses the equivalent vector round-trip and documents it.
- **Assumption:** the simulator pauses at every event after `scenario_first_step` and waits indefinitely for Resume (verified: `Communicate.get_act_from_api`). A participant who starts late misses nothing.
- **Assumption:** one participant per session, using the test account. Nobody else uses that account during a session.

### 6.2 Constraints

- **Constraint:** InteractiveAI code is not modified. The submodule stays at an upstream `main` commit (currently `600a1ce`).
- **Constraint:** `use-cases/common/` is not modified.
- **Constraint:** Python 3.11 service image (`common` uses PEP 604 types at runtime), with the grid2op pins from NFR-09.
- **Constraint:** one session at a time. This follows from the single simulator, single `cabrecommendation` agent slot, shared test account, and traces that are only filterable by time.
- **Constraint:** active-session state lives in process memory (like `BatchJobRunner`). A service restart loses the running session (see risks).

### 6.3 Where an InteractiveAI change would be needed (not in scope)

| ID | Limitation today | InteractiveAI change that would lift it |
|---|---|---|
| IAI-1 | Survey answers (`surveychainer.html` → `Survey.vue`) and the session export (`traceSessionExport.ts`) are only downloaded in the browser; nothing reaches a server | Frontend POSTs both to a configurable URL (or to cabhistoric) at logout / survey finish |
| IAI-2 | No "participant finished" signal; the session must run until the deadline | Frontend emits a trace (e.g. `step: "SESSION_END"`) at logout and survey completion |
| IAI-3 | Applying a recommendation does not resume the simulator; the participant needs the simulator UI to press Resume, so two links are required | Frontend calls the simulator's `/continue_simulation` after an apply, or the simulator resumes when `/api/v1/recommendations` receives an action |
| IAI-4 | Traces carry no user or session id; the result window may include other activity on the account | `TraceModel` gains `user`/`session_id`, and the traces API filters by them |
| IAI-5 | The simulator has no max-steps setting; the episode length is whatever the scenario chronics contain | `CONFIG.toml` key `max_steps`, honoured in `run_simulator` |
| IAI-6 | `load_assistant` hardcodes `Ressources.XD_silly_repo.submission`, so `assistant_path` cannot select another local assistant | Import from `assistant_path` |
| IAI-7 | The simulator dashboard lets the participant change scenario, seed and settings | A locked / "study" mode that hides config forms |
| IAI-8 | Agents are configured only through env vars at container start | Runtime agent registration API (not needed by this design, which keeps a fixed wrapper URL) |

---

## 7. Dependencies

| Dependency | Owner | Status | Risk if unavailable |
|------------|-------|--------|---------------------|
| InteractiveAI submodule at upstream `main` (`600a1ce`) | ainetus / AI-EFFECT fork | Available | Stack cannot be set up |
| `config/dev/cab-standalone/docker-compose.sh`, `resources/loadTestConf.sh`, `usecases_examples/PowerGrid/docker-compose.local.yml` in the submodule | InteractiveAI | Available | Setup script breaks on upstream rename; pin the submodule |
| `use-cases/common` (`concurrent.create_app` with `extra_routers`, `BatchJobRunner`, `FileArtifactStore`) | WP3 | Available | — |
| Docker network `ai-effect-services` | WP3 | Available | Service, simulator and `cab_recommendation` cannot reach each other |
| `Grid2Op==1.9.8` / `LightSim2Grid==0.7.5` wheels for Python 3.11 | PyPI | **Unverified** | Open question 1 |
| `scripts/onboarding-export-generator.py` | WP3 | Available | Export files must be written by hand |
| A scenario in simulator format (e.g. a copy of the submodule's `Ressources/env_ICAPS_input_data_test`) in `scenarios/` | Node operator | Manual step | No session can be submitted |

---

## 8. Open Questions

| # | Question | Owner | Due |
|---|----------|-------|-----|
| 1 | Do `Grid2Op==1.9.8` and `LightSim2Grid==0.7.5` install on Python 3.11? If not: use the oldest versions that do and that read the simulator's `obs.to_json()` (check by a round-trip test on a captured observation), or run the wrapper in a Python 3.9 sidecar. | Implementer → Paul | Before the first image build |
| 2 | Do cabhistoric trace dates come in UTC or local time (the frontend exports "French local time")? The 900 s margin covers small skew, not a 1–2 h offset. Inspect one stored trace and set the query timezone accordingly. | Implementer | During FR-20 |
| 3 | Does the gateway require a bearer token for `GET /cabhistoric/api/v1/traces`? (The Flask view has no auth decorator.) The token is sent regardless; this only affects the troubleshooting docs. | Implementer | During FR-20 |

None of these blocks starting implementation. Each has a stated default.

---

## 9. Verification

### 9.1 Acceptance Criteria

- **FR-01/02/04:** **Given** the service is running with no active session, **when** `/control/execute` is called with method `RunHAISession` and a valid payload, **then** it returns `status: running` and a job id within 2 s.
- **FR-03:** **Given** a payload with `scenario.scenario_name` naming a chronic folder that lacks `load_q_forecasted`, **when** it is submitted, **then** the response is `failed`, its error names `scenario.scenario_name` and the missing file, and no job is registered.
- **FR-03/NFR-07:** **Given** `algorithm: "../service.py"`, **when** it is submitted, **then** it fails with an error naming `algorithm`.
- **FR-05:** **Given** a running session, **when** a second valid payload is submitted, **then** it fails immediately with an error containing the first job id and its deadline.
- **FR-06:** **Given** a running session at 50 % of its duration, **when** status is polled, **then** `progress` is between 45 and 55 and `status` is `running`.
- **FR-08/09:** **Given** a session that has gone live, **when** `GET /control/intermediate/{task_id}` is called with the API key, **then** it returns 200 with the 5.4 object, including 6 instructions with the URLs, credentials and deadline filled in. Without the key (when one is set) it returns 401. For an unknown id it returns 404.
- **FR-11:** **Given** the simulator is not reachable, **when** a session is submitted, **then** the job fails within about 20 s with an error naming the simulator URL, and a new session can be submitted afterwards.
- **FR-12/16/18:** **Given** a live session with `algorithm_template.py`, **when** the agent endpoint receives a recommendation request containing a real simulator observation, **then** it returns one recommendation titled `Do nothing` whose `actions[0]` is accepted by `env.action_space().from_json(...)`.
- **FR-19:** **Given** no active session, **when** the agent endpoint is called, **then** it returns `200 []`.
- **FR-13:** **Given** a session with `env_name: X, scenario_name: Y`, **when** it goes live, **then** `runtime/simulator/CONFIG.toml` on the host contains `env_name = "Ressources/hai_scenarios/X"` and `scenario_name = "Y"`, and the bind-mounted file inside the simulator container shows the same content (same inode, mount not broken).
- **FR-15/16a/20:** **Given** a session with `session_duration_seconds: 60`, **when** 60 s pass, **then** the task becomes `complete`, and the artifact at `/control/output` has `traces` as a list, `survey: null`, `kpis: null`, and `session.ended_at ≥ session.deadline`.
- **FR-20a:** **Given** cabhistoric is down at the deadline, **when** collection runs, **then** after 3 retries the task is `failed` with an error containing the trace query URL, and the next session can be submitted.
- **FR-22–25:** **Given** a machine with Docker and no InteractiveAI containers, **when** `scripts/setup_interactiveai.sh` is run and then `docker compose up -d --build` is run in the use-case dir, **then** `docker exec cab_recommendation curl -sf http://hai-session-runner:8080/api/v1/health` succeeds, the frontend answers on `:3200`, and the simulator answers on `:5122`.
- **FR-27/28:** **Given** the change is complete, **when** the repo is searched, **then** there are no references to `hai-testing-service`, `StartHumanAISession`, `hai-slot-`, `hai-redis` or `hmisurveys` outside `.claude/specs/` and git history, and `export/dockerinfo.json` names `hai-session-runner`.

### 9.2 Test Scenarios

pytest, in `services/human_ai_interaction_testing/tests/`. The simulator, gateway and clock are faked: HTTP via `respx` or `httpx.MockTransport`, the wait via an injectable `threading.Event` or clock. Grid2op is real for the wrapper tests, using a tiny scenario fixture (the submodule's `env_ICAPS_input_data_test` copied, or a test-only minimal env).

| Scenario | Input / State | Expected Result | Covers |
|----------|--------------|-----------------|--------|
| Happy path | Valid payload, fakes healthy, duration shortened | running → intermediate 200 → complete; artifact matches 5.7 | FR-01–16a, FR-20 |
| Validation: missing required | No `session_duration_seconds` | `failed`, names the field | FR-03 |
| Validation: path traversal | `env_name: "../x"` | `failed` | NFR-07 |
| Validation: unknown key | `scenario.max_steps: 10` | `failed`, lists `max_steps` | FR-03 |
| Validation: kpis given | `kpis: ["operation_score"]` | Accepted; warning in result | 5.3 |
| Busy | Second submit during a live session | `failed` with first id + deadline | FR-05 |
| Preflight fail | Simulator fake returns 502 | Job `failed`, names URL; state cleared | FR-11, FR-16a |
| Algorithm raises in `build_agent` | Algorithm file raising `RuntimeError("x")` | Job `failed` with `x`; state cleared | FR-12 |
| Intermediate before live | Poll before go-live | 409 | FR-08 |
| Agent: no session | POST | `[]` | FR-19 |
| Agent: bad body | POST without `context.data.observation` | `[]` + warning recorded | FR-19 |
| Agent: token | `HAI_AGENT_TOKEN` set, wrong bearer | 401 | FR-16 |
| Agent: round-trip | Real observation JSON from the fixture env | One recommendation; `actions[0]` reloads via `from_json` | FR-17, FR-18 |
| Agent: slow act | Agent sleeping past the timeout | `[]` + warning | FR-19a |
| CONFIG write in place | Pre-existing file | Inode unchanged, all 12 keys present | FR-13 |
| Trace retry exhausted | Traces fake 500 ×4 | `failed` with query URL | FR-20a |
| Trace window | started_at, deadline, margin | Query params exact | FR-20 |

Manual end-to-end (documented in the README, run once before sign-off): setup script → service up → `submit-workflow.sh` with a 15-minute session → participant flow from the instructions (load, start, apply at least one recommendation from "HAI algorithm", resume, log out, survey) → result has the agent's `RECOMMENDATIONS` / `AWARD` traces in the window.

### 9.3 Definition of Done

- [ ] All Must requirements implemented
- [ ] All 9.2 tests pass with `python -m pytest tests/ -q`
- [ ] Manual end-to-end run completed once, and its result JSON attached to the PR
- [ ] Open questions 1–3 answered, with the answers recorded in the service README
- [ ] Old implementation removed (FR-27); `export/` regenerated (FR-28)
- [ ] READMEs rewritten (FR-31), including the IAI-1…IAI-8 table
- [ ] InteractiveAI submodule unchanged (`git diff --submodule` shows only an intentional pointer bump, if any)
- [ ] Reviewed and signed off by Paul Bannmüller

---

## 10. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Participant changes the scenario or config in the simulator dashboard | Med | Med | Instructions say not to; the configured scenario is in `config`; IAI-7 |
| Other use of the test account during the window pollutes traces | Low | Med | Warning in result; one session at a time; IAI-4 |
| Algorithm uses `obs.simulate()` / forecasts, which a JSON-rebuilt observation cannot provide | Med | High (agent errors → no recommendations) | FR-19 records the error as a warning; README states the limitation; OQ1 test |
| grid2op version mismatch between wrapper and simulator corrupts actions | Med | High | NFR-09 pins; round-trip test; OQ1 |
| Service restart mid-session loses the session (in-memory state) | Low | High (orchestrator task never completes) | Documented; traces stay in cabhistoric and can be fetched for the logged window; restart policy `unless-stopped` |
| `docker network connect` lost when `frontend` / `cab_recommendation` / simulator is recreated | Med | High (no recommendations, preflight fails) | Setup script is idempotent and re-run; preflight error message tells the operator to re-run it |
| Trace timezone mismatch drops traces | Med | High | OQ2; margin; manual E2E check |
| Participant never presses Resume → episode incomplete | Med | Low (still collected at deadline) | Instructions; IAI-3 |
| Survey data not collected | Certain | Med | Documented in `not_collected`; IAI-1 |

---

## 11. Revision History

| Version | Date | Author | Summary of changes |
|---------|------|--------|-------------------|
| 0.1 | 2026-11-11 | Paul Bannmüller (with Claude) | Initial draft |

---

## 12. Implementation Handoff

> This section is the single source of truth for the implementation agent.
> Every decision needed to start coding is captured here.

**Language & runtime:** Python 3.11 (service image `python:3.11-slim`); Bash for the setup and workflow scripts.

**Execution model:** Mixed. FastAPI app (sync endpoints, as in `common.concurrent`). Each session runs on one `BatchJobRunner` worker thread that blocks until the deadline on a `threading.Event`. The agent endpoint runs `agent.act` in a single-worker `concurrent.futures.ThreadPoolExecutor`, so `HAI_AGENT_ACT_TIMEOUT_SECONDS` can be enforced. Active-session state is one module-level object guarded by a `threading.Lock`.

**Entry point:** `use-cases/dutch-node-hai-testing/services/human_ai_interaction_testing/service.py` → `execute_RunHAISession(request: ExecuteRequest) -> ExecuteResponse`. `if __name__ == "__main__": run(sys.modules[__name__], extra_routers=[agent_router, intermediate_router], artifact_store=get_job_runner().artifact_store)` (check `run`'s kwargs in `common/concurrent.py`; mirror benchmarking's `service.py` bottom block).

**Code location:**
- `services/human_ai_interaction_testing/`: `service.py`, `handler.py` (copy of benchmarking's), `session.py` (active-session state, lifecycle, intermediate builder), `agent_wrapper.py` (env build, observation rebuild, router), `simulator_config.py` (CONFIG.toml render/write), `interactiveai_client.py` (token, health, traces), `Dockerfile`, `requirements.txt`, `algorithms/algorithm_template.py`, `scenarios/README.md`, `run_workflow.sh`, `README.md`, `tests/`.
- Use-case root: `docker-compose.yml` (rewritten), `simulator.override.yml` (new), `scripts/setup_interactiveai.sh` (new), `submit-workflow.sh`, `connections.json`, `export/*` (regenerated), `README.md`, `.gitignore` (+ `runtime/`).

**Existing interfaces to respect:**
- `common.concurrent`: `ExecuteRequest`, `ExecuteResponse`, `create_app(service_module, *, extra_routers, status_provider, output_provider, artifact_store)`, `run(service_module, **create_app_options)`, `_check_api_key` dependency (reuse it for `/control/intermediate`; if it is private, use the same `SERVICE_API_KEY` bearer logic and note it).
- `common.batch_jobs`: `BatchJobRunner.submit(request, work(report_progress) -> dict, data_format)`, `build_runner()`.
- Benchmarking `service.py` helpers to copy (not import): `_decode_inline_json`, `_read_payload`, `_require_plain_name`, `set_job_runner` / `get_job_runner`.
- Algorithm contract: `build_agent(env, context) -> obj with act(observation)`.
- InteractiveAI recommendation contract: request `{event, context, cognitive_snapshot?, num_reco?}`, response `list[{title, description, use_case, agent_type, actions, kpis}]`; the frontend applies `actions[0]`, and the simulator does `env.action_space().from_json(actions[0])`.
- InteractiveAI auth: `POST /auth/token` form (FR-21). Traces: `GET /cabhistoric/api/v1/traces?use_case&date_start&date_end`.

**Library constraints:** `fastapi`, `uvicorn`, `pydantic` (versions as in `common`'s requirements), `httpx` (outbound HTTP), `toml` (same writer as the simulator), `Grid2Op==1.9.8`, `LightSim2Grid==0.7.5`, `numpy<2`. Tests: `pytest`, `respx`. No Docker SDK, no Redis, no gRPC.

**Test framework:** pytest (`python -m pytest tests/ -q` from the service dir); conftest adds `use-cases/` to `sys.path` so that `common` imports, as in benchmarking's tests.

**Style notes:** research-code style — modular, descriptive names, full docstrings (Google style, Args/Returns/Raises), module docstrings that explain *why*, comments on non-obvious decisions with the FR id. Match benchmarking's `service.py` tone.

**Requirement priority order for implementation:**
1. FR-27 — remove the old implementation (clean slate; keep the submodule `InteractiveAI`, drop `hmisurveys`)
2. FR-02/03, 5.3 — payload parsing and validation
3. FR-13, 5.6 — CONFIG.toml rendering and in-place write
4. FR-17/18/19/19a/16 — agent wrapper (env build, observation round-trip, router, token)
5. FR-21/11/20/20a — InteractiveAI client (token, preflight, traces)
6. FR-04/05/12/14/15/16a/06 — session lifecycle on `BatchJobRunner`, busy check, progress
7. FR-08/09/10, 5.4 — intermediate router and builder
8. 5.7 — result assembly
9. FR-26, Dockerfile — compose + image
10. FR-22/23/23a/24/25 — setup script + simulator override
11. FR-28/30/31, FR-29 — export regeneration, workflow scripts, READMEs, algorithm template

**Known gotchas / non-obvious decisions:**
- **Do not extend `/control/status`.** `StatusResponse` in `common` is the `response_model` and drops extra fields. The intermediate result is only at `/control/intermediate/{task_id}`.
- **CONFIG.toml must be written in place** (`open(path, "w")`). It is a single-file bind mount shared with the simulator, which also writes it in place. A temp file + `os.replace` detaches the mount.
- **The runtime CONFIG.toml must exist before `docker compose up`**, or Docker creates a directory there (FR-23a).
- **Build the wrapper env with the simulator's exact `grid2op.make` arguments** (FromHandlers / CSVHandler / PerfectForecastHandler, `h_forecast=[5,10,15,20,25,30]`, LightSimBackend). A plain `grid2op.make(path)` may give a different chronics class and break the observation round-trip.
- **Verify the observation location** by logging the keys of one real request from `cab_recommendation` before relying on `context.data.observation`. If it differs, adapt FR-18 and record it in the README.
- **Call `agent.act(obs)` with one argument** (benchmarking contract), not grid2op's `act(obs, reward, done)`.
- **Do-nothing check:** `action == env.action_space({})`, the same comparison the simulator uses.
- **`agent_type` must be `"IA"`** (the frontend's `services.ts` type).
- **The busy check comes before `BatchJobRunner.submit`.** The runner's own capacity error does not name the deadline. Keep `MAX_CONCURRENT_JOBS=1` anyway, as a second guard.
- **Clear the active session in `finally`** on every job exit path, or the service stays "busy" forever.
- **Timestamps:** store and return UTC ISO-8601 with `Z`. For the trace query, see OQ2.
- **The agent path uses the Docker network, not a host port:** `RL_AGENT_API_URL=http://hai-session-runner:8080/api/v1/recommendation`, which only works after `docker network connect ai-effect-services cab_recommendation` (FR-24).
- **`docker-compose.sh` sources `.secrets` in a subshell.** Export `RL_AGENT_API_URL` / `RL_AGENT_API_TOKEN` / `RL_AGENT_NAME` in the setup script's environment before calling it, and make sure a `.secrets` file does not override them. If `.secrets` exists and sets them, warn and let the script's values win by exporting them after sourcing, as the reference script does for `--a3s`.
- **The simulator's local compose (`docker-compose.local.yml`) is "local" config with port 5122.** The default `docker-compose.yml` there is the server config. Always use `-f docker-compose.local.yml -f <our override>`.
- **After recreating the simulator, reload the frontend nginx** (`docker exec frontend nginx -c /personal-conf/nginx.conf -s reload`, falling back to `nginx -s reload`, then to `docker restart frontend`). Otherwise the `/powergrid-simu/` proxy keeps a stale upstream IP and applying recommendations fails.
- **Keycloak `frontendUrl` must equal `HAI_FRONTEND_PUBLIC_URL`**, or tokens fail with 401.

**What the implementation must NOT do:**
- Modify anything inside `services/human_ai_interaction_testing/InteractiveAI/` (no files added either; overrides live in the use-case dir).
- Call or vendor `local_setup.sh`.
- Call simulator control endpoints (`/login`, `/load_simulation`, `/start_simulation`, `/continue_simulation`, `/edit_config`, …). Only `GET /` for preflight.
- Modify `use-cases/common/` or the orchestrator.
- Mount the Docker socket or start/stop containers from the service.
- Compute KPIs, end the session early, or try to collect survey/session-export files.
- Reintroduce Redis, slot pools, signed tokens, or a session proxy.
- Support grid2op built-in environment names for `env_name`.
