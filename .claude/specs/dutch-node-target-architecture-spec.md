# Dutch Node Target Architecture Spec

**Status:** Draft
**Author:** Paul Bannmüller
**Date:** 2026-08-04
**Domain:** Architecture — WP3 TEF Node Services & Deployment
**Tier:** A (full spec)

---

## 1. Overview

The three Dutch node services run today as a Docker Compose stack that publishes roughly twenty host ports, mounts the Docker socket into the same process that terminates untrusted browser traffic, and is capped at one participant by fixed port bindings. Session traces are dropped silently because the collect URL is a Docker DNS name without the endpoint path and the receiving service has no CORS middleware. Results are returned as a `DataReference` with `protocol: "grpc"` pointing at each service's own port — correct for a pipeline node, meaningless for a standalone package that has no downstream consumer and no caller able to open that channel.

Underneath all of it sits a quieter problem: the PowerGrid simulator reads no environment variables at all, so the seven per-session variables `_launch_session_containers` assembles never arrive. Every session runs the scenario baked into `config/CONFIG.toml`, against CAB server addresses hardcoded to `192.168.210.100:3200` and similar. The simulator also writes configuration *back* into those TOML files at runtime, putting mutable state inside an image that is meant to be disposable.

This spec defines the target architecture: a stateless control service with no privileged access, a pool of pre-declared session slots configured over HTTP, a proxy that gives every participant-facing surface a single origin and a signed link, event-driven result collection, and two batch-job services whose outputs are actually retrievable. It also defines the enabling changes to the InteractiveAI fork that make the pool possible — delivered as a branch on `AI-EFFECT/InteractiveAI`, not as an upstream pull request.

---

## 2. Goals

1. No component in the WP3 stack holds root-equivalent access to the host — no `/var/run/docker.sock` mount in the primary design.
2. Two to five participants can run sessions concurrently, limited by CPU and memory rather than by port assignments.
3. Exactly one port is reachable from outside the host, permanently: growing the study never requires another firewall change.
4. Every participant-facing surface is served from one origin over HTTPS, behind a signed link that expires with the session.
5. The result of any workflow submitted through the platform is retrievable by the caller that submitted it.
6. A session's outcome is determined by events, not by a thread polling a host directory for files to appear.
7. Per-session configuration reaches the simulator, and the simulator holds no mutable configuration inside its image.

## 3. Non-Goals

- Changes to the orchestrator (`orchestrator/`). Its API, worker model and Redis usage stay as they are.
- Changes to the `hmisurveys` submodule. It is static HTML behind a WP3-owned wrapper and needs nothing; its pin stays at `fa52f51`.
- Hardening the InteractiveAI CAB platform's development defaults (Postgres `trust` auth, Keycloak `admin/admin`, pgAdmin, permissive CORS). That is deployment configuration, tracked separately; this spec names it as a dependency only.
- Resolving whether the AI-EFFECT platform orchestrates our services remotely (Mode B) or hands packages back for local orchestration (Mode A). Carried forward as an open question.
- An upstream pull request to `ainetus/InteractiveAI`. Work lands on the `AI-EFFECT` fork branch; upstream contribution is a separate, later decision.
- Serving multiple concurrent sessions from a single simulator process. The app's simulation engine is a module-level singleton by construction; making it per-session is a rewrite of its core and is explicitly rejected.
- Migration to Kubernetes.
- Publishing images to `ghcr.io/ai-effect` (carried forward from the onboarding-restructure spec).
- Changing any `.proto` file or the operation signatures they declare.

---

## 4. Users & Stakeholders

| Role | Description | Relationship to this spec |
|------|-------------|--------------------------|
| Study participant | Grid operator taking part in a human-AI interaction session; reaches the simulator and survey from a browser, possibly off campus | Primary user — everything in §5.1 "Proxy & access" exists for them |
| WP3 researcher | Starts sessions, retrieves KPIs and survey outcomes, runs benchmark and synthesis jobs | Primary user |
| Faculty ICT | Approves what the server exposes and operates the network path | Approver — goals 1 and 3 are written for this audience |
| Data protection officer | Reviews handling of participant traces, survey answers and cognitive data | Informed — NFR-06 and FR-15 are the surface they will review |
| AI-EFFECT platform | Submits workflows against the exported packages | Consumer of the control interface; constrains FR-31 |
| Paul Bannmüller | Owns the node and the fork branch | Decision-maker on requirement conflicts |

