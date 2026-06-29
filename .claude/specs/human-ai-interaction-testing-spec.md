# Human-AI Interaction Testing Service Spec

**Status:** Draft
**Author:** Paul Bannmüller
**Date:** 2026-06-29
**Domain:** Software Feature — WP3 TEF Service Integration

---

## 1. Overview

The WP3 Human-AI Interaction Testing (HAI) service lets a TEF orchestrator run a structured human-in-the-loop evaluation of an AI-assisted power-grid operator workflow. When the orchestrator calls `StartHumanAISession`, the service launches two per-session Docker containers — `powergrid-simulator-app` (the grid simulation GUI) and `hai-survey-wrapper` (the post-episode questionnaire) — on top of a persistently running InteractiveAI CAB platform. The operator interacts with both tools; the service collects the resulting KPI JSON and survey JSON via a shared host-mounted volume, then returns the combined results through three gRPC RPCs.

This spec replaces the previous scaffold design and is the authoritative contract for both the remaining backend implementation and the new `hai-survey-wrapper` Docker image.

---

## 2. Goals

1. One `docker compose up` in `docker-compose-all.yml` starts the full CAB platform and the WP3 HAI service; no manual steps needed before calling `StartHumanAISession`.
2. Each `StartHumanAISession` call launches exactly two per-session containers: `powergrid-simulator-app` and `hai-survey-wrapper`.
3. The operator receives two browser URLs (`gui_url`, `survey_url`) from the `StartSessionResponse`; both are navigable immediately after the RPC returns.
4. KPI data from the InteractiveAI session trace reaches the shared volume without the operator uploading a file manually.
5. Survey results from the hmisurveys questionnaire reach the shared volume without the operator uploading a file manually.
6. The session transitions to `COMPLETED` only when both result files are present; neither file alone is sufficient.
7. Both per-session containers are cleaned up after every session, regardless of outcome.
8. The service integrates cleanly into the existing WP3 orchestrator control-plane protocol (HTTP `POST /control/execute` + gRPC `GetSessionResult`).

---

## 3. Non-Goals

- Launching the CAB platform per-session. The full CAB stack (15+ services) is pre-deployed and must be running before `StartHumanAISession` is called.
- Modifying the PowerGrid simulator Python code (`PowerGrid_poc_simulator_app.py`, `Simulator.py`, `Listener.py`).
- Per-session Keycloak authentication or operator account provisioning.
- Multi-session concurrency (v1 permits one active session at a time; `has_active_session()` guard remains).
- Agent upload by the AI vendor at session time.
- Real-time KPI streaming during the episode (results are collected only at episode end).
- Modifying `surveychainer.html` or any other file in the `hmisurveys` static asset tree.

---

## 4. Users & Stakeholders

| Role | Description | Relationship to this spec |
|---|---|---|
| TEF orchestrator | Automated WP3 pipeline that calls the gRPC/HTTP control plane | Primary caller |
| Grid operator | Human who navigates the InteractiveAI GUI and fills in the survey | User of both browser URLs |
| AI vendor | Supplies scenario, agent, KPI list via `HumanAISessionSpec` | Provides session inputs |
| WP3 Dutch node developer | Implements and maintains this service | Owner |

---

## 5. Requirements

### 5.1 Functional Requirements

