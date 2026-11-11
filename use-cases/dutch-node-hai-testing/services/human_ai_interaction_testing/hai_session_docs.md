# HAI Session Service — Documentation

*Auto-generated from source docstrings. Do not edit by hand.*

**Spec:** `.claude/specs/hai-orchestrated-session-spec.md`
**Generated:** 2026-11-11

---

## Overview

Human-AI interaction session service for AI-Effect orchestration.

Exposes one operation, ``RunHAISession``. Each function named
``execute_<MethodName>`` is exposed as an operation, so helpers here are kept
private with a leading underscore.

RunHAISession runs one human-AI interaction test on an InteractiveAI stack
that is already up (``scripts/setup_interactiveai.sh``). The request selects,
by name, an algorithm in the ``algorithms/`` folder and a grid2op scenario in
``scenarios/`` — the benchmarking convention — and sets how long the
participant has. The service then:

- serves the algorithm as the recommendation agent InteractiveAI calls;
- writes the simulator's CONFIG.toml for the scenario;
- publishes the participant's links and instructions at
  ``/control/intermediate/{job_id}``;
- at the deadline, collects InteractiveAI's traces of the session into one JSON
  result, served at ``/control/data/{job_id}``.

InteractiveAI itself is used unmodified. Where that limits what a session can
collect, the README and spec section 6.3 say which InteractiveAI change would
lift it.

Spec: .claude/specs/hai-orchestrated-session-spec.md
Spec coverage: FR-01, FR-03, FR-04, FR-05, FR-07

---

## Modules

### `service.py`

Human-AI interaction session service for AI-Effect orchestration.

Exposes one operation, ``RunHAISession``. Each function named
``execute_<MethodName>`` is exposed as an operation, so helpers here are kept
private with a leading underscore.

RunHAISession runs one human-AI interaction test on an InteractiveAI stack
that is already up (``scripts/setup_interactiveai.sh``). The request selects,
by name, an algorithm in the ``algorithms/`` folder and a grid2op scenario in
``scenarios/`` — the benchmarking convention — and sets how long the
participant has. The service then:

- serves the algorithm as the recommendation agent InteractiveAI calls;
- writes the simulator's CONFIG.toml for the scenario;
- publishes the participant's links and instructions at
  ``/control/intermediate/{job_id}``;
- at the deadline, collects InteractiveAI's traces of the session into one JSON
  result, served at ``/control/data/{job_id}``.

InteractiveAI itself is used unmodified. Where that limits what a session can
collect, the README and spec section 6.3 say which InteractiveAI change would
lift it.

Spec: .claude/specs/hai-orchestrated-session-spec.md
Spec coverage: FR-01, FR-03, FR-04, FR-05, FR-07

#### `set_settings(settings: HAISettings | None) -> None`

Install settings for this process, or None to read the environment again.

#### `get_settings() -> HAISettings`

Return the process-wide settings, reading the environment on first use.

#### `set_job_runner(runner: BatchJobRunner | None) -> None`

Install a job runner, or None to build a fresh one from the environment.

#### `get_job_runner() -> BatchJobRunner`

Return the process-wide job runner, building it on first use.

#### `get_agent_registry() -> AgentRegistry`

Return the process-wide agent registry, building it on first use.

#### `set_session_coordinator(coordinator: SessionCoordinator | None) -> None`

Install a session coordinator, or None to build the default on next use.

#### `get_session_coordinator() -> SessionCoordinator`

Return the process-wide session coordinator, building it on first use.

#### `execute_RunHAISession(request: ExecuteRequest) -> ExecuteResponse`

Start a human-AI interaction session and return immediately with its job id.

Input (inline base64 JSON; spec section 5.3):
    algorithm: File name in the algorithms folder (default ``algorithm_template.py``).
    scenario:
        env_name: Required. grid2op environment directory in the scenarios folder.
        scenario_name: Required. Chronic folder inside ``<env_name>/chronics``.
        scenario_first_step: Step the operator takes over from (default 0).
        env_seed: Environment seed (default 0).
        step_duration_s: Seconds per simulator step (default 0.5).
    session_duration_seconds: Required. 60 to 2 592 000.
    kpis: Reserved; accepted and recorded as a warning, not computed.

The request is validated on the calling thread, so a bad one is refused now
(FR-03). A second request while a session holds the slot is refused with
that session's deadline (FR-05). Otherwise the session runs as a background
job (FR-04): poll ``/control/status/{job_id}``, read the participant's links
at ``/control/intermediate/{job_id}``, and fetch the result from the URL
``/control/output/{job_id}`` returns once the deadline has passed.