---

## 5. Requirements

### 5.1 Functional Requirements

**Session control plane**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-01 | `hai-control` implements the AI-Effect control interface (`/control/execute`, `/control/status/{id}`, `/control/output/{id}`, `/health`) and the `/collect` router, and mounts no Docker socket and no host bind volumes | Must | Replaces the current monolithic `hai-testing-service` process |
| FR-02 | Session state lives in Redis keyed by session id; `hai-control` holds no session state in process memory | Must | A restart mid-session must not orphan a running session |
| FR-03 | `/control/*` requires a `SERVICE_API_KEY` bearer token | Must | Closes the gap where `services_api_key` is passed by the orchestrator and ignored by the service |
| FR-04 | `POST /collect/session-trace` is authenticated by the session's signed token, not by `SERVICE_API_KEY` | Must | The caller is the participant's browser and cannot hold a service key. Token arrives as `wp3_session_id` plus signature; an invalid or expired token is rejected with 401 |
| FR-05 | `/control/output/{id}` returns a `DataReference` with `protocol: "http"` pointing at a URL the caller can fetch | Must | Replaces the self-referential `grpc://…:50051` reference that nothing resolves |
| FR-06 | A session that receives no results before `session_timeout_seconds` is marked failed and its slot released | Must | Slot leakage is the pool's main failure mode |

**Session runtime — pool (primary design)**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-07 | `N` session slots, each one simulator container plus one survey-wrapper container, are declared in `docker-compose.yml` and started with the stack. `N` is configurable; default 3, supported range 2–5 | Must | Sized in §6; capacity ceiling is an open question |
| FR-08 | `hai-control` reserves a free slot atomically in Redis, configures it over HTTP, and releases it when the session ends, fails or times out | Must | Atomic reservation — two concurrent `StartHumanAISession` calls must never receive the same slot |
| FR-09 | No component in the WP3 stack mounts `/var/run/docker.sock` | Must | The primary goal of the redesign |
| FR-10 | `StartHumanAISession` returns `RESOURCE_EXHAUSTED` when every slot is occupied, naming the number of busy slots | Must | Replaces today's blanket "only one session at a time" |
| FR-11 | Slot configuration is idempotent: reconfiguring an already-reserved slot resets it to a clean state | Must | Depends on OQ-1 |

**Session runtime — fallback (only if OQ-1 fails)**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-12 | If simulator state cannot be reset in-process, a separate `hai-sessions` component becomes the only component with Docker access, exposing exactly `POST /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}` on the internal network | Conditional | Not reachable from the orchestrator or any external path |
| FR-13 | `hai-sessions` reaches Docker through a socket proxy restricted to container create, start, stop, remove, and refuses any image outside a two-entry allowlist, any bind mount, any `privileged`, and any host networking | Conditional | Enforced in `hai-sessions` itself, not only in the proxy |

**Proxy & access**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-14 | An internal reverse proxy routes `/s/{session_id}/gui` and `/s/{session_id}/survey` to the containers of the slot reserved for that session | Must | Traefik with the Docker provider, or a generated nginx config; see §12 |
| FR-15 | Session links carry an HMAC signature with an expiry; the proxy rejects a missing, malformed, expired or wrongly-signed token before the request reaches any session container | Must | Secret from env, never committed |
| FR-16 | The signed token must not persist in proxy access logs | Must | Exchange `?t=` for a session cookie on first request and redirect to the clean URL; strip query strings from access logs for `/s/*` |
| FR-17 | The CAB frontend, the simulator, the survey and the collect endpoint are served from a single origin | Must | Removes the CORS failure by construction |
| FR-18 | `VITE_WP3_COLLECT_URL` is a relative path (`/wp3/collect/session-trace`) | Must | Makes the frontend image environment-independent; ends per-deployment rebuilds of the submodule frontend |
| FR-19 | No container in the WP3 stack publishes a host port. The only host-published port belongs to the proxy | Must | Includes gRPC ports 50053–50055 |