| ID | Requirement | Priority | Notes |
|---|---|---|---|
| FR-01 | `StartHumanAISession` launches `powergrid-simulator-app` container on the configured host port | Must | Image from `HAI_INTERACTIVE_AI_IMAGE` env var |
| FR-02 | `StartHumanAISession` launches `hai-survey-wrapper` container on the configured host port | Must | Image from `HAI_HMISURVEYS_IMAGE` env var |
| FR-03 | Both containers mount the per-session results directory from the host at `/results` (read-write) | Must | Same host path, same bind mount |
| FR-04 | `StartSessionResponse.gui_url` points to the operator-accessible URL of `powergrid-simulator-app` | Must | Returned synchronously before containers are healthy |
| FR-05 | `StartSessionResponse.survey_url` points to the operator-accessible URL of `hai-survey-wrapper` | Must | Returned synchronously |
| FR-06 | `GetSessionStatus` returns the current `SessionPhase` (PENDING → GUI_READY → IN_PROGRESS → SURVEY → COMPLETED \| FAILED) | Must | Phase transitions are monotonic |
| FR-07 | `GetSessionResult` returns `kpis` and `survey_outcomes` maps for completed sessions | Must | gRPC error `FAILED_PRECONDITION` if not yet COMPLETED |
| FR-08 | `kpis.json` is written to `/results` by the `WP3 collect endpoint` when the operator logs out of InteractiveAI | Must | See FR-12; file written server-side, not browser-side |
| FR-09 | `survey_outcomes.json` is written to `/results` by the `hai-survey-wrapper` Flask sidecar when the operator submits the survey | Must | Flask sidecar receives the results via POST from the wrapper page |
| FR-10 | The background polling thread transitions to COMPLETED only when both `kpis.json` AND `survey_outcomes.json` are present in `/results` | Must | Neither file alone triggers completion |
| FR-11 | The background polling thread transitions to FAILED after `session_timeout_seconds` regardless of which files are present | Must | Timeout check precedes file check each poll cycle |
| FR-12 | WP3 service exposes `POST /collect/session-trace` HTTP endpoint | Must | Receives InteractiveAI session JSON; writes `kpis.json` to active session results dir |
| FR-13 | `traceSessionExport.ts` in the InteractiveAI frontend is modified to POST the session JSON to `VITE_WP3_COLLECT_URL` alongside the existing browser download | Must | ~5 lines of TypeScript; `VITE_WP3_COLLECT_URL` is a new build-time env var |
| FR-14 | Both per-session containers are stopped and removed in the `finally` block of the polling thread | Must | Applies on COMPLETED, FAILED, and unexpected error |
| FR-15 | `docker-compose-all.yml` includes the full InteractiveAI CAB platform services so `docker compose up` starts everything | Must | CAB platform services copied from `InteractiveAI/config/dev/cab-standalone/docker-compose.yml` |
| FR-16 | `hai-survey-wrapper` Docker image bundles hmisurveys static HTML/JS and serves a wrapper `index.html` that embeds `surveychainer.html` in an iframe | Must | WP3-owned Dockerfile |
| FR-17 | `hai-survey-wrapper` Flask sidecar exposes `GET /healthz` returning HTTP 200 | Should | Used by WP3 service health check and Docker healthcheck |
| FR-18 | `hai-survey-wrapper` Flask sidecar exposes `POST /save_results` accepting a JSON body and writing it to `/results/survey_outcomes.json` | Must | Called from the wrapper page's `window.postMessage` listener |
| FR-19 | `powergrid-simulator-app` container receives `HAI_CAB_URL` env var so it connects to the pre-running CAB platform | Must | CAB platform URL is static; set in `docker-compose-all.yml` or `.env` |
| FR-20 | Session ID linking: `gui_url` includes `?session_id=<session_id>` as a query parameter so the InteractiveAI frontend can include it in the `POST /collect/session-trace` body | Should | **Blocked by OQ-1** — proposed approach; may change |

---

## 6. Assumptions & Constraints

- **Assumption:** The full CAB platform is running and healthy before `StartHumanAISession` is called. WP3 does not health-check the CAB platform at session start.
- **Assumption:** `powergrid-simulator-app` is the "web-app / Case 2" mode (single `powergrid-simulator-app` image, port 5100) as described in `InteractiveAI/usecases_examples/PowerGrid/README.md`. The separate `app` + `api` two-container Case 1 mode is out of scope.
- **Assumption:** The hmisurveys static files are in `use-cases/dutch_node/human_ai_interaction_testing/hmisurveys/` and the `hai-survey-wrapper` Dockerfile copies them into the image at build time.
- **Assumption:** `VITE_POWERGRID_SIMU` URL (baked into the InteractiveAI frontend at build time) already points to `powergrid-simulator-app` at the correct address. This is not managed by WP3.
- **Constraint:** The Docker SDK requires `/var/run/docker.sock` to be mounted into the WP3 service container. This is already present in `docker-compose-all.yml`.
- **Constraint:** The existing `session_manager.py`, `session_operations.py`, and `human_ai_interaction_testing.proto` define the interfaces that must be respected — field names and function signatures must not change.
- **Constraint:** `POLL_INTERVAL_SECONDS = 5.0` is fixed and must not change.
- **Constraint:** v1 permits one active session at a time. `has_active_session()` guard is not relaxed.

