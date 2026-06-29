# Human-AI Interaction Testing Service Spec

**Status:** Draft
**Author:** Paul Bannmüller
**Date:** 2026-06-29
**Domain:** Software Feature / Architecture

---

## 1. Overview

The Human-AI Interaction Testing Service (`human_ai_interaction_testing`) is a new WP3 gRPC microservice that orchestrates an operator-in-the-loop evaluation session using the InteractiveAI framework. An AI vendor submits a testing configuration; the service launches the InteractiveAI GUI in a Docker container; a human operator accesses the GUI via browser, operates a grid2op power grid simulation, and completes an hmisurveys questionnaire automatically shown after the session ends. The service collects grid performance KPIs and survey outcomes via a shared Docker volume and returns them to the caller through a polling API. This complements the existing automated benchmarking service with subjective and behavioral human factors measurement.

---

## 2. Goals

1. Provide a gRPC-accessible service that AI vendors can invoke to run a human-AI interaction test session.
2. Launch and manage the InteractiveAI Docker container per session using the Docker Python SDK.
3. Return a browser-accessible `gui_url` in `StartSessionResponse` so the test coordinator can share it with the human operator.
4. Expose a session lifecycle via a phase status enum: PENDING → GUI_READY → IN_PROGRESS → SURVEY → COMPLETED | FAILED.
5. Collect results (InteractiveAI grid KPIs + hmisurveys outcomes) via a shared Docker volume and expose them via `GetSessionResult`.
6. Support a configurable session timeout after which the session transitions to FAILED.
7. Follow the same Python/gRPC service structure as the existing benchmarking service.
8. Integrate into the WP3 orchestrator via `blueprint.json` and `dockerinfo.json`.

---

## 3. Non-Goals

- Custom agent upload via URI or `AlgorithmPayload` (future milestone).
- Scenario generation via the data synthesizer (future milestone; InteractiveAI default scenario used for now).
- Multi-operator parallel sessions for the same session ID.
- Real-time streaming of operator actions or grid state to the caller.
- Authentication or access control for the `gui_url`.
- Modification of InteractiveAI or hmisurveys source code beyond the minimal hook needed to write results to the shared volume.
- Session state persistence across WP3 service restarts (known v1 limitation).

---

## 4. Users & Stakeholders

| Role | Description | Relationship to this spec |
|------|-------------|--------------------------|
| AI Vendor | Submits testing config, receives results after session | Primary caller of gRPC API |
| Human Operator | Uses InteractiveAI GUI, fills out hmisurveys survey | End-user of the browser GUI |
| WP3 Researcher / Test Coordinator | Runs the service, shares `gui_url` with operator | Infrastructure operator |
| WP3 Orchestrator | Invokes RPCs as part of a pipeline topology | Automated caller |

---

## 5. Requirements

### 5.1 Functional Requirements

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-01 | `StartHumanAISession(HumanAISessionSpec)` launches the InteractiveAI Docker container and returns `session_id` + `gui_url` | Must | Container managed via Docker Python SDK |
| FR-02 | `GetSessionStatus(SessionStatusRequest)` returns the current phase of a session | Must | Phases: PENDING, GUI_READY, IN_PROGRESS, SURVEY, COMPLETED, FAILED |
| FR-03 | `GetSessionResult(SessionResultRequest)` returns grid KPIs and survey outcomes for a COMPLETED session; returns error if not yet COMPLETED | Must | |
| FR-04 | `HumanAISessionSpec` includes: scenario config, agent config, survey selection, KPI list, session timeout | Must | All fields required |
| FR-05 | Scenario config defaults to InteractiveAI's built-in default scenario | Must | Extensible to data synthesizer in a future milestone |
| FR-06 | Agent config defaults to the agent built into InteractiveAI | Must | Extensible to URI/payload in a future milestone |
| FR-07 | hmisurveys survey is served from the same InteractiveAI container and shown automatically after the grid session ends | Must | Same browser tab, no second URL |
| FR-08 | InteractiveAI writes results (grid KPIs + survey outcomes) to a shared Docker volume as a JSON file on survey submit | Must | Requires minimal hook in InteractiveAI — see Open Questions |
| FR-09 | WP3 service polls the shared volume and transitions session to COMPLETED when the results file appears | Must | Internal polling interval: 5 seconds |
| FR-10 | Session transitions to FAILED if the configured timeout elapses before COMPLETED | Must | Timeout field in `HumanAISessionSpec` |
| FR-11 | Result structure uses `map<string, MetricValue>` for both KPIs and survey outcomes, mirroring the benchmarking service | Must | Reuse or replicate `MetricValue` from `benchmarking.proto` |
| FR-12 | Service registers in `dockerinfo.json`; a `blueprint.json` pipeline definition is provided | Must | Follow Dutch node conventions |
| FR-13 | InteractiveAI container is stopped and removed when a session reaches COMPLETED or FAILED | Should | Prevent resource leaks |
| FR-14 | `GetSessionStatus` includes an error message string when phase is FAILED | Should | Aids debugging |
| FR-15 | InteractiveAI is launched in web-app / browser-served simulator mode | Must | More stable than alternative launch modes in current implementation |

