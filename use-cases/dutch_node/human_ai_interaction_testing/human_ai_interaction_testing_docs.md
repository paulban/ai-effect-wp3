# Human-AI Interaction Testing Service — Documentation

*Auto-generated from source docstrings. Do not edit by hand.*

**Spec:** `human-ai-interaction-testing-spec.md`
**Generated:** 2026-06-29

---

## Overview

WP3 Dutch Node service that orchestrates human-operator evaluation sessions using
the InteractiveAI framework and hmisurveys. An AI vendor submits a HumanAISessionSpec;
the service launches an InteractiveAI Docker container; a human operator accesses the
GUI via browser, operates a grid2op power grid simulation, and fills out an hmisurveys
survey shown automatically at the end. Results (grid KPIs + survey outcomes) are written
to a shared volume and returned to the caller via a polling gRPC API.

---

## Modules

### `proto/human_ai_interaction_testing.proto`

Defines the gRPC service and all message types for the Human-AI Interaction Testing
data plane.

**Service:** `HumanAIInteractionTestingService`

| RPC | Input | Output |
|-----|-------|--------|
| `StartHumanAISession` | `HumanAISessionSpec` | `StartSessionResponse` |
| `GetSessionStatus` | `SessionStatusRequest` | `SessionStatusResponse` |
| `GetSessionResult` | `SessionResultRequest` | `SessionResultResponse` |

**Session phases (enum `SessionPhase`):**
`PENDING → GUI_READY → IN_PROGRESS → SURVEY → COMPLETED | FAILED`

**Key messages:**
- `HumanAISessionSpec` — what the AI vendor fills out (scenario, agent, survey, kpis, timeout)
- `StartSessionResponse` — `session_id` + `gui_url`
- `SessionStatusResponse` — `phase` + `error_message`
- `SessionResultResponse` — `kpis` and `survey_outcomes` as `map<string, MetricValue>`

---

### `common/session_manager.py`

Thread-safe registry of all active and completed testing sessions. Each session
is a `SessionState` dataclass tracking phase, gui_url, container_id, results, and
error_message. Phase transitions are monotonically enforced (FAILED is always
reachable as a terminal failure state).

#### Classes

##### `SessionPhase(IntEnum)`

Ordered session lifecycle phases. Values must only ever increase within a session.

| Name | Value | Meaning |
|------|-------|---------|
| `PENDING` | 1 | Container not yet ready |
| `GUI_READY` | 2 | InteractiveAI container up; browser-accessible |
| `IN_PROGRESS` | 3 | Operator is interacting with the GUI |
| `SURVEY` | 4 | Grid episode ended; survey is displayed |
| `COMPLETED` | 5 | Operator submitted survey; results written |
| `FAILED` | 6 | Timeout elapsed or container error |

##### `SessionState`

Dataclass holding all mutable state for a session. Never access fields outside
a SessionManager lock-protected context unless the session is terminal.

##### `SessionManager`

| Method | Description |
|--------|-------------|
| `create(session_id)` | Register a new session in PENDING; raises if duplicate |
| `advance_phase(session_id, new_phase, **fields)` | Atomic phase transition with optional field updates; raises on backwards transitions |
| `get(session_id)` | Return a shallow copy of the session state, or None |
| `has_active_session()` | True if any non-terminal session exists (v1 constraint check) |

#### Functions

##### `get_session_manager() → SessionManager`

Return the process-wide singleton. Used by gRPC servicer and HTTP control plane.

---

### `common/session_operations.py`

Core service logic: gRPC servicer, Docker container management, background result
polling, timeout enforcement, and container cleanup.

#### Configuration (environment variables)

| Variable | Default | Notes |
|----------|---------|-------|
| `HAI_INTERACTIVE_AI_IMAGE` | *(required)* | InteractiveAI Docker image |
| `HAI_INTERACTIVE_AI_PORT` | `8090` | Host port for GUI |
| `HAI_GUI_BASE_URL` | `http://host.docker.internal:8090` | Returned as `gui_url` |
| `HAI_RESULTS_HOST_PATH` | `/tmp/hai_sessions` | Host-side results base dir |
| `HAI_RESULTS_CONTAINER_PATH` | `/hai-sessions` | Container-side mount |
| `HAI_DOCKER_NETWORK` | `ai-effect-services` | Docker network for InteractiveAI |
| `HAI_RESULTS_FILENAME` | `session_result.json` | File written on survey submit |

#### Classes

##### `HumanAIInteractionTestingServicer`

gRPC servicer implementing the three RPCs.

| Method | Behaviour |
|--------|-----------|
| `StartHumanAISession` | Validates spec, checks single-session constraint, launches Docker container, starts polling thread, returns session_id + gui_url immediately |
| `GetSessionStatus` | Reads phase from SessionManager; maps to proto enum |
| `GetSessionResult` | Returns kpis + survey_outcomes if COMPLETED; FAILED_PRECONDITION otherwise |

#### Functions

##### `start_grpc_server() → grpc.Server`