---

## 7. Dependencies

| Dependency | Owner | Status | Risk if unavailable |
|---|---|---|---|
| InteractiveAI CAB platform (`docker-compose.yml` from `InteractiveAI/config/dev/cab-standalone/`) | AI4REALNET / Dutch node | Available in repo copy at `InteractiveAI/` | Session cannot proceed |
| `powergrid-simulator-app` Docker image | AI4REALNET / Dutch node | Must be built locally; not on a public registry | `StartHumanAISession` returns error |
| `hai-survey-wrapper` Docker image | WP3 (this spec) | New image to build | `StartHumanAISession` returns error |
| `traceSessionExport.ts` modification | WP3 (this spec) | Small change to InteractiveAI frontend | `kpis.json` never written; session times out |
| `VITE_WP3_COLLECT_URL` baked into InteractiveAI frontend at build time | WP3 + InteractiveAI build | Requires a frontend rebuild | `POST /collect/session-trace` never called |

---

## 8. Open Questions

| # | Question | Owner | Due |
|---|---|---|---|
| OQ-1 | How does the WP3 session ID reach the `POST /collect/session-trace` POST body? Proposed: pass as `?session_id=xxx` query param on `gui_url`; frontend reads from `window.location.search` and includes it in the POST. Needs confirmation that the frontend can read URL params. **FR-20 is blocked by this.** | Dutch node dev | Before FR-20 implementation |
| OQ-2 | What is the exact port `powergrid-simulator-app` exposes inside the container? Assumed 5000 (Flask default) → mapped to host 5100 via Docker. Confirm by running the image. | Dutch node dev | Before FR-01 implementation |
| OQ-3 | What is the exact port `hai-survey-wrapper` should expose inside the container? Proposed: 8080 (nginx/Flask combo), mapped to host port from `HAI_HMISURVEYS_PORT` env var. | Dutch node dev | Before FR-02 / FR-16 implementation |
| OQ-4 | Which `surveychainer.html` parameter (participant ID, condition) should be pre-filled by the wrapper page, and from what source? Proposed: `pid` = session_id, `cond` = scenario name from session spec. | Dutch node dev | Before FR-16 / FR-18 implementation |

---

## 9. Verification

### 9.1 Acceptance Criteria

**FR-01/02 — Two containers launched per session:**
> Given the service is running and `HAI_INTERACTIVE_AI_IMAGE` and `HAI_HMISURVEYS_IMAGE` are set, when `StartHumanAISession` is called, then `docker ps` shows exactly two new containers with labels `hai.session_id=<id>` and `hai.role=interactive-ai` and `hai.role=hmisurveys` respectively.

**FR-03 — Shared volume mount:**
> Given a session has been started, when a file is written to `/results/kpis.json` inside the `powergrid-simulator-app` container, then the same file appears at `<HAI_RESULTS_HOST_PATH>/<session_id>/kpis.json` on the host.

**FR-04/05 — URLs returned immediately:**
> Given `StartHumanAISession` is called, when the response is received, then `gui_url` is non-empty and navigable in a browser, and `survey_url` is non-empty and navigable in a browser, without waiting for any health check.

**FR-06 — Phase progression:**
> Given a session was started, when `GetSessionStatus` is called immediately, then `phase == GUI_READY`. When the polling thread detects both result files, then `phase == COMPLETED`.

