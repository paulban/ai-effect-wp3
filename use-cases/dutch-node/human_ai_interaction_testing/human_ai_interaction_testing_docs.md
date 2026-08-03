# Human-AI Interaction Testing Service — Documentation

*Auto-generated from source docstrings.*

**Spec:** `human-ai-interaction-testing-spec.md`
**Generated:** 2026-06-29

---

## Overview

WP3 Human-AI Interaction Testing service. Orchestrates per-session Docker containers
for the InteractiveAI `powergrid-simulator-app` and the WP3-built `hai-survey-wrapper`,
collects results from both tools via a shared host-mounted volume, and exposes gRPC
and HTTP control-plane APIs for the WP3 orchestrator.

---

## Modules

### `common/session_manager.py`

Thread-safe session state manager tracking each testing session from PENDING through COMPLETED or FAILED.

#### `SessionPhase`

| Value | Meaning |
|---|---|
| `PENDING` (1) | Container not yet ready |
| `GUI_READY` (2) | powergrid-simulator-app is up and browser-accessible |
| `IN_PROGRESS` (3) | Operator is interacting with the GUI |
| `SURVEY` (4) | Grid episode ended; hai-survey-wrapper survey is displayed |
| `COMPLETED` (5) | Operator submitted survey; results written to volume |
| `FAILED` (6) | Timeout elapsed or container error |

#### `SessionManager` — key methods

| Method | Description |
|---|---|
| `create(session_id)` | Register a new session in PENDING phase |
| `advance_phase(session_id, new_phase, *, ...)` | Atomic phase transition with optional field updates |
| `get(session_id)` | Return snapshot of session state |
| `has_active_session()` | True if any non-terminal session exists (v1 guard) |
| `get_active_session_id()` | Return the single active session_id or None (FR-12 fallback) |

---

### `common/session_operations.py`

gRPC servicer, Docker container management, result polling, and the collect endpoint.

#### Environment variables

| Variable | Default | Description |
|---|---|---|
| `HAI_INTERACTIVE_AI_IMAGE` | _(required)_ | `powergrid-simulator-app` Docker image |
| `HAI_INTERACTIVE_AI_PORT` | `8090` | Host port for the simulator (container port 5000) |
| `HAI_GUI_BASE_URL` | `http://host.docker.internal:8090` | Returned as `gui_url` |
| `HAI_CAB_URL` | `http://frontend:80` | CAB platform URL forwarded to the simulator (FR-19) |
| `HAI_HMISURVEYS_IMAGE` | _(required)_ | `hai-survey-wrapper` Docker image |
| `HAI_HMISURVEYS_PORT` | `8091` | Host port for the survey wrapper (container port 80) |
| `HAI_SURVEY_BASE_URL` | `http://host.docker.internal:8091` | Returned as `survey_url` |
| `HAI_RESULTS_HOST_PATH` | `/tmp/hai_sessions` | Host path for per-session result dirs |
| `HAI_RESULTS_CONTAINER_PATH` | `/hai-sessions` | Mount point inside this service container |
| `HAI_DOCKER_NETWORK` | `ai-effect-services` | Docker network for sub-containers |
| `HAI_KPIS_FILENAME` | `kpis.json` | Written by `POST /collect/session-trace` |
| `HAI_SURVEY_FILENAME` | `survey_outcomes.json` | Written by hai-survey-wrapper |

#### gRPC RPCs

- **`StartHumanAISession`** — Launch both Docker containers, advance to GUI_READY, start polling thread. Returns `session_id`, `gui_url`, `survey_url` synchronously.
- **`GetSessionStatus`** — Return the current `SessionPhase`.
- **`GetSessionResult`** — Return `kpis` and `survey_outcomes` maps for a COMPLETED session.

#### HTTP endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/control/execute` | Execute a named method (`StartHumanAISession`) |
| `GET` | `/control/status/{task_id}` | Poll orchestrator status |
| `GET` | `/control/output/{task_id}` | Get gRPC result reference |
| `POST` | `/collect/session-trace` | Receive InteractiveAI session JSON; write `kpis.json` (FR-12) |
| `GET` | `/health` | Health check |

#### `_handle_collect_session_trace(body) -> tuple[dict, int]`

Receives the InteractiveAI historic-session JSON, resolves the active session, and
writes `kpis.json` to `RESULTS_CONTAINER_BASE_PATH / session_id / KPIS_FILENAME`.

Session ID resolution: prefers `body["wp3_session_id"]` (FR-20, OQ-1); falls back to
the single active session (`get_active_session_id()`) under the v1 single-session constraint.

Returns `({"status": "saved", "session_id": ...}, 200)` on success, or an error tuple.

---

### `hai-survey-wrapper/app.py`

Flask sidecar inside the `hai-survey-wrapper` Docker image. Proxied by nginx at `/api/`.

| Endpoint | Description |
|---|---|
| `GET /api/healthz` | Returns `{"status": "ok"}` HTTP 200 (FR-17) |
| `POST /api/save_results` | Receives survey JSON; writes `survey_outcomes.json` (FR-18) |

Environment variables: `HAI_RESULTS_PATH` (default `/results`), `HAI_SURVEY_FILENAME` (default `survey_outcomes.json`), `FLASK_PORT` (default `5000`).

