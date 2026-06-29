# Human-AI Interaction Testing Service Spec

**Status:** Draft
**Author:** Paul Bannmüller
**Date:** 2026-06-29
**Domain:** Software Feature / Architecture

---

## 1. Overview

The Human-AI Interaction Testing Service (`human_ai_interaction_testing`) is a new WP3 gRPC microservice that orchestrates an operator-in-the-loop evaluation session using the InteractiveAI framework and hmisurveys as **two independent containers**. An AI vendor submits a testing configuration; the service launches both an InteractiveAI container (grid simulation) and an hmisurveys container (survey) and returns two browser URLs — `gui_url` for the grid GUI and `survey_url` for the survey. The human operator completes the grid session first, then navigates to the survey URL. Each container writes its results independently to a shared directory; the WP3 service polls for both result files and transitions to COMPLETED only when both are present. This design keeps InteractiveAI and hmisurveys fully decoupled and requires only a minimal export hook in each tool.

---

## 2. Goals

1. Provide a gRPC-accessible service that AI vendors can invoke to run a human-AI interaction test session.
2. Launch and manage **two Docker containers** per session (InteractiveAI + hmisurveys) using the Docker Python SDK.
3. Return both `gui_url` (InteractiveAI grid GUI) and `survey_url` (hmisurveys survey) in `StartSessionResponse`.
4. Expose a session lifecycle via a phase status enum: PENDING → GUI_READY → IN_PROGRESS → SURVEY → COMPLETED | FAILED.
5. Collect results by polling a shared directory for two result files (`kpis.json` from InteractiveAI, `survey_outcomes.json` from hmisurveys) and expose them via `GetSessionResult`.
6. Support a configurable session timeout after which the session transitions to FAILED.
7. Follow the same Python/gRPC service structure as the existing benchmarking service.
8. Integrate into the WP3 orchestrator via `blueprint.json` and `dockerinfo.json`.

---

## 3. Non-Goals

- Custom agent upload via URI or `AlgorithmPayload` (future milestone).
- Scenario generation via the data synthesizer (future milestone; InteractiveAI default scenario used for now).
- Multi-operator parallel sessions for the same session ID.
- Real-time streaming of operator actions or grid state to the caller.
- Authentication or access control for `gui_url` or `survey_url`.
- Integrating InteractiveAI and hmisurveys into a single container — they remain independent.
- Automatic navigation from the grid GUI to the survey (operator navigates manually to `survey_url`).
- Modification of InteractiveAI or hmisurveys source code beyond the minimal per-tool result export hook.
- Session state persistence across WP3 service restarts (known v1 limitation).

---

## 4. Users & Stakeholders

| Role | Description | Relationship to this spec |
|------|-------------|--------------------------|
| AI Vendor | Submits testing config, receives results after session | Primary caller of gRPC API |
| Human Operator | Uses InteractiveAI GUI, then navigates to hmisurveys survey | End-user of both browser URLs |
| WP3 Researcher / Test Coordinator | Runs the service, shares both URLs with operator | Infrastructure operator |
| WP3 Orchestrator | Invokes RPCs as part of a pipeline topology | Automated caller |

---

## 5. Requirements