**FR-07 — Results retrieval:**
> Given a session is COMPLETED, when `GetSessionResult` is called, then `kpis` map contains the keys from `kpis.json` and `survey_outcomes` map contains the keys from `survey_outcomes.json`.

**FR-08/09/10 — Both files required:**
> Given only `kpis.json` is written, when the polling thread runs, then the session remains in its current phase. Given only `survey_outcomes.json` is written, then the same. Given both files are written, then the session transitions to COMPLETED.

**FR-11 — Timeout:**
> Given a session with `session_timeout_seconds = 60` is started and no result files are written, when 60 seconds have elapsed, then `GetSessionStatus` returns `phase == FAILED` with a non-empty `error_message` naming the missing files.

**FR-12/13 — Collect endpoint:**
> Given the InteractiveAI frontend calls `POST /collect/session-trace` with a valid session JSON body and a known `session_id`, when the request is received, then `kpis.json` is written to `<results_dir>/<session_id>/kpis.json`.

**FR-14 — Container cleanup:**
> Given a session completes or fails, when `GetSessionStatus` returns a terminal phase, then `docker ps` shows neither `hai-ia-<id>` nor `hai-survey-<id>` containers.

**FR-15 — Single compose up:**
> Given `docker-compose-all.yml` is present, when `docker compose -f docker-compose-all.yml up -d` is run, then all CAB platform services and the WP3 HAI service are running without additional commands.

**FR-16/18 — Survey wrapper:**
> Given `hai-survey-wrapper` is running, when the operator opens `survey_url` in a browser, then `surveychainer.html` is displayed inside an iframe. When the operator completes the survey, then `POST /save_results` is called by the wrapper page and `survey_outcomes.json` appears in `/results`.

### 9.2 Test Scenarios

| Scenario | Input / State | Expected Result | Covers |
|---|---|---|---|
| Happy path | Both result files written within timeout | Session → COMPLETED, both result maps non-empty | FR-10, FR-07 |
| KPI only | Only `kpis.json` written | Session stays in progress until timeout | FR-10 |
| Survey only | Only `survey_outcomes.json` written | Session stays in progress until timeout | FR-10 |
| Timeout no files | No files written, timeout = 5s | Session → FAILED, error names both missing files | FR-11 |
| Timeout one file | Only `kpis.json` written, timeout = 5s | Session → FAILED, error names missing survey file | FR-11 |
| Missing image | `HAI_INTERACTIVE_AI_IMAGE` unset | `StartHumanAISession` returns `success=False` with clear message | FR-01 |
| Concurrent session | Second `StartHumanAISession` while session active | Returns `RESOURCE_EXHAUSTED` | NFR (existing) |
| Invalid JSON in kpis.json | `kpis.json` contains `not-json` | Session → FAILED, error describes parse failure | FR-08 |
| Survey wrapper health | `GET /healthz` on `hai-survey-wrapper` | HTTP 200 | FR-17 |
| Collect endpoint | `POST /collect/session-trace` with valid JSON + known session_id | `kpis.json` written to correct dir | FR-12 |

### 9.3 Definition of Done

- [ ] All Must requirements implemented and acceptance criteria passing
- [ ] `hai-survey-wrapper` Docker image builds and passes `GET /healthz`
- [ ] `traceSessionExport.ts` change builds without TypeScript errors; existing browser download still works
- [ ] `docker-compose-all.yml` runs all CAB platform services alongside the WP3 HAI service
- [ ] All existing tests in `test_session_manager.py` and `test_session_operations.py` pass (37/37)
- [ ] New tests cover FR-08, FR-09, FR-10, FR-12, FR-18 acceptance criteria
- [ ] OQ-1 resolved and FR-20 implemented or explicitly deferred
- [ ] README.md updated with operator workflow, new env vars, and image build instructions

---