### 5.2 Non-Functional Requirements

| ID | Category | Requirement | Target / Threshold |
|----|----------|-------------|-------------------|
| NFR-01 | Compatibility | Primary runtime target is Linux; Windows dev environment may have InteractiveAI compatibility issues | Document Windows workaround in README; do not block on it |
| NFR-02 | Stability | Always use InteractiveAI's browser-based web-app simulator mode | Non-negotiable; other modes are less stable |
| NFR-03 | Scalability | One active session per service instance is sufficient for v1 | Concurrent sessions are out of scope |
| NFR-04 | Observability | Service emits structured logs for all session phase transitions | Follow benchmarking service logging conventions |
| NFR-05 | Reliability | Graceful FAILED transition on timeout with log entry | Prevents sessions hanging indefinitely |

---

## 6. Assumptions & Constraints

- **Assumption:** InteractiveAI can be configured to write a results JSON file to a mounted Docker volume on survey submit, either via an existing hook or a minimal addition to its source.
- **Assumption:** InteractiveAI's web-app mode starts headlessly (no local display required) and serves the GUI on a configurable HTTP port.
- **Assumption:** The `ai-effect-services` Docker network is available and the WP3 service container can communicate with the InteractiveAI container on it.
- **Assumption:** The Docker socket is accessible inside the WP3 service container (`/var/run/docker.sock` mounted).
- **Constraint:** The InteractiveAI GUI and hmisurveys survey must be served from the same container to avoid a second browser tab.
- **Constraint:** Windows development may cause InteractiveAI startup failures — develop and test on Linux or WSL2.
- **Constraint:** KPI names and survey field names are determined by InteractiveAI and hmisurveys; the service must not hardcode them beyond what those frameworks define.

---

## 7. Dependencies

| Dependency | Owner | Status | Risk if unavailable |
|------------|-------|--------|---------------------|
| InteractiveAI (AI4REALNET/InteractiveAI) | AI4REALNET | External repo | Blocks GUI launch and KPI collection |
| hmisurveys (AI4REALNET/hmisurveys) | AI4REALNET | External repo | Blocks survey step |
| Docker Python SDK (`docker` PyPI package) | PyPI | Available | Blocks container lifecycle management |
| `ai-effect-services` Docker network | WP3 infra | Existing | Blocks inter-container communication |
| Shared Docker volume (per session) | WP3 service | Created at runtime | Blocks results handoff |
| `MetricValue` proto type | This repo (`benchmarking.proto`) | Existing | Reused in result messages |

---

## 8. Open Questions

| # | Question | Status | Owner | Due |
|---|----------|--------|-------|-----|
| 1 | **InteractiveAI ↔ hmisurveys integration**: InteractiveAI and hmisurveys are not yet connected. Two pieces of work are needed inside the InteractiveAI layer (outside WP3): (a) trigger the hmisurveys survey UI in the same browser tab after the grid episode ends; (b) on survey submit, collect grid KPIs from InteractiveAI and survey outcomes from hmisurveys and write `session_result.json` to the mounted `/results` path. **This blocks FR-07 and FR-08 entirely.** | ❌ Open — significant dev work required | Paul / AI4REALNET | Before end-to-end testing |
| 2 | Which specific survey from hmisurveys is used? Output field names / scoring schema depend on survey selection. | ❌ Open — depends on survey chosen | Paul / AI4REALNET | When survey is selected |
| 3 | What are the exact KPI names exposed by InteractiveAI for the grid2op use case? | ❌ Open — needs investigation in InteractiveAI repo | Paul / AI4REALNET | Before end-to-end testing |
| 4 | InteractiveAI Docker image: built locally from the AI4REALNET/InteractiveAI repo. Image name/tag to be confirmed and set as `HAI_INTERACTIVE_AI_IMAGE`. | ✅ Resolved — local build | Paul | Set env var before running |
| 5 | Which HTTP port does InteractiveAI's web-app mode listen on? | ❌ Open — must be discovered by running InteractiveAI | Paul | Before first local test |
| 6 | Is the Docker socket accessible inside the WP3 service container in the deployment environment? | ❌ Open — needs verification | Paul | Before deployment |