Returns:
    An ExecuteResponse with status ``running`` and the job id, or ``failed``
    with the reason.

---

### `session.py`

Session — one human-AI interaction session, from go-live to collected result.

A session runs on a ``BatchJobRunner`` worker thread and goes through fixed
steps:

1. **preflight** — the simulator, the recommendation gateway and the test
   account must answer (FR-11);
2. **activate** — build the wrapper environment and the algorithm's agent, and
   install it as the agent InteractiveAI calls (FR-12);
3. **configure** — write the simulator's CONFIG.toml for the scenario (FR-13);
4. **go live** — fix the deadline and publish the participant's links and
   instructions as the intermediate result (FR-14);
5. **wait** — until the deadline, reporting progress (FR-15). Nothing else ends
   a session: InteractiveAI sends no "participant finished" signal;
6. **collect** — remove the agent, fetch the traces of the session window and
   assemble the result (FR-16a, FR-20).

Only one session runs at a time, because the simulator, the agent slot in
cabrecommendation and the test account are shared, and traces can only be told
apart by time. ``SessionCoordinator.reserve`` enforces that before a job is
submitted, so a second request is refused with the running session's deadline
(FR-05).

Spec coverage: FR-05, FR-06, FR-08, FR-09, FR-10, FR-14, FR-15, FR-16a,
sections 5.4 and 5.7

#### `format_utc(moment: datetime) -> str`

Format a moment as ISO-8601 UTC with a ``Z`` suffix.

Args:
    moment: A timezone-aware datetime.

Returns:
    E.g. ``2026-11-11T10:00:00Z``.

#### class `SessionClock`

Wall clock and interruptible wait; replaced by a fake clock in tests.

##### `SessionClock.now(self) -> datetime`

Return the current time in UTC.

##### `SessionClock.wait(self, stop_event: threading.Event, seconds: float) -> bool`

Wait up to ``seconds``, returning early if the event is set.

Returns:
    True if the event was set, False if the time elapsed.

#### class `ActiveSession`

The session holding the service's single slot.

Attributes:
    job_id: Job id returned to the orchestrator.
    deadline: When results are collected; None until the session is live.
    stop_event: Set to end the wait early (shutdown).

#### `build_intermediate_result(job_id: str, config: SessionConfig, settings: HAISettings, started_at: datetime, deadline: datetime) -> dict[str, Any]`

Build what the participant needs, handed to the orchestrator mid-session (section 5.4).

TODO(orchestrator): the orchestrator does not yet consume intermediate
results. Until it does, this object is served at
``GET /control/intermediate/{task_id}``, logged at go-live and stored in the
final result's ``session.intermediate``. When the orchestrator gains an
intermediate-result channel, this is the payload to send through it (FR-10).

Args:
    job_id: The session's job id.
    config: The validated session request.
    settings: Public URLs and test account.
    started_at: Go-live time, UTC.
    deadline: Collection time, UTC.

Returns:
    The intermediate object.

#### `build_session_result(job_id: str, config: SessionConfig, settings: HAISettings, session_times: dict[str, datetime], intermediate: dict[str, Any], traces: list[Any], trace_query: dict[str, str], session_warnings: SessionWarnings) -> dict[str, Any]`

Assemble the ``HAISessionResult`` artifact (section 5.7).

Args:
    job_id: The session's job id.
    config: The validated session request.
    settings: For the recorded InteractiveAI commit.
    session_times: ``started_at``, ``deadline`` and ``ended_at``, UTC.
    intermediate: The object published at go-live.
    traces: cabhistoric traces, unchanged.
    trace_query: Query description from ``InteractiveAIClient.build_trace_query``.
    session_warnings: Warnings recorded during the session.

Returns:
    The JSON-serialisable result.

#### `compute_progress(started_at: datetime, now: datetime, session_duration_seconds: int) -> int`

Return the session's progress for ``/control/status`` (FR-06).

Args:
    started_at: Go-live time.
    now: Current time.
    session_duration_seconds: Configured duration.

Returns:
    Elapsed share in percent, between 0 and MAXIMUM_PROGRESS_WHILE_RUNNING.

#### class `SessionCoordinator`

Owns the single session slot and runs sessions through their lifecycle.