## 10. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| InteractiveAI frontend cannot read URL query params for session_id (OQ-1) | Medium | High — `kpis.json` never written | Fall back to a fixed well-known path or require operator to copy session_id manually |
| `powergrid-simulator-app` exposes a different internal port than assumed | Medium | Medium — container port binding fails | Confirm OQ-2 by running the image before implementing FR-01 |
| `traceSessionExport.ts` POST fails silently in production (no server listening) | Low | High — silent data loss | Add error logging to the `fetch()` call; test with a stub server |
| CAB platform not running when `StartHumanAISession` is called | High | High — session fails immediately | Add a startup health-check probe in `docker-compose-all.yml` for the WP3 service |
| Survey wrapper `window.postMessage` not fired if operator closes browser early | Medium | High — `survey_outcomes.json` never written; session times out | Document in README; consider a manual fallback upload endpoint |

---

## 11. Revision History

| Version | Date | Author | Summary of changes |
|---|---|---|---|
| 0.1 | 2026-06-29 | Paul Bannmüller | Initial draft — two-container per-session design, CAB platform pre-deployed, hai-survey-wrapper image, collect endpoint for InteractiveAI KPIs |

---

## 12. Implementation Handoff

> This section is the single source of truth for the implementation agent.
> Every decision needed to start coding is captured here.

**Language & runtime:** Python 3.11 for the WP3 service; TypeScript (Vite/Vue 3) for the `traceSessionExport.ts` change; Python 3.11 + nginx for the `hai-survey-wrapper` image.

**Execution model:** Async I/O is not used. gRPC server uses `ThreadPoolExecutor(max_workers=10)`. Background polling thread is a `daemon=True` `threading.Thread`. The Flask sidecar in `hai-survey-wrapper` is synchronous (standard `flask run`).

**Entry point:** `use-cases/dutch_node/human_ai_interaction_testing/main.py` (existing). The new `POST /collect/session-trace` endpoint must be registered in the FastAPI control-plane app (see `common/control_interface.py`).

**Code locations:**
- WP3 service: `use-cases/dutch_node/human_ai_interaction_testing/common/session_operations.py` — add `_handle_collect_session_trace()` and register it in `session_handlers`
- `docker-compose-all.yml`: `use-cases/dutch_node/human_ai_interaction_testing/docker-compose-all.yml` — add all CAB platform services
- `hai-survey-wrapper`: new directory `use-cases/dutch_node/human_ai_interaction_testing/hai-survey-wrapper/` containing `Dockerfile`, `app.py` (Flask sidecar), `static/index.html` (wrapper page)
- Frontend change: `use-cases/dutch_node/human_ai_interaction_testing/InteractiveAI/frontend/src/utils/traceSessionExport.ts` — add ~5 lines after line 618 (`download(...)` call)

**Existing interfaces to respect:**
- `advance_phase(session_id, new_phase, *, gui_url, survey_url, container_id, survey_container_id, volume_name, error_message, kpis, survey_outcomes, session_metadata)` — do not change signature
- `SessionState` fields — do not rename or remove existing fields
- `StartSessionResponse` proto — field 4 = `gui_url`, field 5 = `survey_url` — do not renumber
- `_parse_results_file(path) -> dict[str, Any]` — returns a plain dict; do not change return type
- `POLL_INTERVAL_SECONDS = 5.0` — do not change
- `session_handlers` dict in `session_operations.py` — add `"CollectSessionTrace": _handle_collect_session_trace`

**Library constraints:**
- WP3 service: existing deps only (grpcio, fastapi, uvicorn, docker). No new packages.
- `hai-survey-wrapper`: Flask (lightweight; no heavy frameworks). nginx as static file server. Both in same Docker image via supervisord or a simple entrypoint script.
- TypeScript change: no new imports; use the standard `fetch()` Web API already available in Vue 3 / Vite.

**Test framework:** pytest (existing). New tests go in `tests/test_session_operations.py`.

**Style notes:** research-code style — modular, descriptive names, full docstrings on every public function. All new constants go in the env-var configuration block at the top of `session_operations.py`. No magic numbers.

**Requirement priority order for implementation:**