---

## 9. Verification

### 9.1 Acceptance Criteria

**FR-01 — Start session:**
> **Given** a valid `HumanAISessionSpec`, **when** `StartHumanAISession` is called, **then** a `session_id` and a reachable `gui_url` are returned within 30 seconds, and the InteractiveAI GUI is accessible in a browser at that URL.

**FR-02 — Status polling:**
> **Given** a started session, **when** `GetSessionStatus` is called repeatedly, **then** the phase progresses monotonically through PENDING → GUI_READY → IN_PROGRESS → SURVEY → COMPLETED and never goes backwards.

**FR-03 — Results retrieval:**
> **Given** a COMPLETED session, **when** `GetSessionResult` is called, **then** the response contains non-empty `kpis` and `survey_outcomes` maps with string keys and `MetricValue` entries.
> **Given** a session in any non-COMPLETED phase, **when** `GetSessionResult` is called, **then** a gRPC error (NOT_FOUND or FAILED_PRECONDITION) is returned.

**FR-07 — Survey auto-display:**
> **Given** an operator who has finished the grid episode, **when** the episode ends, **then** the hmisurveys survey is shown automatically in the same browser tab without any additional navigation required from the operator.

**FR-10 — Timeout:**
> **Given** a session with a 5-minute timeout, **when** 5 minutes elapse without the operator submitting the survey, **then** `GetSessionStatus` returns FAILED with a non-empty error message.

### 9.2 Test Scenarios

| Scenario | Input / State | Expected Result | Covers |
|----------|--------------|-----------------|--------|
| Happy path | Valid spec, operator completes grid + survey | COMPLETED; KPIs + survey outcomes in result | FR-01–FR-03, FR-07–FR-09, FR-11 |
| Timeout before survey submit | Operator never submits | FAILED after configured timeout | FR-10, FR-14 |
| Get result before COMPLETED | Session in IN_PROGRESS | gRPC error returned | FR-03 |
| Invalid / incomplete spec | Missing required fields | gRPC error on StartHumanAISession | FR-04 |
| Container cleanup | Session reaches COMPLETED or FAILED | InteractiveAI container stopped and removed | FR-13 |
| Status message on failure | Timed-out session | FAILED + non-empty `error_message` | FR-14 |

### 9.3 Definition of Done

- [ ] All Must requirements implemented
- [ ] Acceptance criteria passing on Linux
- [ ] Proto file committed: `HumanAISessionSpec`, `StartSessionResponse`, `SessionStatusResponse`, `SessionResultResponse`
- [ ] `docker-compose-all.yml`, `dockerinfo.json`, `blueprint.json` added for the new service
- [ ] Session lifecycle (all phase transitions) verified end-to-end manually
- [ ] Timeout behavior verified
- [ ] Container cleanup verified (no dangling containers after COMPLETED or FAILED)
- [ ] Health check endpoint at `/health` implemented
- [ ] Windows compatibility limitations documented in `README.md`
- [ ] Open Questions 1–3 resolved and reflected in proto / implementation

---

## 10. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **InteractiveAI ↔ hmisurveys integration requires substantial dev work** (OQ-1) | **Confirmed** | **High** | Treat as a separate sub-task: (a) connect hmisurveys survey display to InteractiveAI episode-end event; (b) implement results JSON export to `/results`. Coordinate with AI4REALNET or implement in the WP3 fork of InteractiveAI. |
| InteractiveAI Windows incompatibility blocks local development | High | Medium | Develop and test in WSL2 or Linux VM; document in README |
| Docker socket not accessible inside service container (OQ-6) | Unknown | High | Verify in deployment environment before running; document as setup requirement |
| InteractiveAI web-app port unknown (OQ-5) | Confirmed unknown | Medium | Discover by running InteractiveAI locally; then set `HAI_INTERACTIVE_AI_PORT` accordingly |
| KPI or survey field names unknown until InteractiveAI runs (OQ-3) | Confirmed unknown | Low | Service dynamically parses JSON keys; no hardcoded names. Investigate InteractiveAI source for KPI emission. |