### 5.1 Functional Requirements

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-01 | `StartHumanAISession(HumanAISessionSpec)` launches **both** the InteractiveAI and hmisurveys Docker containers and returns `session_id`, `gui_url`, and `survey_url` | Must | Both containers managed via Docker Python SDK |
| FR-02 | `GetSessionStatus(SessionStatusRequest)` returns the current phase of a session | Must | Phases: PENDING, GUI_READY, IN_PROGRESS, SURVEY, COMPLETED, FAILED |
| FR-03 | `GetSessionResult(SessionResultRequest)` returns grid KPIs and survey outcomes for a COMPLETED session; returns gRPC error if not yet COMPLETED | Must | |
| FR-04 | `HumanAISessionSpec` includes: scenario config, agent config, survey selection, KPI list, session timeout | Must | All fields required |
| FR-05 | Scenario config defaults to InteractiveAI's built-in default scenario | Must | Extensible to data synthesizer in a future milestone |
| FR-06 | Agent config defaults to the agent built into InteractiveAI | Must | Extensible to URI/payload in a future milestone |
| FR-07 | hmisurveys runs as a **separate Docker container**; its browser URL is returned as `survey_url` in `StartSessionResponse`; the operator navigates there after completing the grid session | Must | Replaces the previous "same-tab auto-display" design; InteractiveAI and hmisurveys remain independent |
| FR-08 | InteractiveAI writes grid KPIs to `kpis.json` in the shared results directory when the grid episode ends | Must | Requires a minimal export hook in InteractiveAI — see OQ-1 |
| FR-09 | hmisurveys writes survey outcomes to `survey_outcomes.json` in the shared results directory when the operator submits the survey | Must | Requires a minimal export hook in hmisurveys — see OQ-2 |
| FR-10 | WP3 service polls the shared results directory every 5 seconds; transitions to COMPLETED only when **both** `kpis.json` and `survey_outcomes.json` are present | Must | |
| FR-11 | Session transitions to FAILED if the configured timeout elapses before COMPLETED | Must | Timeout field in `HumanAISessionSpec` |
| FR-12 | Result structure uses `map<string, MetricValue>` for both KPIs and survey outcomes, mirroring the benchmarking service | Must | Reuse or replicate `MetricValue` from `benchmarking.proto` |
| FR-13 | Service registers in `dockerinfo.json`; a `blueprint.json` pipeline definition is provided | Must | Follow Dutch node conventions |
| FR-14 | **Both** InteractiveAI and hmisurveys containers are stopped and removed when a session reaches COMPLETED or FAILED | Should | Prevent resource leaks |
| FR-15 | `GetSessionStatus` includes an error message string when phase is FAILED | Should | Aids debugging |
| FR-16 | InteractiveAI is launched in web-app / browser-served simulator mode | Must | More stable than alternative launch modes in current implementation |

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

- **Assumption:** InteractiveAI can be minimally modified to write `kpis.json` to a mounted results path when the grid episode ends (OQ-1).
- **Assumption:** hmisurveys can be minimally modified to write `survey_outcomes.json` to a mounted results path when the survey is submitted (OQ-2).
- **Assumption:** InteractiveAI's web-app mode starts headlessly (no local display required) and serves the GUI on a configurable HTTP port.
- **Assumption:** hmisurveys serves its survey on a configurable HTTP port.
- **Assumption:** The `ai-effect-services` Docker network is available and the WP3 service container can communicate with both sub-containers on it.
- **Assumption:** The Docker socket is accessible inside the WP3 service container (`/var/run/docker.sock` mounted).
- **Constraint:** InteractiveAI and hmisurveys remain independent — they do not communicate with each other. The operator manually navigates from `gui_url` to `survey_url`.
- **Constraint:** Windows development may cause InteractiveAI startup failures — develop and test on Linux or WSL2.
- **Constraint:** KPI names and survey field names are determined by each tool; the WP3 service must not hardcode them.

---

## 7. Dependencies

| Dependency | Owner | Status | Risk if unavailable |
|------------|-------|--------|---------------------|
| InteractiveAI (AI4REALNET/InteractiveAI) | AI4REALNET | External repo — build locally | Blocks GUI launch and KPI collection |
| hmisurveys (AI4REALNET/hmisurveys) | AI4REALNET | External repo | Blocks survey step |
| Docker Python SDK (`docker` PyPI package) | PyPI | Available | Blocks container lifecycle management |
| `ai-effect-services` Docker network | WP3 infra | Existing | Blocks inter-container communication |
| Shared results directory (per session, host-mounted) | WP3 service | Created at runtime | Blocks results handoff |
| `MetricValue` proto type | This repo (`benchmarking.proto`) | Existing | Reused in result messages |