### `hai-survey-wrapper/static/index.html`

Wrapper page: embeds `surveychainer.html` in a full-screen iframe, listens for
`window.postMessage` from the surveychainer on survey completion, POSTs to `/api/save_results`.

---

## Session lifecycle

```
StartHumanAISession
  ├─ Launch powergrid-simulator-app → gui_url (host port 8090)
  ├─ Launch hai-survey-wrapper      → survey_url (host port 8091)
  ├─ Advance to GUI_READY
  └─ Start polling thread (5s interval)
       │
       ├─ Operator uses InteractiveAI GUI (episodes, AI recommendations)
       ├─ Episode ends → operator logs out of InteractiveAI frontend
       │     └─ traceSessionExport.ts POSTs to POST /collect/session-trace
       │           └─ kpis.json written to shared volume
       │
       ├─ Operator navigates to survey_url
       ├─ Completes surveychainer → wrapper page POSTs to /api/save_results
       │           └─ survey_outcomes.json written to shared volume
       │
       └─ Both files present → COMPLETED (FR-10)
            Both containers stopped and removed (FR-14)
            Results available via GetSessionResult gRPC
```

---

## Infrastructure

### `docker-compose-all.yml`

One `docker compose up` starts the full stack: WP3 HAI testing service + full InteractiveAI CAB platform (15 services).

**Prerequisites:**
```bash
docker network create ai-effect-services
```

**Build `hai-survey-wrapper`** (from `human_ai_interaction_testing/`):
```bash
docker build -f hai-survey-wrapper/Dockerfile -t hai-survey-wrapper:latest .
```

**Build `powergrid-simulator-app`** (from `InteractiveAI/usecases_examples/PowerGrid/`):
```bash
docker build -t powergrid-simulator-app:latest .
```

**Rebuild InteractiveAI frontend** (after adding `VITE_WP3_COLLECT_URL`, FR-13):
The `frontend` service in `docker-compose-all.yml` includes `VITE_WP3_COLLECT_URL` as a build arg. Set it in `.env`:
```
VITE_WP3_COLLECT_URL=http://hai-testing-service:8080
```

---

## Requirements Coverage

| Requirement | Status | Implemented in |
|---|---|---|
| FR-01 | ✓ | `_launch_session_containers()` — powergrid-simulator-app, `ports={"5000/tcp": port}` |
| FR-02 | ✓ | `_launch_session_containers()` — hai-survey-wrapper, `ports={"80/tcp": port}` |
| FR-03 | ✓ | Both containers receive `volumes={host_path: {"/results", "rw"}}` |
| FR-04 | ✓ | `StartSessionResponse.gui_url` returned synchronously |
| FR-05 | ✓ | `StartSessionResponse.survey_url` returned synchronously |
| FR-06 | ✓ | `GetSessionStatus` gRPC |
| FR-07 | ✓ | `GetSessionResult` gRPC |
| FR-08 | ✓ | `_handle_collect_session_trace()` writes `kpis.json` |
| FR-09 | ✓ | `hai-survey-wrapper/app.py: save_survey_results()` writes `survey_outcomes.json` |
| FR-10 | ✓ | `_run_session_polling_thread()` — both files required |
| FR-11 | ✓ | `_run_session_polling_thread()` — timeout → FAILED |
| FR-12 | ✓ | `_handle_collect_session_trace()` + `create_collect_router()` at `POST /collect/session-trace` |
| FR-13 | ✓ | `traceSessionExport.ts` — `fetch()` POST after `download()` when `VITE_WP3_COLLECT_URL` is set |
| FR-14 | ✓ | `_run_session_polling_thread()` finally block — both containers stopped/removed |
| FR-15 | ✓ | `docker-compose-all.yml` — all CAB services with corrected paths |
| FR-16 | ✓ | `hai-survey-wrapper/Dockerfile`, `static/index.html`, `nginx.conf`, `supervisord.conf` |
| FR-17 | ✓ | `hai-survey-wrapper/app.py: health_check()` |
| FR-18 | ✓ | `hai-survey-wrapper/app.py: save_survey_results()` |
| FR-19 | ✓ | `CAB_PLATFORM_URL` constant; forwarded as `CAB_API_URL` env var to simulator container |
| FR-20 | ⚠ TODO | Blocked by OQ-1 — see Open Items |

---

## Open Items

**OQ-1 (blocks FR-20):** Session ID linking. Proposed: append `?session_id=<id>` to `gui_url`; frontend reads `window.location.search` and adds `wp3_session_id` to the POST body. Until resolved, the v1 single-active-session fallback is used in `_handle_collect_session_trace`.

**OQ-2:** Confirm `powergrid-simulator-app` internal port is 5000. Fix `_launch_session_containers` port mapping if different.

**OQ-3:** Confirm `hai-survey-wrapper` nginx internal port is 80 (as declared in `nginx.conf` and Dockerfile `EXPOSE 80`).

**OQ-4:** Which surveychainer parameters (pid, cond) should `static/index.html` pre-fill from the session context?

**Flask sidecar tests (FR-17/18):** 5 tests skipped in the WP3 service venv (Flask not installed). Enable with `pip install flask` or test via `docker run hai-survey-wrapper:latest`.