Start the gRPC data plane server on port `GRPC_PORT` (default: 50051).

##### `_launch_interactive_ai_container(session_id, host_results_path, spec) → (container_id, gui_url)`

Launch InteractiveAI in web-app mode with the results directory mounted.
Passes scenario/agent/survey/kpi config via environment variables.

##### `_run_session_polling_thread(session_id, container_id, container_session_dir, timeout_seconds)`

Background daemon thread. Polls the results file every 5 seconds, transitions
to COMPLETED on file detection, FAILED on timeout. Always cleans up the container
on exit (FR-13).

##### `_parse_results_file(results_path) → (kpis, survey_outcomes)`

Parse the JSON file written by InteractiveAI. Expects `{"kpis": {...}, "survey_outcomes": {...}}`.
Keys are taken as-is — never hardcoded.

##### `_metric_value_from_any(value) → MetricValue`

Convert Python values to MetricValue protobuf messages. Mirrors benchmarking service
for result consistency.

---

### `common/control_interface.py`

FastAPI HTTP control plane consumed by the WP3 orchestrator.

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/control/execute` | Trigger a named method (StartHumanAISession) |
| `GET` | `/control/status/{task_id}` | Poll orchestrator-status string + progress |
| `GET` | `/control/output/{task_id}` | Get gRPC DataReference when COMPLETED |
| `GET` | `/health` | Health check (returns `{"status": "ok"}`) |

**Phase → orchestrator status mapping:**

| Phase | Status | Progress |
|-------|--------|----------|
| PENDING | pending | 0 |
| GUI_READY | running | 20 |
| IN_PROGRESS | running | 50 |
| SURVEY | running | 80 |
| COMPLETED | complete | 100 |
| FAILED | failed | 0 |

---

### `main.py`

Service entrypoint. Starts gRPC server then HTTP server (blocking).

---

## Requirements Coverage

| Requirement | Status | Implemented in |
|-------------|--------|---------------|
| FR-01 | ✓ | `session_operations.py: StartHumanAISession`, `_launch_interactive_ai_container()` |
| FR-02 | ✓ | `session_manager.py: SessionManager.advance_phase()`, `GetSessionStatus` |
| FR-03 | ✓ | `session_operations.py: GetSessionResult` |
| FR-04 | ✓ | `proto/human_ai_interaction_testing.proto`, `StartHumanAISession` validation |
| FR-05 | ✓ | `session_operations.py`: empty `scenario.name` uses InteractiveAI default |
| FR-06 | ✓ | `session_operations.py`: empty `agent.name` uses built-in agent |
| FR-07 | ⚠ TODO | Depends on OQ-1 (InteractiveAI survey-submit hook). Survey display is a feature of the InteractiveAI container itself; the WP3 service assumes it works correctly once the container is running. |
| FR-08 | ✓ | `session_operations.py: _parse_results_file()` |
| FR-09 | ✓ | `session_operations.py: _run_session_polling_thread()` (5s interval) |
| FR-10 | ✓ | `session_operations.py: _run_session_polling_thread()` timeout watchdog |
| FR-11 | ✓ | `session_operations.py: _metric_value_from_any()`, `GetSessionResult` response |
| FR-12 | ✓ | `dockerinfo.json`, `blueprint.json`, `docker-compose-all.yml` |
| FR-13 | ✓ | `session_operations.py: _stop_and_remove_container()` in finally block |
| FR-14 | ✓ | `session_manager.py: SessionState.error_message`, `GetSessionStatus` response |
| FR-15 | ✓ | `session_operations.py: _launch_interactive_ai_container()` — web-app mode launch; `Dockerfile` comment; `README.md` |
| NFR-01 | ✓ | `README.md`: Windows issues documented with WSL2 workaround |
| NFR-02 | ✓ | Web-app mode enforced via launch flags in `_launch_interactive_ai_container()` |
| NFR-03 | ✓ | `session_manager.py: has_active_session()` checked in `StartHumanAISession` |
| NFR-04 | ✓ | `control_interface.py: /health` endpoint; structured logging in all phase transitions |
| NFR-05 | ✓ | `_run_session_polling_thread()` transitions to FAILED with log on timeout |

---

## Open Items

| Item | Reason |
|------|--------|
| FR-07 (survey auto-display) | Depends on OQ-1: InteractiveAI must display hmisurveys automatically after the grid episode ends. WP3 service assumes this is handled inside the container; no WP3-side code change needed once OQ-1 is confirmed. |
| OQ-1 | Confirm/add survey-submit hook in InteractiveAI that writes `session_result.json` |
| OQ-2 | Enumerate hmisurveys output fields; update README and test fixtures |
| OQ-3 | Enumerate InteractiveAI KPI names; update README and test fixtures |
| OQ-4 | Set `HAI_INTERACTIVE_AI_IMAGE` in `.env` / `docker-compose-all.yml` |
| OQ-5 | Confirm InteractiveAI web-app port; update `HAI_INTERACTIVE_AI_PORT` default |
| OQ-6 | Verify Docker socket accessibility in deployment; update setup docs |