---

## 8. Open Questions

| # | Question | Status | Owner | Due |
|---|----------|--------|-------|-----|
| 1 | Does InteractiveAI already write KPIs somewhere on episode end, or does a minimal hook need to be added to write `kpis.json` to a mounted `/results` path? | ❌ Open — check InteractiveAI source | Paul / AI4REALNET | Before end-to-end testing |
| 2 | Does hmisurveys already export survey responses somewhere on submit, or does a minimal hook need to be added to write `survey_outcomes.json` to a mounted `/results` path? | ❌ Open — check hmisurveys source | Paul / AI4REALNET | Before end-to-end testing |
| 3 | What are the exact KPI field names emitted by InteractiveAI for the grid2op use case? | ❌ Open — investigate InteractiveAI source | Paul / AI4REALNET | Before end-to-end testing |
| 4 | What are the survey field names / scoring schema for the chosen hmisurveys survey? | ❌ Open — depends on survey selected | Paul / AI4REALNET | When survey is selected |
| 5 | InteractiveAI Docker image: built locally from AI4REALNET/InteractiveAI. What is the image tag to use? | ✅ Resolved — local build | Paul | Set `HAI_INTERACTIVE_AI_IMAGE` before running |
| 6 | Which HTTP port does InteractiveAI's web-app mode listen on by default? | ❌ Open — discover by running it | Paul | Before first local test |
| 7 | Which HTTP port does hmisurveys serve on by default? | ❌ Open — discover by running it | Paul | Before first local test |
| 8 | Is the Docker socket accessible inside the WP3 service container in the deployment environment? | ❌ Open — needs verification | Paul | Before deployment |

---

## 9. Verification

### 9.1 Acceptance Criteria

**FR-01 — Start session:**
> **Given** a valid `HumanAISessionSpec`, **when** `StartHumanAISession` is called, **then** a `session_id`, a reachable `gui_url`, and a reachable `survey_url` are returned within 30 seconds, and both URLs are accessible in a browser.

**FR-02 — Status polling:**
> **Given** a started session, **when** `GetSessionStatus` is called repeatedly, **then** the phase progresses monotonically through PENDING → GUI_READY → IN_PROGRESS → SURVEY → COMPLETED and never goes backwards.

**FR-03 — Results retrieval:**
> **Given** a COMPLETED session, **when** `GetSessionResult` is called, **then** the response contains non-empty `kpis` and `survey_outcomes` maps with string keys and `MetricValue` entries.
> **Given** a session in any non-COMPLETED phase, **when** `GetSessionResult` is called, **then** a gRPC error (NOT_FOUND or FAILED_PRECONDITION) is returned.

**FR-07 — Separate survey URL:**
> **Given** a started session, **when** `StartHumanAISession` returns, **then** `survey_url` points to a running hmisurveys container accessible in a browser independently of `gui_url`.

**FR-10 — Two-file polling:**
> **Given** a session where only `kpis.json` has appeared, **when** `GetSessionStatus` is called, **then** the session is NOT yet COMPLETED. **Given** both `kpis.json` and `survey_outcomes.json` are present, **then** the session transitions to COMPLETED.

**FR-11 — Timeout:**
> **Given** a session with a 5-minute timeout, **when** 5 minutes elapse without both result files appearing, **then** `GetSessionStatus` returns FAILED with a non-empty error message.

### 9.2 Test Scenarios