**Results**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-20 | `hai-survey-wrapper` POSTs survey outcomes to `hai-control` instead of writing `survey_outcomes.json` to a shared directory | Must | WP3-owned code; no submodule change involved |
| FR-21 | Session completion is triggered by receipt of both the session trace and the survey outcome, not by polling | Must | The polling thread and `_run_session_polling_thread` are deleted |
| FR-22 | The shared host bind mount and `HAI_RESULTS_HOST_PATH` / `HAI_RESULTS_CONTAINER_PATH` are removed entirely | Must | Ends host-path-to-container-path coupling |
| FR-23 | Results are persisted to a named Docker volume with a configurable retention period, not to `/tmp` | Must | `/tmp/hai_sessions` is subject to tmp cleanup and lost on reboot |

**Batch services — synthesizer and benchmarking**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-24 | Both services run in concurrent (asynchronous) control mode: `/control/execute` returns a task id immediately and `/control/status/{id}` reports progress | Must | A grid2op benchmark must not hold an HTTP request open |
| FR-25 | Both serve their artifacts at `/control/data/{id}` and return that URL from `/control/output/{id}` | Must | The pattern the templates already ship |
| FR-26 | Both declare CPU and memory limits and cap concurrent jobs | Must | A benchmark run will otherwise take the whole machine and starve live sessions |
| FR-27 | Neither publishes a host port nor exposes a gRPC port | Must | Covered by FR-19; restated because the compose files currently do both |

**Shared control interface**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-28 | All three services use the shared control interface in `use-cases/common/` instead of the node's forked `common/control_interface.py` | Must | The fork dropped the `SERVICE_API_KEY` check; one implementation keeps auth, health and DataReference shape uniform |
| FR-29 | Behaviour specific to the node (the `/collect` router, session phase mapping) is layered on top of the shared interface, not by forking it | Must | Extension point, not a copy |

**InteractiveAI fork — Tier 1 (required regardless of the pool)**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-30 | The simulator reads every `CONFIG.toml` value from an environment override when one is present, falling back to the file otherwise | Must | Today it reads no environment variables at all — the seven passed at launch are ignored |
| FR-31 | The CAB platform URL comes from an environment variable, overriding the hardcoded addresses in `API_POWERGRID_CAB.toml` | Must | Currently `192.168.210.100:3200` and similar; `HAI_CAB_URL` / FR-19 of the previous spec is unsatisfied on the simulator side |
| FR-32 | When configuration arrives from the environment or the control API, the simulator does not write it back to the TOML files | Must | `load_and_edit_config` and `edit_parameters` currently `toml.dump` into the image filesystem |
| FR-33 | `ProxyFix` is extended with `x_prefix=1` so `url_for` honours `X-Forwarded-Prefix` | Must | One line; needed for path-based routing. Not needed if per-session subdomains are used instead |

**InteractiveAI fork — Tier 2 (enables the pool)**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-34 | The simulator exposes a JSON control API: `POST /hai/session` (configure and initialise for a session), `POST /hai/reset` (return to idle), `GET /hai/state` (idle / configured / running / finished) | Must | Wraps the existing `simu.load_and_edit_config()` and `simu.initialize_simulation()` calls that `/edit_config` already performs |
| FR-35 | The change is additive: existing routes, templates and behaviour are unchanged, and the app still starts and runs with no WP3 environment variables set | Must | Keeps the fork mergeable against upstream |
| FR-36 | Both tiers land on branch `feat/wp3-simulator-config-api` on `AI-EFFECT/InteractiveAI` (`origin`), branched from `2a41af9` so the collect hook is retained, and the submodule is re-pinned to the new tip by SHA with no `branch` key added to `.gitmodules` | Must | Matches the `feat/wp3-collect-hook` precedent. No pull request to `ainetus/InteractiveAI` |

**Compatibility**

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-37 | The compose service name and port the orchestrator addresses stay `hai-testing-service:8080`, whatever the internal module is called | Must | `export/dockerinfo.json` and the uploaded portal package reference this name; renaming it silently breaks the package |
| FR-38 | `human_ai_interaction_testing.proto` and the `StartHumanAISession` signature are unchanged | Must | Changing them invalidates the exported blueprint |
| FR-39 | The `hmisurveys` submodule is untouched and stays pinned at `fa52f51` | Must | Stated so an implementer does not "helpfully" bump it |