Example:
    >>> coordinator = SessionCoordinator(settings, AgentRegistry())
    >>> error = coordinator.reserve(job_id)        # None when the slot was free
    >>> result = coordinator.run_session(job_id, config, report_progress)

##### `SessionCoordinator.reserve(self, job_id: str) -> str | None`

Claim the session slot for a job.

Args:
    job_id: The job about to be submitted.

Returns:
    None if the slot was claimed, otherwise the error for the caller.

##### `SessionCoordinator.release(self, job_id: str) -> None`

Free the slot if this job holds it.

Args:
    job_id: The job that ended or was never started.

##### `SessionCoordinator.forget(self, job_id: str) -> None`

Release the slot and drop the job's intermediate entry; for a job never submitted.

Args:
    job_id: The job the runner refused.

##### `SessionCoordinator.stop_active_session(self) -> None`

End the active session's wait early; its results are still collected.

##### `SessionCoordinator.get_intermediate_result(self, job_id: str) -> tuple[str, dict[str, Any] | None]`

Look up a job's intermediate object.

Args:
    job_id: Job id from ``/control/execute``.

Returns:
    ``("unknown", None)``, ``("pending", None)`` before go-live, or
    ``("ready", intermediate)``.

##### `SessionCoordinator.run_session(self, job_id: str, config: SessionConfig, report_progress: Callable[[int], None]) -> dict[str, Any]`

Run one session to its result; the job body handed to ``BatchJobRunner``.

The agent is removed and the slot freed on every exit path, so a failed
session never leaves the service busy (FR-16a).

Args:
    job_id: The session's job id; must hold the slot (``reserve``).
    config: The validated session request.
    report_progress: Publishes progress to ``/control/status``.

Returns:
    The ``HAISessionResult`` artifact.

Raises:
    PreflightError: If a dependency is down before go-live.
    TraceCollectionError: If the traces cannot be fetched at the deadline.
    Exception: Whatever activating the algorithm or writing the config raises.

#### `create_intermediate_router(coordinator: SessionCoordinator) -> APIRouter`

Build ``GET /control/intermediate/{task_id}``, the orchestrator placeholder (FR-08).