| Scenario | Input / State | Expected Result | Covers |
|----------|--------------|-----------------|--------|
| Happy path | Valid spec, operator completes grid + survey | COMPLETED; KPIs + survey outcomes in result | FR-01–FR-03, FR-07–FR-12 |
| Only KPIs file present | `kpis.json` written, no `survey_outcomes.json` | Session remains IN_PROGRESS / SURVEY, not COMPLETED | FR-10 |
| Only survey file present | `survey_outcomes.json` written, no `kpis.json` | Session remains IN_PROGRESS / SURVEY, not COMPLETED | FR-10 |
| Timeout before either file | No result files appear | FAILED after configured timeout | FR-11, FR-15 |
| Get result before COMPLETED | Session in IN_PROGRESS | gRPC error returned | FR-03 |
| Invalid / incomplete spec | `session_timeout_seconds` ≤ 0 | gRPC error on StartHumanAISession | FR-04 |
| Container cleanup | Session reaches COMPLETED or FAILED | Both containers stopped and removed | FR-14 |
| Status message on failure | Timed-out session | FAILED + non-empty `error_message` | FR-15 |

### 9.3 Definition of Done

- [ ] All Must requirements implemented
- [ ] Acceptance criteria passing on Linux
- [ ] Proto file updated: `StartSessionResponse` has `gui_url` + `survey_url`; all other messages unchanged
- [ ] Both containers (InteractiveAI + hmisurveys) launched and cleaned up correctly
- [ ] Polling waits for both `kpis.json` and `survey_outcomes.json`
- [ ] `docker-compose-all.yml` documents all required env vars for both images
- [ ] Session lifecycle (all phase transitions) verified end-to-end manually
- [ ] Timeout behavior verified
- [ ] Container cleanup verified (no dangling containers after COMPLETED or FAILED)
- [ ] Health check endpoint at `/health` implemented
- [ ] Windows compatibility limitations documented in `README.md`
- [ ] OQ-1 and OQ-2 resolved (minimal export hooks added in respective repos)

---

## 10. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| InteractiveAI has no KPI export hook on episode end (OQ-1) | Unknown | High | Check source; add minimal hook to write `kpis.json` to mounted `/results` path on episode end |
| hmisurveys has no results export hook on survey submit (OQ-2) | Unknown | High | Check source; add minimal hook to write `survey_outcomes.json` to mounted `/results` path on submit |
| InteractiveAI Windows incompatibility blocks local development | High | Medium | Develop and test in WSL2 or Linux VM; document in README |
| Docker socket not accessible inside service container (OQ-8) | Unknown | High | Verify in deployment environment; document as setup requirement |
| InteractiveAI web-app port unknown (OQ-6) | Confirmed | Medium | Discover by running InteractiveAI locally; set `HAI_INTERACTIVE_AI_PORT` |
| hmisurveys port unknown (OQ-7) | Confirmed | Medium | Discover by running hmisurveys locally; set `HAI_HMISURVEYS_PORT` |
| KPI / survey field names unknown (OQ-3, OQ-4) | Confirmed | Low | Service parses JSON keys dynamically; no code change needed once hooks exist |

---

## 11. Revision History

| Version | Date | Author | Summary of changes |
|---------|------|--------|-------------------|
| 0.1 | 2026-06-29 | Paul Bannmüller | Initial draft |
| 0.2 | 2026-06-29 | Paul Bannmüller | Two-instances design: InteractiveAI and hmisurveys as separate containers; `survey_url` added to `StartSessionResponse`; polling changed to wait for two separate result files (`kpis.json` + `survey_outcomes.json`) |

---

## 12. Implementation Handoff

> This section is the single source of truth for the implementation agent.
> Every decision needed to start coding is captured here.

**Language & runtime:** Python 3.11
**Execution model:** Synchronous gRPC handlers + per-session background polling thread
**Entry point:** `use-cases/dutch_node/human_ai_interaction_testing/main.py`
**Code location:** `use-cases/dutch_node/human_ai_interaction_testing/` — edit existing files in place

**Existing interfaces to respect:**
- `SessionManager` / `SessionPhase` in `common/session_manager.py` — no changes needed
- `common/proto_runtime.py` — no changes needed
- `common/control_interface.py` — no changes needed
- `common/__init__.py` — no changes needed
- `main.py` — no changes needed