### 5.2 Non-Functional Requirements

| ID | Category | Requirement | Target / Threshold |
|----|----------|-------------|--------------------|
| NFR-01 | Security | No WP3 component can create, modify or inspect containers on the host in the primary design | Zero socket mounts; verified by inspecting the running stack |
| NFR-02 | Security | Compromise of the internet-facing component must not yield host root | In the fallback design, blast radius is limited to the two allowlisted images |
| NFR-03 | Security | All participant-facing traffic is HTTPS and single-origin | No cross-origin request in a normal session |
| NFR-04 | Capacity | Five concurrent sessions run without a slot-reservation error | Slot reservation completes in < 500 ms |
| NFR-05 | Availability | Restarting `hai-control` does not lose or corrupt an active session | Session resumes from Redis state |
| NFR-06 | Data protection | Participant results carry a configurable retention period and are never written to a world-readable host path | Retention default 90 days, configurable |
| NFR-07 | Operability | A failed or abandoned session releases its slot without operator intervention | Within `session_timeout_seconds` + 60 s |

---

## 6. Assumptions & Constraints

- **Assumption:** The InteractiveAI CAB platform runs persistently and is healthy before any session starts. Unchanged from the previous spec; WP3 still does not health-check it.
- **Assumption:** `simu.initialize_simulation()` can be called repeatedly in one process with no state leaking between sessions. **This is unverified and blocks the pool design** — see OQ-1.
- **Assumption:** Write access to `AI-EFFECT/InteractiveAI` exists, as evidenced by the `feat/wp3-collect-hook` branch already pushed there.
- **Assumption:** A faculty-side TLS terminator may sit in front of the node's internal proxy. The node's design does not depend on it; the internal proxy is required either way because it performs session routing, not just TLS.
- **Constraint:** The simulator's engine (`com`, `simu`, and the background task started at import) is a module-level singleton. One container serves one session at a time. This is what makes the design a *pool* rather than a shared multi-tenant service.
- **Constraint:** Each pooled slot holds an idle grid2op process. Memory, not ports, is the ceiling on `N`.
- **Constraint:** The fork must stay reasonably mergeable against `ainetus/InteractiveAI`; changes are additive and confined to configuration and new routes.
- **Constraint:** The exported portal packages (`export/blueprint.json`, `export/dockerinfo.json`) must remain valid without regeneration.

---

## 7. Dependencies

| Dependency | Owner | Status | Risk if unavailable |
|------------|-------|--------|---------------------|
| `AI-EFFECT/InteractiveAI` fork write access | Paul | Available (branch already pushed) | Tier 1 and 2 cannot land; whole pool design blocked |
| Redis for session state | This spec | Deploy alongside the node | `hai-control` cannot be stateless; FR-02 and NFR-05 fail |
| Reverse proxy (Traefik or nginx) | This spec | To build | FR-14 to FR-18 all fail; no session routing, no signed links, CORS returns |
| InteractiveAI CAB platform running | AI4REALNET / Dutch node | Available in repo | Sessions cannot proceed |
| CAB credential hardening | Deployment work, separate | Not started | Not blocking for this spec; blocking for exposure |
| Faculty ICT network path | Faculty ICT | Requested | Blocks external participants only; local development unaffected |

---

## 8. Open Questions