1. **FR-15** — Add CAB platform services to `docker-compose-all.yml` (prerequisite for everything; no code change)
2. **FR-12** — Add `POST /collect/session-trace` endpoint in `session_operations.py`; write `kpis.json` to active session results dir
3. **FR-16 + FR-18** — Build `hai-survey-wrapper`: Dockerfile, nginx config, Flask sidecar with `/healthz` and `/save_results`, wrapper `index.html`
4. **FR-03** — Verify shared volume mount already in `_launch_session_containers`; confirm internal container port for `hai-survey-wrapper`
5. **FR-13** — Modify `traceSessionExport.ts`: add `fetch(VITE_WP3_COLLECT_URL, ...)` after `download(...)` call
6. **FR-19** — Add `HAI_CAB_URL` env var pass-through to `powergrid-simulator-app` in `_launch_session_containers`
7. **FR-17** — `GET /healthz` on `hai-survey-wrapper` (part of FR-16 build, listed separately for test coverage)
8. **FR-20** — Append `?session_id=<id>` to `gui_url` in `StartHumanAISession` (blocked by OQ-1; implement once OQ-1 is resolved)

**Known gotchas / non-obvious decisions:**
- `kpis.json` is NOT written by the simulator or the frontend download. It is written by the WP3 `POST /collect/session-trace` endpoint when the InteractiveAI frontend POSTs the session JSON on logout. The polling thread waits for the file WP3 itself writes.
- `survey_outcomes.json` is NOT a download in the browser. The `hai-survey-wrapper` Flask sidecar writes it to `/results` when `POST /save_results` is called from the wrapper page's `window.postMessage` listener.
- The wrapper `index.html` must set `surveychainer.html` as the iframe `src`. The `surveychainer.html` fires `window.top.postMessage(results, '*')` on survey completion. The parent wrapper page listens for this message and POSTs to `http://localhost:<flask_port>/save_results`.
- `traceSessionExport.ts` line 618: `download(json, 'application/json;charset=utf-8', sessionFileName(session, 'json'))` — add the `fetch()` POST immediately after this line, inside the same `try` block.
- `VITE_WP3_COLLECT_URL` must be set at InteractiveAI frontend build time (Vite bakes it in). It must point to the WP3 service's HTTP control-plane address (e.g. `http://hai-testing-service:8080`). The endpoint path is `/collect/session-trace`.
- `hai-survey-wrapper` Flask sidecar and nginx run in the same container. Use a `supervisord.conf` or a minimal shell `entrypoint.sh` that starts both processes. nginx listens on port 80 (static files), Flask listens on port 5000 (API). An nginx `location /api/ { proxy_pass http://127.0.0.1:5000; }` block routes API calls. The container exposes one external port (e.g. 8080) mapped by nginx.
- Per-session results directory path: `RESULTS_CONTAINER_BASE_PATH / session_id`. The `POST /collect/session-trace` handler must look up the active session's `volume_name` (= session_id) from the session manager to resolve the write path.
- Container name prefix `hai-ia-{session_id[:8]}` and `hai-survey-{session_id[:8]}` already used in `_launch_session_containers`; keep these.
- The `HAI_INTERACTIVE_AI_IMAGE` env var currently points to the full CAB platform image. **This is wrong for the revised design** — after this spec, it should point only to `powergrid-simulator-app`. The `docker-compose-all.yml` change (FR-15) makes the CAB platform start separately; the per-session container is only the simulator.

**What the implementation must NOT do:**
- Do not modify `PowerGrid_poc_simulator_app.py`, `Simulator.py`, `Listener.py`, or any other Python file in `InteractiveAI/usecases_examples/PowerGrid/`.
- Do not modify `surveychainer.html` or any other file in the hmisurveys static asset tree.
- Do not change the `_parse_results_file` return type (must stay `dict[str, Any]`).
- Do not change `POLL_INTERVAL_SECONDS`.
- Do not remove the `has_active_session()` concurrency guard.
- Do not hardcode session IDs, container names, or file paths — all must derive from env vars or session_id.
- Do not introduce any new required Python packages beyond what is already in the service `requirements.txt` or `Dockerfile`.