**Files that need changes:**

1. `proto/human_ai_interaction_testing.proto`
   - Add `survey_url` field (field number 5) to `StartSessionResponse`
   - No other proto changes

2. `common/session_operations.py` — most changes are here:
   - Add env vars: `HAI_HMISURVEYS_IMAGE`, `HAI_HMISURVEYS_HOST_PORT` (default `8091`), `HAI_SURVEY_BASE_URL` (default `http://host.docker.internal:8091`)
   - `SessionState` in `session_manager.py` needs a `survey_container_id` field — add it (or store alongside `container_id` as a second field)
   - `_launch_interactive_ai_container()` → rename to `_launch_session_containers()`: launch both InteractiveAI AND hmisurveys containers; return `(ia_container_id, hmisurveys_container_id, gui_url, survey_url)`
   - `_stop_and_remove_container()` → call it twice (for both container IDs) in the finally block
   - `_run_session_polling_thread()`: change polling condition from "one file" to "both `kpis.json` AND `survey_outcomes.json` present"; parse both and merge results
   - `_parse_results_file()` → keep as-is (used twice, once per file)
   - `StartHumanAISession` gRPC handler: pass `survey_url` into `StartSessionResponse`
   - HTTP `_execute_start_session()`: pass `survey_url` back similarly

3. `common/session_manager.py`
   - Add `survey_container_id: str = ""` field to `SessionState` dataclass
   - Add `survey_url: str = ""` field to `SessionState` dataclass
   - Update `advance_phase()` signature to accept `survey_container_id` and `survey_url` optional kwargs

4. `docker-compose-all.yml`
   - Add env vars: `HAI_HMISURVEYS_IMAGE`, `HAI_HMISURVEYS_HOST_PORT`, `HAI_SURVEY_BASE_URL`

5. `README.md`
   - Update env var table and architecture diagram for two-container design
   - Update OQ section

**Library constraints:** unchanged — `docker` SDK already present

**Test framework:** pytest — update existing tests to cover two-file polling and `survey_url` in response

**Requirement priority order for implementation:**
1. Proto: add `survey_url` to `StartSessionResponse`
2. `session_manager.py`: add `survey_container_id` and `survey_url` fields + kwargs
3. `session_operations.py`: launch two containers, return both URLs
4. `session_operations.py`: polling thread waits for both `kpis.json` and `survey_outcomes.json`
5. `session_operations.py`: cleanup both containers in finally block
6. `docker-compose-all.yml` + `README.md`: document new env vars
7. Tests: add two-file polling tests, `survey_url` presence test

**Known gotchas / non-obvious decisions:**
- hmisurveys container gets the **same** results directory mounted as InteractiveAI (`/results`), but writes a different filename (`survey_outcomes.json` vs `kpis.json`) — this is intentional so both are in one place for the polling thread
- Use a different host port for hmisurveys than InteractiveAI to avoid collision: default `8091` vs `8090`
- Both containers must be attached to `ai-effect-services` network
- Cleanup in `_run_session_polling_thread()` finally block must stop BOTH containers; use the `survey_container_id` stored in `SessionState`
- The polling condition is `kpis.json AND survey_outcomes.json` — not OR; session does not complete until both files exist
- `gui_url` and `survey_url` are both stored in `SessionState` for retrieval by `GetSessionStatus` if needed
- `_parse_results_file()` is called twice with different paths; each returns its dict directly — merge at the call site into `kpis` and `survey_outcomes` separately

**What the implementation must NOT do:**
- Do not merge `kpis.json` and `survey_outcomes.json` into a single file — keep them separate
- Do not require InteractiveAI and hmisurveys to communicate with each other
- Do not implement agent upload, data synthesizer scenario, or concurrent sessions
- Do not use `docker compose` subprocess calls — Docker Python SDK only
- Do not hardcode KPI or survey field names