| # | Question | Owner | Due |
|---|----------|-------|-----|
| 1 | ~~Can `simu.initialize_simulation()` be called twice in one process without leaking grid2op environment state?~~ **Resolved 2026-08-04 by static analysis, pending a runtime check.** It reassigns every instance attribute (`env`, `obs`, `local_assistant`, `agent_reco`, `listen`) and accumulates nothing, and upstream already re-initialises in place via `/reset_simulation`. One real defect was found and fixed: the previous `env` was never `close()`d, leaking a LightSim backend per initialisation — harmless for a single-use container, not for a pooled one. `Simulator.release_environment()` now closes it. **Still to confirm on a running stack:** that memory is actually reclaimed across repeated sessions. | Paul | Before the first multi-session pilot |
| 2 | Idle memory footprint of one pooled simulator process, and therefore the real ceiling on `N` | Paul | Before raising `N` above 2 |
| 3 | ~~Path-based routing or per-session subdomains?~~ **Resolved 2026-08-04: path-based.** Keeps the ICT request at one hostname and needs no wildcard certificate; FR-33's `x_prefix=1` is a one-line change and is implemented. | Paul | Closed |
| 4 | Does the AI-EFFECT platform orchestrate our services remotely (Mode B) or hand packages back (Mode A)? Affects whether `dockerinfo.json` must carry a public FQDN. Carried forward, unresolved. | Platform team | Before portal upload |
| 5 | Does anyone hold `ghcr.io/ai-effect` package write access? Carried forward from the onboarding-restructure spec. | Paul | Before portal upload |
| 6 | Retention period for participant results — 90 days is a placeholder pending the data protection officer | DPO | Before first real participant |

---

## 9. Verification

### 9.1 Acceptance Criteria

**FR-09 / NFR-01 — no privileged access:**
> Given the full stack is running in the primary design, when `docker inspect` is run over every WP3 container, then no container has `/var/run/docker.sock` in its mounts and none runs privileged.

**FR-05 — retrievable results:**
> Given a benchmark workflow submitted through the orchestrator, when `/control/output/{task_id}` is called, then the response is a `DataReference` with `protocol: "http"` whose `uri` returns the artifact on a plain GET from the orchestrator's network position.

**FR-07 / FR-08 / NFR-04 — concurrent sessions:**
> Given `N=5` slots and five `StartHumanAISession` calls issued within one second, when all five return, then each has a distinct `session_id`, each `gui_url` resolves to a different slot, and no slot is assigned twice.

**FR-10 — exhaustion:**
> Given all `N` slots are reserved, when a further `StartHumanAISession` is called, then it returns `RESOURCE_EXHAUSTED` with a message naming the number of busy slots, and no slot state is mutated.

**FR-15 / FR-16 — signed links:**
> Given a valid session URL, when the signature is altered by one character, then the proxy returns 403 and no request reaches any session container. And: given a completed request with a valid token, when the proxy access log is inspected, then the token does not appear in it.

**FR-17 / FR-18 — single origin:**
> Given a participant completes a session in a browser, when the network log is inspected, then every request to the simulator, survey, CAB frontend and collect endpoint shares one origin, and no CORS preflight occurs.

**FR-20 / FR-21 — event-driven completion:**
> Given a session where both the trace and the survey outcome are POSTed, when the second POST is received, then the session transitions to complete within one second without any filesystem polling, and no bind mount exists on any container.

**FR-06 / NFR-07 — timeout releases the slot:**
> Given a session that receives no results, when `session_timeout_seconds` elapses, then the session is marked failed, the slot returns to the free pool, and a subsequent `StartHumanAISession` is assigned that slot.

**FR-28 / FR-03 — auth is actually enforced:**
> Given `SERVICE_API_KEY` is set, when `/control/execute` is called without a bearer token, then it returns 401 — for all three services.

**FR-30 / FR-31 — configuration arrives:**
> Given the simulator container is started with a scenario and CAB URL in its environment, when the dashboard is loaded, then the running configuration reflects those values and not the contents of `CONFIG.toml`.

**FR-32 — no config write-back:**
> Given a session is configured through the control API, when the container filesystem is diffed afterwards, then `config/CONFIG.toml` and `config/API_POWERGRID_CAB.toml` are unmodified.

**FR-34 / FR-35 — additive control API:**
> Given the simulator image built from the branch, when it is started with no WP3 environment variables and no control API call, then it behaves exactly as the current image does.

**FR-36 — branch and pin:**
> Given the branch is pushed, when `git log feat/wp3-simulator-config-api` is inspected, then `2a41af9` is an ancestor; and `git submodule status` reports the new tip SHA with no `branch` key present in `.gitmodules`.

**FR-37 — package still valid:**
> Given the redesigned stack, when the committed `export/dockerinfo.json` is used unmodified, then the orchestrator reaches the control service at `hai-testing-service:8080`.

