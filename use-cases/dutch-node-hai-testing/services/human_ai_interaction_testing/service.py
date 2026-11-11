"""Human-AI interaction session service for AI-Effect orchestration.

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
"""

from __future__ import annotations

import logging
import sys

from agent_wrapper import AgentRegistry, create_agent_router
from common.batch_jobs import STATUS_RUNNING, BatchJobRunner, build_job_id, build_runner
from handler import ExecuteRequest, ExecuteResponse, run
from session import SessionCoordinator, create_intermediate_router
from session_config import parse_session_config, read_request_payload
from settings import HAI_SESSION_RESULT_FORMAT, HAISettings

logger = logging.getLogger(__name__)

# Built on first use so importing this module reads no environment and opens no
# files, which keeps it importable in tests. Tests install their own through
# the set_* functions.
_settings: HAISettings | None = None
_job_runner: BatchJobRunner | None = None
_agent_registry: AgentRegistry | None = None
_session_coordinator: SessionCoordinator | None = None


def set_settings(settings: HAISettings | None) -> None:
    """Install settings for this process, or None to read the environment again."""
    global _settings
    _settings = settings


def get_settings() -> HAISettings:
    """Return the process-wide settings, reading the environment on first use."""
    global _settings
    if _settings is None:
        _settings = HAISettings.from_environment()
    return _settings


def set_job_runner(runner: BatchJobRunner | None) -> None:
    """Install a job runner, or None to build a fresh one from the environment."""
    global _job_runner
    _job_runner = runner


def get_job_runner() -> BatchJobRunner:
    """Return the process-wide job runner, building it on first use."""
    global _job_runner
    if _job_runner is None:
        _job_runner = build_runner()
    return _job_runner


def get_agent_registry() -> AgentRegistry:
    """Return the process-wide agent registry, building it on first use."""
    global _agent_registry
    if _agent_registry is None:
        _agent_registry = AgentRegistry()
    return _agent_registry


def set_session_coordinator(coordinator: SessionCoordinator | None) -> None:
    """Install a session coordinator, or None to build the default on next use."""
    global _session_coordinator
    _session_coordinator = coordinator


def get_session_coordinator() -> SessionCoordinator:
    """Return the process-wide session coordinator, building it on first use."""
    global _session_coordinator
    if _session_coordinator is None:
        _session_coordinator = SessionCoordinator(get_settings(), get_agent_registry())
    return _session_coordinator


def execute_RunHAISession(request: ExecuteRequest) -> ExecuteResponse:
    """Start a human-AI interaction session and return immediately with its job id.

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
    """
    settings = get_settings()
    try:
        config = parse_session_config(
            read_request_payload(request.inputs), settings.algorithm_directory, settings.scenario_directory
        )
    except Exception as validation_error:  # noqa: BLE001 - reported to the caller
        logger.warning("RunHAISession rejected: %s", validation_error)
        return ExecuteResponse(status="failed", task_id=request.task_id, error=str(validation_error))

    coordinator = get_session_coordinator()
    job_id = build_job_id(request.workflow_id, request.task_id)
    # FR-05: checked before submitting, because the runner's own capacity error
    # cannot name the running session's deadline. The runner's cap of 1 stays as
    # a second guard.
    busy_error = coordinator.reserve(job_id)
    if busy_error is not None:
        logger.warning("RunHAISession refused: %s", busy_error)
        return ExecuteResponse(status="failed", task_id=job_id, error=busy_error)

    def run_session(report_progress) -> dict:
        """Job body: the session from preflight to collected result."""
        return coordinator.run_session(job_id, config, report_progress)

    response = get_job_runner().submit(request, run_session, data_format=HAI_SESSION_RESULT_FORMAT)
    if response.status != STATUS_RUNNING:
        coordinator.forget(job_id)
    return response


if __name__ == "__main__":
    run(
        sys.modules[__name__],
        extra_routers=[
            create_agent_router(get_agent_registry(), get_settings),
            create_intermediate_router(get_session_coordinator()),
        ],
        artifact_store=get_job_runner().artifact_store,
    )