``/control/status`` cannot carry the object: its ``response_model`` in the
shared control plane drops unknown fields. The route uses the same bearer
check as ``/control/*``.

Args:
    coordinator: Supplies the intermediate objects.

Returns:
    The router.

---

### `session_config.py`

Session config — decoding and validating a ``RunHAISession`` request.

A request is one inline base64 JSON input (the benchmarking convention). It
names an algorithm file and a grid2op scenario that the node operator has placed
in the service's directories, and sets the session's timing. It never carries
code or paths: names are checked to be bare, so a caller can choose among what
is on the node but cannot point the service anywhere else.

Validation is complete and does no network I/O (NFR-01), so a bad request is
rejected by ``/control/execute`` itself rather than becoming a job that fails
later.

Spec coverage: FR-02, FR-03, section 5.3, NFR-07

#### class `ScenarioSelection`

The grid2op scenario a session plays, as the request selected it.

Attributes:
    env_name: Directory name of the grid2op environment in the scenarios folder.
    scenario_name: Chronic folder name inside ``<env_name>/chronics``.
    scenario_first_step: Step the operator takes over from.
    env_seed: Seed of the grid2op environment.
    step_duration_s: Seconds between simulator steps outside events.

#### class `SessionConfig`

A validated ``RunHAISession`` request, with defaults filled in.

Attributes:
    algorithm: File name of the algorithm in the algorithms folder.
    scenario: The selected scenario.
    session_duration_seconds: Time from go-live to result collection.
    kpis: Requested KPI names; accepted and reserved, not computed.
    algorithm_path: Resolved path of the algorithm file.
    scenario_directory: Resolved path of the grid2op environment directory.

##### `SessionConfig.to_result_dict(self) -> dict[str, Any]`

Return the request as it is recorded in the result's ``config`` block.

Returns:
    The validated request with defaults filled in, without local paths.

##### `SessionConfig.kpi_warnings(self) -> list[str]`

Return the warning recorded when KPIs were requested (section 5.3).

Returns:
    One warning naming the requested KPIs, or an empty list.

#### `decode_inline_json(input_reference: dict[str, Any]) -> dict[str, Any]`

Decode an inline DataReference carrying base64-encoded JSON.

Args:
    input_reference: One entry of ``ExecuteRequest.inputs``.

Returns:
    The decoded JSON object.

Raises:
    ValueError: If the uri is empty or not base64-encoded JSON.

#### `read_request_payload(inputs: list[dict[str, Any]]) -> dict[str, Any]`

Read the session request from the first input.

Args:
    inputs: ``ExecuteRequest.inputs``.

Returns:
    The decoded payload.

Raises:
    ValueError: If there is no input, it is not inline, or it is not a JSON object.

#### `require_plain_name(name: Any, field_name: str) -> str`

Accept a bare file or directory name, rejecting anything path-like (NFR-07).

Args:
    name: The value from the request.
    field_name: Dotted field name, used in the error message.

Returns:
    The name, stripped of surrounding whitespace.

Raises:
    ValueError: If the name is empty or contains a path component.

#### `resolve_algorithm(name: Any, algorithm_directory: Path) -> Path`

Resolve an algorithm name to its ``.py`` file in the algorithms folder.

Args:
    name: The ``algorithm`` value from the request.
    algorithm_directory: The folder algorithms are selected from.

Returns:
    The algorithm file's path.

Raises:
    ValueError: If the name is not a ``.py`` file present there.

#### `resolve_scenario_directory(env_name: str, scenario_name: str, scenario_directory: Path) -> Path`

Check that a scenario exists in the simulator's format and return its env directory.

The simulator builds its environment from per-chronic CSV handlers, so the
chronic folder must hold every series in ``REQUIRED_CHRONIC_SERIES``.
Built-in grid2op environment names are not supported: the simulator needs
the environment on disk.

Args:
    env_name: Validated bare name of the environment directory.
    scenario_name: Validated bare name of the chronic folder.
    scenario_directory: The folder scenarios are selected from.

Returns:
    The environment directory's path.

Raises:
    ValueError: Naming the field and what is missing.

#### `parse_session_config(payload: dict[str, Any], algorithm_directory: Path, scenario_directory: Path) -> SessionConfig`

Validate a decoded request against the section 5.3 schema.

Args:
    payload: The decoded request.
    algorithm_directory: The folder algorithms are selected from.
    scenario_directory: The folder scenarios are selected from.

Returns:
    The validated configuration with defaults filled in.

Raises:
    ValueError: On the first invalid field, naming it.

---

### `agent_wrapper.py`

Agent wrapper — serves a benchmarking-style algorithm as an InteractiveAI agent.

InteractiveAI's recommendation service (``cabrecommendation``) asks external
agents for recommendations over HTTP: it POSTs ``{event, context, ...}`` to the
URL in ``RL_AGENT_API_URL`` and expects a JSON list of recommendations back. The
setup script points that URL at this service once and for all, so switching the
algorithm per session needs no container restart: the session installs its
agent here, and the endpoint answers with it.

Per request, the wrapper:

1. reads the grid2op observation the simulator posted as context
   (``context.data.observation``, the output of ``obs.to_json()``);
2. loads it into an observation of its own environment, built exactly as the
   simulator builds its own so both share action and observation spaces;
3. calls ``agent.act(observation)`` — the benchmarking contract, one argument;
4. returns one recommendation whose ``actions[0]`` is ``action.to_json()``. The
   frontend applies ``actions[0]`` and the simulator rebuilds the action from it
   with ``action_space().from_json``.

A failure here never fails the session: the wrapper answers ``[]``, which
InteractiveAI shows as "no recommendation", and records a warning.

Limitation: an observation rebuilt from JSON is not attached to the simulator's
live environment, so algorithms that call ``observation.simulate`` get the
wrapper environment's forecast state, not the participant's.

Spec coverage: FR-12, FR-16, FR-17, FR-18, FR-19, FR-19a, section 5.5

#### `build_simulator_environment(environment_directory: Path, scenario: ScenarioSelection, backend_factory: Optional[Callable[[], Any]]=None) -> Any`

Build a grid2op environment the way the PowerGrid simulator builds its own (FR-17).

The arguments are copied from ``Simulator.initialize_simulation`` at
InteractiveAI 600a1ce: LightSim backend, ``FromHandlers`` chronics with CSV
handlers for the injections and perfect-forecast handlers for the forecasts.
A plain ``grid2op.make(path)`` would pick a different chronics class, and
the simulator's observations would not load into it.

Args:
    environment_directory: The grid2op environment directory.
    scenario: Selects the chronic and seed.
    backend_factory: Builds the backend; LightSimBackend when omitted.

Returns:
    The environment, seeded, set to the chronic and reset.

Raises:
    ValueError: If the chronic is not in the environment.

#### `load_algorithm_module(algorithm_path: Path) -> ModuleType`

Import an algorithm file under a unique module name.

A unique name per load means two sessions using files of the same name, or
the same file edited in between, never share a cached module.

Args:
    algorithm_path: The ``.py`` file.

Returns:
    The imported module.

Raises:
    ImportError: If the file cannot be loaded.
    AttributeError: If it defines no ``build_agent``.

#### `build_agent_context(config: SessionConfig) -> dict[str, Any]`

Return the ``context`` argument handed to ``build_agent`` (section 5.5).

Args:
    config: The validated session request.

Returns:
    The context mapping.

#### class `ActiveAgent`

The algorithm currently answering recommendation requests.

Attributes:
    job_id: Session the agent belongs to.
    environment: Wrapper environment, sharing the simulator's spaces.
    agent: Object returned by ``build_agent``, with ``act(observation)``.
    session_warnings: Where request failures are recorded.
    act_executor: Runs ``agent.act`` so a slow call can be timed out (FR-19a).
        One worker, so calls never overlap on a non-thread-safe agent.

#### `activate_algorithm(config: SessionConfig, job_id: str, session_warnings: SessionWarnings, backend_factory: Optional[Callable[[], Any]]=None) -> ActiveAgent`

Build the wrapper environment and the session's agent (FR-12).

Args:
    config: The validated session request.
    job_id: The session's job id.
    session_warnings: The session's warning log.
    backend_factory: Overrides the grid2op backend, for tests.

Returns:
    The agent, ready to install in an ``AgentRegistry``.

Raises:
    Exception: Whatever the environment build, the import or ``build_agent`` raises.

#### class `AgentRegistry`

Holds the one active agent, shared between the session job and request threads.

Example:
    >>> registry = AgentRegistry()
    >>> registry.current() is None
    True

##### `AgentRegistry.activate(self, active_agent: ActiveAgent) -> None`

Install the session's agent, replacing any previous one.

Args:
    active_agent: The agent to answer requests with from now on.

##### `AgentRegistry.deactivate(self, job_id: str | None=None) -> None`

Remove the active agent, so requests are answered with ``[]``.

Args:
    job_id: Only deactivate if the active agent belongs to this session.
        None deactivates whatever is active.

##### `AgentRegistry.current(self) -> ActiveAgent | None`

Return the active agent, or None.

#### `extract_observation_payload(request_body: Any) -> dict[str, Any]`

Return the serialised observation from a recommendation request.

The simulator posts its context as ``{"date", "data": {"observation",
"topology"}, "use_case"}``, and cabrecommendation forwards it as ``context``.

Args:
    request_body: The decoded POST body.

Returns:
    The ``obs.to_json()`` mapping.

Raises:
    ValueError: Naming the top-level keys present, if the observation is missing.

#### `rebuild_observation(environment: Any, observation_payload: dict[str, Any]) -> Any`

Load a simulator observation into an observation of the wrapper environment.

Starts from a copy of the environment's current observation, which has the
right class and shapes, and overwrites every serialised attribute with
``from_json``. (``observation_space()`` is not a constructor in grid2op 1.9.)

Args:
    environment: The wrapper environment.
    observation_payload: Output of the simulator's ``obs.to_json()``.

Returns:
    A grid2op observation equal to the simulator's.

#### `action_to_recommendation(action: Any, environment: Any) -> dict[str, Any]`

Turn a grid2op action into the recommendation format InteractiveAI expects (FR-18).

Args:
    action: The algorithm's action.
    environment: The wrapper environment, for the do-nothing comparison.

Returns:
    One recommendation mapping.

#### `recommend(request_body: Any, active_agent: ActiveAgent | None, act_timeout_seconds: float) -> list[dict]`

Answer one recommendation request; never raises (FR-18, FR-19, FR-19a).

Args:
    request_body: The decoded POST body.
    active_agent: The session's agent, or None outside a session.
    act_timeout_seconds: Longest ``agent.act`` may take.

Returns:
    One recommendation, or ``[]`` when there is no session or anything fails.

#### `create_agent_router(registry: AgentRegistry, settings_provider: Callable[[], HAISettings]) -> APIRouter`

Build the router cabrecommendation calls as its agent (FR-16).

Args:
    registry: Supplies the active agent.
    settings_provider: Returns the current settings (token, act timeout).

Returns:
    A router with ``POST /api/v1/recommendation`` and ``GET /api/v1/health``.

---

### `interactiveai_client.py`

InteractiveAI client — the few HTTP calls the session makes to the CAB stack.

The service talks to InteractiveAI only through its nginx gateway (the
``frontend`` container), as the test account:

- ``POST /auth/token`` — a Keycloak password grant for ``opfab-client``, as in
  InteractiveAI's ``resources/getToken.sh`` (FR-21);
- ``GET /cabrecommendation/api/v1/health`` — preflight (FR-11);
- ``GET /cabhistoric/api/v1/traces`` — the session's traces (FR-20).

It reaches the simulator once, with ``GET /``, to check that it is up before a
session goes live. It never drives the simulator: the participant does.

The HTTP client and the sleep function are injectable, so tests run against an
``httpx.MockTransport`` without waiting.

Spec coverage: FR-11, FR-20, FR-20a, FR-21

#### class `PreflightError`

Raised when a dependency of the session is not reachable before go-live.

#### class `TraceCollectionError`

Raised when the session's traces cannot be fetched after every retry.

#### `format_trace_query_time(moment: datetime) -> str`

Format a UTC moment for cabhistoric's ``date_start`` / ``date_end`` filter.

Args:
    moment: A timezone-aware UTC datetime.

Returns:
    Naive ISO-8601 seconds in UTC, e.g. ``2026-11-11T10:00:00``.

#### class `InteractiveAIClient`

Calls the InteractiveAI gateway and simulator on behalf of a session.

Example:
    >>> client = InteractiveAIClient(HAISettings.from_environment())
    >>> client.run_preflight()            # raises PreflightError when down
    >>> traces, query = client.fetch_traces(start, end)

##### `InteractiveAIClient.fetch_access_token(self) -> str`

Obtain an access token for the test account (FR-21).

Returns:
    The bearer token.

Raises:
    RuntimeError: If the gateway rejects the request or returns no token.

##### `InteractiveAIClient.run_preflight(self) -> None`

Check the simulator, the recommendation gateway and authentication (FR-11).

Raises:
    PreflightError: Naming the first check that failed and its URL.

##### `InteractiveAIClient.build_trace_query(self, window_start: datetime, window_end: datetime) -> dict[str, str]`

Describe the traces query for a session window, margin included (FR-20).

Args:
    window_start: Session go-live, UTC.
    window_end: Session deadline, UTC.

Returns:
    ``url`` (full query URL), ``date_start``, ``date_end`` and ``use_case``.

##### `InteractiveAIClient.fetch_traces(self, window_start: datetime, window_end: datetime) -> tuple[list[Any], dict[str, str]]`

Fetch every PowerGrid trace of a session window, retrying with back-off (FR-20a).

Args:
    window_start: Session go-live, UTC.
    window_end: Session deadline, UTC.

Returns:
    The traces unchanged, and the query description for the result metadata.

Raises:
    TraceCollectionError: Including the query URL, once every attempt failed,
        so the traces can be fetched by hand. They stay in cabhistoric.

---

### `simulator_config.py`

Simulator config — the CONFIG.toml that selects the participant's scenario.

The PowerGrid simulator rereads ``config/CONFIG.toml`` every time "Load
simulation" is clicked (``Simulator.load_and_edit_config``). This service and
the simulator share that file through a single-file bind mount, so writing it
is all it takes to set the next run's scenario. The simulator is never called.

Two details keep the mount intact:

- the file is rewritten **in place** (truncate and write). A temp file renamed
  over it would replace the inode, and the simulator's mount would keep
  pointing at the old one;
- every key of the shipped file is written, not only the ones a request sets,
  because the simulator indexes keys such as ``assistant_path`` directly.

Spec coverage: FR-13, section 5.6

#### `render_simulator_config(scenario: ScenarioSelection, simulator_env_prefix: str) -> dict[str, Any]`

Build the full CONFIG.toml content for a scenario (section 5.6).

Args:
    scenario: The validated scenario selection.
    simulator_env_prefix: Path of the scenarios mount as the simulator sees
        it, relative to its working directory, e.g. ``Ressources/hai_scenarios``.

Returns:
    A mapping with every key of the simulator's shipped CONFIG.toml, in its order.

#### `write_simulator_config_in_place(config_path: Path, config_values: dict[str, Any]) -> None`

Overwrite the shared CONFIG.toml without replacing the file.

Args:
    config_path: The bind-mounted CONFIG.toml.
    config_values: Content from ``render_simulator_config``.

Raises:
    FileNotFoundError: If the file does not exist. The setup script creates
        it before the simulator starts (FR-23a); a missing file means the
        mount is not in place and writing a new one would not reach the
        simulator.

---

### `session_warnings.py`

Session warnings — a thread-safe, capped log of what went wrong but did not fail.

A broken algorithm raises on every recommendation request, which in a long
session would mean thousands of identical warnings in the result. The log keeps
the first ``MAXIMUM_RECORDED_WARNINGS`` and counts the rest.

Written to from request threads (the agent endpoint) and read by the session
job thread, hence the lock.

Spec coverage: FR-19, section 5.7 (``metadata.warnings``)

#### class `SessionWarnings`

Collects warning messages for one session, keeping at most a fixed number.

Example:
    >>> session_warnings = SessionWarnings(maximum_recorded=1)
    >>> session_warnings.add("first")
    >>> session_warnings.add("second")
    >>> session_warnings.as_list()
    ['first', '1 further warning(s) not recorded']

##### `SessionWarnings.add(self, message: str) -> None`

Record one warning, or count it once the cap is reached.

Args:
    message: One-line description.

##### `SessionWarnings.as_list(self) -> list[str]`

Return the recorded warnings, followed by a count of dropped ones if any.

Returns:
    A new list, safe to serialise.

---

### `settings.py`

Settings — every tunable of the HAI session service in one place.

The service reads its deployment configuration from the environment once, into
a frozen ``HAISettings``. Fixed protocol values — retry schedules, validation
bounds, the simulator's default CONFIG.toml entries — are module constants
next to it, each with a note on where its value comes from.

Nothing here performs I/O beyond reading ``os.environ``, so importing the
module is safe in tests.

Spec coverage: section 5.8 (environment variables), NFR-04, FR-11, FR-19a,
FR-20, FR-20a (timing constants)

#### class `HAISettings`

Deployment configuration of the HAI session service (spec section 5.8).

Attributes:
    algorithm_directory: Where algorithm ``.py`` files are selected from.
    scenario_directory: Where grid2op environment directories are selected from.
    simulator_config_path: The CONFIG.toml shared with the simulator by bind mount.
    simulator_env_prefix: The scenarios mount as the simulator sees it,
        relative to its working directory ``/code``.
    cab_internal_url: InteractiveAI gateway as this service reaches it.
    simulator_internal_url: Simulator as this service reaches it.
    frontend_public_url: InteractiveAI frontend link for the participant.
    simulator_public_url: Simulator link for the participant.
    simulator_cab_server: Server the participant selects in the simulator login.
    cab_username: InteractiveAI test account user.
    cab_password: InteractiveAI test account password.
    agent_token: Bearer token required on the agent endpoint, or None for open.
    agent_act_timeout_seconds: Longest an algorithm may take per recommendation.
    trace_window_margin_seconds: Widening of the trace query window on each side.
    interactiveai_commit: InteractiveAI commit recorded in the result, if known.

##### `HAISettings.from_environment(cls, environ: Mapping[str, str] | None=None) -> 'HAISettings'`

Read the settings from environment variables, applying section 5.8 defaults.

Args:
    environ: Mapping to read from; ``os.environ`` when omitted.

Returns:
    The settings.

Raises:
    ValueError: If a numeric variable is set to an invalid value.

---

## Requirements Coverage

| Requirement | Status | Implemented in |
|-------------|--------|---------------|
| FR-01 | ✓ | `service.py: execute_RunHAISession()`, `run(...)` |
| FR-02 | ✓ | `session_config.py: read_request_payload()`, `decode_inline_json()` |
| FR-03 | ✓ | `session_config.py: parse_session_config()`; `service.py` returns `failed` |
| FR-04 | ✓ | `service.py`: `BatchJobRunner.submit` (`MAX_CONCURRENT_JOBS=1`) |
| FR-05 | ✓ | `session.py: SessionCoordinator.reserve()`, checked before submit |
| FR-06 | ✓ | `session.py: compute_progress()`, `_wait_until_deadline()` |
| FR-07 | ✓ | `common` control plane + `artifact_store` |
| FR-08 | ✓ | `session.py: create_intermediate_router()` |
| FR-09 | ✓ | `session.py: SessionCoordinator._go_live()` (log), `build_session_result()` |
| FR-10 | ✓ | `session.py: build_intermediate_result()` (`TODO(orchestrator)`) |
| FR-11 | ✓ | `interactiveai_client.py: InteractiveAIClient.run_preflight()` |
| FR-12 | ✓ | `agent_wrapper.py: activate_algorithm()`, `load_algorithm_module()` |
| FR-13 | ✓ | `simulator_config.py: render_simulator_config()`, `write_simulator_config_in_place()` |
| FR-14 | ✓ | `session.py: SessionCoordinator._go_live()` |
| FR-15 | ✓ | `session.py: SessionCoordinator._wait_until_deadline()`, `SessionClock` |
| FR-16 | ✓ | `agent_wrapper.py: create_agent_router()` |
| FR-16a | ✓ | `session.py: SessionCoordinator.run_session()` (`finally`) |
| FR-17 | ✓ | `agent_wrapper.py: build_simulator_environment()` |
| FR-18 | ✓ (deviation) | `agent_wrapper.py: rebuild_observation()`, `action_to_recommendation()`. Starts from `env.get_obs().copy()`: `observation_space()` is not a constructor in grid2op 1.9 |
| FR-19 | ✓ | `agent_wrapper.py: recommend()`; `session_warnings.py: SessionWarnings` |
| FR-19a | ✓ | `agent_wrapper.py: recommend()` (single-worker executor, timeout) |
| FR-20 | ✓ | `interactiveai_client.py: build_trace_query()`, `fetch_traces()` |
| FR-20a | ✓ | `interactiveai_client.py: fetch_traces()` (5/15/45 s), `TraceCollectionError` |
| FR-21 | ✓ | `interactiveai_client.py: fetch_access_token()` |
| FR-22 | ✓ (deviation) | `scripts/setup_interactiveai.sh`. Starts the backend with `docker compose --env-file runtime/interactiveai.env` instead of `docker-compose.sh`, which sources `.secrets` after the caller's env and would override `RL_AGENT_API_URL` |
| FR-23 / FR-23a | ✓ | `simulator.override.yml`; setup script `prepare_runtime_files()` |
| FR-24 | ✓ | setup script `connect_network()` |
| FR-25 | ✓ | setup script `verify()`, `print_summary()` |
| FR-26 | ✓ | `docker-compose.yml`, `Dockerfile` |
| FR-27 | ✓ | Old `hai/`, survey wrapper, proxy, Redis/slots, `docker-compose-all.yml`, `hmisurveys` submodule removed |
| FR-28 | ✓ | `connections.json`, `export/` regenerated; `proto/hai_session.proto` added for the generator |
| FR-29 | ✓ | `algorithms/algorithm_template.py`, `scenarios/README.md` |
| FR-30 | ✓ | `run_workflow.sh`, `submit-workflow.sh` |
| FR-31 | ✓ | Use-case and service READMEs |
| NFR-04 | ✓ | `settings.py: MINIMUM/MAXIMUM_SESSION_DURATION_SECONDS` |
| NFR-05 / NFR-06 | ✓ | `_check_api_key` on `/control/intermediate`; `HAI_AGENT_TOKEN` on the agent endpoint |
| NFR-07 | ✓ | `session_config.py: require_plain_name()` |
| NFR-08 | ✓ | `hai_artifacts` volume |
| NFR-09 | ✓ | `requirements.txt` pins (+ `pandas<2.3`, `setuptools<81`, found necessary) |

---

## Open Items

- **Open question 1 (resolved):** Grid2Op 1.9.8 and LightSim2Grid 0.7.5 install on Python 3.11. Two more pins were needed: `pandas<2.3` (pandas 3 breaks pandapower 2.13.1) and `setuptools<81` (grid2op imports `pkg_resources`).
- **Open question 2 (open):** timezone of cabhistoric trace dates. The query uses naive UTC ± 900 s. Verify on the first end-to-end run.
- **Open question 3 (open):** whether the traces API needs a token. One is sent regardless.
- **Unverified assumption:** the observation sits at `context.data.observation` in a real `cab_recommendation` request. If it does not, the wrapper answers `[]` and records the request's top-level keys as a warning.
- **Not run:** the manual end-to-end session (full InteractiveAI stack plus a participant).
- `/control/intermediate` answers `409` for a session that failed before going live (it never became ready).