### 9.2 Test Scenarios

| Scenario | Input / State | Expected Result | Covers |
|----------|---------------|-----------------|--------|
| Slot reuse probe | One slot, two sequential sessions with different scenarios | Second session runs the second scenario with no state from the first | OQ-1, FR-11 |
| Concurrent reservation | 5 simultaneous start calls, `N=5` | 5 distinct slots, no double assignment | FR-08, NFR-04 |
| Pool exhausted | 6th call with `N=5` | `RESOURCE_EXHAUSTED`, state unchanged | FR-10 |
| Control restart mid-session | Restart `hai-control` while a session is active | Session survives; results still accepted | FR-02, NFR-05 |
| Tampered link | Valid URL with altered signature | 403 at the proxy | FR-15 |
| Expired link | Token past expiry | 403 at the proxy | FR-15 |
| Unauthenticated control call | No bearer token, key configured | 401 from all three services | FR-03, FR-28 |
| Collect without token | POST to `/collect/session-trace` with no session token | 401 | FR-04 |
| Abandoned session | No results before timeout | Session failed, slot released | FR-06, NFR-07 |
| Batch job lifecycle | Benchmark submitted | Task id immediately, progress via status, artifact URL fetchable | FR-24, FR-25 |
| Resource cap | Benchmark running during a live session | Session remains responsive; benchmark stays within its limits | FR-26 |
| Port surface | Full stack up | Only the proxy port is host-published | FR-19 |
| Simulator without WP3 env | Branch image, no env, no API call | Behaves as today | FR-35 |
| Regression — existing suites | HAI, benchmarking, synthesizer test suites | Pass, adjusted for removed polling and volume code | FR-01, FR-21 |

### 9.3 Definition of Done

- [ ] All Must requirements implemented
- [ ] All acceptance criteria in 9.1 passing
- [ ] OQ-1 answered in writing, with the chosen branch (pool or fallback) recorded in the revision history
- [ ] No container in the stack mounts the Docker socket, or — in the fallback — exactly one does, with an enforced allowlist
- [ ] `docker compose ps` shows exactly one host-published port
- [ ] A full session runs end to end in a browser from off-host, producing both a trace and a survey outcome
- [ ] Five concurrent sessions verified
- [ ] Branch `feat/wp3-simulator-config-api` pushed to `AI-EFFECT/InteractiveAI`; submodule re-pinned; `hmisurveys` pin unchanged
- [ ] Existing test suites pass
- [ ] `README.md` and `human_ai_interaction_testing_docs.md` updated to describe the pool, the proxy and the removed volume
- [ ] Deployment overview figure updated to match the built architecture

---

## 10. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `initialize_simulation` leaks state between sessions, invalidating the pool | Medium | High | Answer OQ-1 with a two-session probe *before* building the pool. Fallback design (FR-12/13) is fully specified, so the loss is the pool's security benefit, not the schedule |
| Idle pooled processes exhaust memory | Medium | Medium | Measure per-slot footprint (OQ-2), make `N` configurable, start at 3 |
| Slot leak from crashed or abandoned sessions | Medium | High | FR-06 timeout plus a reaper on `hai-control` startup that reconciles Redis against reality |
| Fork diverges far enough from `ainetus/InteractiveAI` to make future rebases painful | Medium | Medium | Keep changes additive (FR-35), confined to config handling and new routes, on a single named branch |
| Frontend must be rebuilt for the relative collect URL | High | Low | One-time cost; after FR-18 the image is environment-independent and never needs rebuilding per deployment again |
| Session token leaks through logs or referrer headers | Medium | High | FR-16 cookie exchange and log stripping; short expiry |
| Redesign drifts from the exported portal packages | Medium | Medium | FR-37 and FR-38 freeze the externally visible names and the proto |
| Effort underestimated because the simulator work looks small | Medium | Medium | Tier 1 is required regardless and is on the critical path; sequence it first (see §12) |

---

## 11. Revision History