---

## 11. Revision History

| Version | Date | Author | Summary of changes |
|---------|------|--------|-------------------|
| 0.1 | 2026-06-29 | Paul Bannmüller | Initial draft |

---

## 12. Implementation Handoff

> This section is the single source of truth for the implementation agent.
> Every decision needed to start coding is captured here.

**Language & runtime:** Python 3.11
**Execution model:** Synchronous gRPC handlers + per-session background polling thread (same `TaskManager` pattern as `benchmarking/common/task_manager.py`)
**Entry point:** `use-cases/dutch_node/human_ai_interaction_testing/main.py`
**Code location:** New service at `use-cases/dutch_node/human_ai_interaction_testing/`, mirroring `use-cases/dutch_node/benchmarking/` directory structure exactly
**Existing interfaces to respect:**
- `TaskManager` from `use-cases/dutch_node/benchmarking/common/task_manager.py` — copy or import; adapt for session phase enum
- `MetricValue` from `use-cases/dutch_node/benchmarking/proto/benchmarking.proto` — replicate in new proto file
- `dockerinfo.json` and `blueprint.json` schema from `use-cases/dutch_node/benchmarking/`
- HTTP `/health` endpoint (see benchmarking service)

**Library constraints:**
- `docker` (Docker Python SDK) — new dependency; add to `requirements.txt` and `pyproject.toml`
- `grpcio`, `grpcio-tools`, `protobuf` — same versions as benchmarking service
- No additional libraries without discussion

**Test framework:** pytest, same as benchmarking service

**Style notes:** Research-code style — modular, descriptive variable names, full docstrings; follow benchmarking service conventions exactly

**Requirement priority order for implementation:**
1. FR-04 — Define `.proto`: `HumanAISessionSpec`, `StartSessionResponse`, `SessionStatusResponse`, `SessionResultResponse`, phase enum
2. FR-01 — `StartHumanAISession`: launch InteractiveAI container via Docker Python SDK; mount shared volume; return `session_id` + `gui_url`
3. FR-02 — `GetSessionStatus`: session state tracking via `TaskManager`-style class with phase enum
4. FR-08 + FR-09 — Background polling thread watches shared volume for results JSON file; parses and stores on detect
5. FR-10 — Timeout watchdog in background thread; transitions to FAILED if deadline exceeded
6. FR-03 — `GetSessionResult`: return stored KPIs + survey outcomes; error if not COMPLETED
7. FR-13 — Container cleanup on COMPLETED or FAILED (Docker SDK `container.stop()` + `container.remove()`)
8. FR-12 — `docker-compose-all.yml`, `dockerinfo.json`, `blueprint.json`
9. FR-15 — Verify InteractiveAI is launched in web-app mode; document launch flags in Dockerfile / compose

**Known gotchas / non-obvious decisions:**
- The Docker socket must be mounted into the WP3 service container: `- /var/run/docker.sock:/var/run/docker.sock`
- InteractiveAI container must be attached to the `ai-effect-services` Docker network so the WP3 service can reach it by container name
- The shared volume name must be unique per session — use `f"hai-results-{session_id}"` — to allow future concurrent sessions without collision
- `gui_url` must be externally reachable from the operator's browser; use `host.docker.internal` or the host machine's IP, not the container-internal address
- Always launch InteractiveAI in web-app / browser-served simulator mode (FR-15) — other modes are less stable in the current implementation
- On Windows, InteractiveAI may fail to start entirely; document this in README and expect it during development
- Session state is in-memory only (no DB); a service restart loses all active sessions — document as known v1 limitation, do not silently recover
- Do not hardcode KPI or survey field names; parse the results JSON keys as-is into `map<string, MetricValue>`

**What the implementation must NOT do:**
- Do not implement concurrent multi-session support (one active session per service instance in v1)
- Do not modify InteractiveAI source beyond the minimal results-export hook (OQ-1)
- Do not hardcode KPI or survey field names — use JSON keys directly
- Do not implement agent upload via URI/payload or data synthesizer scenario integration (future milestones)
- Do not add authentication to the `gui_url`
- Do not use `docker compose` subprocess calls — use the Docker Python SDK directly