| Version | Date | Author | Summary of changes |
|---------|------|--------|--------------------|
| 0.1 | 2026-08-04 | Paul Bannmüller | Initial draft. Pool primary with socket fallback; 2–5 concurrent sessions; service-served artifact URLs; signed one-time links; InteractiveAI changes delivered as a fork branch rather than an upstream PR |
| 0.2 | 2026-08-04 | Paul Bannmüller | Implementation pass. OQ-1 resolved by static analysis, so the **pool is taken and FR-12/FR-13 are not implemented**. OQ-3 resolved as path-based routing. `N=2` adopted as the deployment default. Corrected the background-loop gotcha. Four decisions recorded below that the draft did not anticipate. |

### Decisions taken during implementation

| Decision | Requirement | Rationale |
|----------|-------------|-----------|
| The node's own package renamed `common/` → `hai/` | FR-28 | The node's package shadowed the shared one, which is *why* it carried a forked control interface. Both cannot be called `common`; the service now imports `common.concurrent` like every other service. |
| Proxy is nginx with `auth_request`, not Traefik with the Docker provider | FR-14 | Traefik's Docker provider needs the socket, which contradicts goal 1. Slots are statically declared, so only the session→slot mapping is dynamic: nginx resolves it per request from `/internal/authorize-session` and needs no daemon access at all. |
| The gRPC server is removed rather than kept unpublished | FR-19, FR-38 | Nothing called it. The results data plane is now the HTTP artifact endpoint. The proto is unchanged and remains the portal's interface description, which is all FR-38 requires. |
| Transport fields are stripped from posted results before storage | FR-16 | A posted result carries its own session token. Storing the body whole would have written a working credential into the participant data the artifact endpoint serves. |

---

## 12. Implementation Handoff

> Single source of truth for the implementation agent.

**Language & runtime:** Python 3.11 (services, per `Dockerfile` `python:3.11-slim` and `requires-python = ">=3.11"`); Python 3.9 for the InteractiveAI simulator (`Dockerfile.app` `python:3.9-slim-bullseye`) — do not use 3.10+ syntax in submodule code.

**Execution model:** Async for `hai-control` (FastAPI, already async on the collect route); the batch services use the concurrent control-interface pattern — `/control/execute` returns immediately, work runs in a background worker, progress is reported through `/control/status`. No polling threads anywhere.

**Entry points:**
- `use-cases/dutch-node-hai-testing/services/human_ai_interaction_testing/main.py`
- `use-cases/dutch-node-benchmarking/services/benchmarking/main.py`
- `use-cases/dutch-node-data-synthesizer/services/data_synthesizer/main.py`
- `InteractiveAI/usecases_examples/PowerGrid/PowerGrid_poc_simulator_app.py`

**Code location:**
- `hai-control` replaces the current service in place; keep the directory and compose service name (FR-37)
- Slot pool logic in a new `common/slot_pool.py` alongside the existing `session_manager.py`
- Proxy configuration in a new top-level directory of the use case, next to `docker-compose.yml`
- Simulator changes in `PowerGrid_poc_simulator_app.py`, `app/models/Simulator.py`, `app/models/Communicate.py`, plus a new `config/env_overrides.py`

**Existing interfaces to respect:**
- `use-cases/common/concurrent.py` and `sequential.py` — the shared control interface all three services must adopt (FR-28)
- `create_app(execute_handlers, service_name, collect_session_trace)` in `common/control_interface.py` — keep the signature; layer, don't fork
- `SessionPhase` and the session manager's public methods — the phase model is sound and stays
- `simu.load_and_edit_config(params)` and `simu.initialize_simulation(com, session)` — the JSON API wraps these; do not reimplement them
- `human_ai_interaction_testing.proto` — frozen (FR-38)

**Library constraints:** No new dependencies in the simulator beyond what `requirements-app.txt` already carries — the JSON API uses Flask, which is present. `docker>=7.0.0` is removed from the HAI service's dependencies in the primary design and appears only in `hai-sessions` if the fallback is taken. Redis client for `hai-control`. No message broker, no Kubernetes client.

**Test framework:** pytest. Existing suites at `services/human_ai_interaction_testing/tests/` (3 modules) and `services/benchmarking/tests/`. Tests that assert on the polling thread or the results bind mount are expected to be deleted, not adapted.

**Style notes:** Research-code style, matching the existing services — descriptive names, module docstrings explaining *why*, comments only where intent is non-obvious. Requirement IDs referenced in docstrings, as the current code already does.

**Requirement priority order for implementation:**
1. **OQ-1 probe** — two sequential sessions on one container, different scenarios. Everything downstream depends on the answer; do this before writing pool code.
2. **FR-30 to FR-33** — InteractiveAI Tier 1. Required whichever branch is taken, and on the critical path. Land on the fork branch early.
3. **FR-28, FR-29, FR-03** — adopt the shared control interface and enforce auth across all three services. Small, independent, unblocks nothing but closes the worst gap.
4. **FR-34 to FR-36** — InteractiveAI Tier 2 and the submodule re-pin.
5. **FR-07, FR-08, FR-10, FR-11** — the slot pool. Or FR-12/FR-13 if OQ-1 failed.
6. **FR-14 to FR-19** — proxy, signed links, single origin, port removal.
7. **FR-20 to FR-23** — push-based results; delete the polling thread and the bind mount.
8. **FR-01, FR-02, FR-04 to FR-06** — `hai-control` state model and lifecycle.
9. **FR-24 to FR-27** — batch services.

**Known gotchas / non-obvious decisions:**
- The simulator reads **no** environment variables today. `grep -rn "environ\|getenv"` over `usecases_examples/PowerGrid` returns no code hits. Do not assume any of the seven variables currently passed at container launch has an effect.
- `Simulator.load_and_edit_config` loads from the hardcoded relative path `"config/CONFIG.toml"` and `toml.dump`s back into it; `Communicate.load_config` does the same with `"config/API_POWERGRID_CAB.toml"`. Both write paths must become no-ops when configuration comes from the environment or the API (FR-32).
- `API_POWERGRID_CAB.toml` ships hardcoded LAN addresses (`192.168.210.100:3200` and similar). The CAB URL on the shared network is `http://frontend:80`.
- ~~The background simulation loop is already running before any request arrives.~~ **Wrong; corrected 2026-08-04.** `run_simulator` is a *generator*, so the module-level `socketio.start_background_task(simu.run_simulator, com)` calls it, receives a generator object, and never iterates it — the body never executes. The loop actually runs when `/start_simulation` streams it through `stream_with_context`, so its lifetime is bound to the participant's SSE connection. There is no rogue thread to stop before reconfiguring, which is why the reset protocol is simply close-then-reinitialise.
- Flask's `session` in the simulator is a cookie, per browser. It is not the WP3 session and must not be conflated with it.
- `ProxyFix` is already present with `x_proto=1, x_host=1`. Only `x_prefix=1` is missing, and only for path-based routing.
- The simulator already sets `Access-Control-Allow-Origin: *` with an upstream comment recommending same-origin proxying. After FR-17 the wildcard should be narrowed, not relied upon.
- `traceSessionExport.ts` POSTs to `WP3_COLLECT_URL` **directly** — the variable must contain the full path, not a base URL. This is the current bug; do not reproduce it.
- The frontend swallows collect failures and logs only to the browser console. Add a server-side signal so a silently failing collect is visible in operations, not only in a participant's devtools.
- Branch from `2a41af9` (`feat/wp3-collect-hook`), not from `main`, or the collect hook is lost.
- Pin the submodule by SHA. Do not add a `branch` key to `.gitmodules` — the existing entries have none.
- VS Code's git extension holds handles on submodule directories listed in `git.scanRepositories`, which makes moves fail with "Permission denied" on Windows. Carried forward from the previous spec.

**What the implementation must NOT do:**
- Do not change any `.proto` file, operation signature, or the `hai-testing-service:8080` address the orchestrator uses.
- Do not modify the `hmisurveys` submodule or move its pin.
- Do not open a pull request against `ainetus/InteractiveAI`.
- Do not make the simulator serve multiple concurrent sessions from one process.
- Do not keep a Docker socket mount in the primary design "temporarily" — if OQ-1 fails, take the fallback explicitly and record it in the revision history.
- Do not regenerate the portal export packages as part of this work.
- Do not harden the CAB platform's credentials here; that is separate deployment work.
- Do not introduce a message broker, an object store, or an orchestrator change.
