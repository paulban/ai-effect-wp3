"""gRPC servicer, Docker container management, and result polling for Human-AI Testing.

This module owns the three gRPC RPCs exposed by HumanAIInteractionTestingService and
all supporting logic: launching the InteractiveAI Docker container per session,
monitoring the shared results directory for the results JSON produced by the container,
enforcing session timeouts, and cleaning up containers when sessions terminate.

Environment variables consumed (all optional, defaults documented below):
  HAI_INTERACTIVE_AI_IMAGE   Docker image for InteractiveAI (no default — must be set)
  HAI_INTERACTIVE_AI_PORT    Host port to expose the InteractiveAI GUI on (default: 8090)
  HAI_GUI_BASE_URL           Base URL returned as gui_url to callers
                             (default: http://host.docker.internal:8090)
  HAI_RESULTS_HOST_PATH      Absolute host path for the shared results directory
                             (default: /tmp/hai_sessions)
  HAI_RESULTS_CONTAINER_PATH Path inside this service container where the results
                             directory is mounted (default: /hai-sessions)
  HAI_DOCKER_NETWORK         Docker network to attach InteractiveAI to
                             (default: ai-effect-services)
  GRPC_PORT                  gRPC server port for this service (default: 50051)
  HAI_RESULTS_FILENAME       Filename written by InteractiveAI on survey submit
                             (default: session_result.json)

Spec coverage: FR-01, FR-03, FR-08, FR-09, FR-10, FR-13, FR-15
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent import futures
from pathlib import Path
from typing import Any

import grpc

from .proto_runtime import ensure_generated
from .session_manager import SessionPhase, get_session_manager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven configuration
# ---------------------------------------------------------------------------

# Docker image for InteractiveAI — MUST be set in the environment.
# TODO (OQ-4): Confirm the exact image name/tag with AI4REALNET.
INTERACTIVE_AI_IMAGE: str = os.environ.get("HAI_INTERACTIVE_AI_IMAGE", "")

# Host port where InteractiveAI's web-app GUI is reachable from the browser.
# TODO (OQ-5): Confirm the default port with the InteractiveAI documentation.
INTERACTIVE_AI_HOST_PORT: int = int(os.environ.get("HAI_INTERACTIVE_AI_PORT", "8090"))

# The gui_url returned to callers — must be reachable from the operator's browser.
# On Docker Desktop (Mac/Windows) host.docker.internal resolves to the host machine.
# On Linux, set this to the host IP or a hostname accessible to the operator.
GUI_BASE_URL: str = os.environ.get(
    "HAI_GUI_BASE_URL",
    f"http://host.docker.internal:{INTERACTIVE_AI_HOST_PORT}",
)

# Host-side absolute path where per-session result subdirectories are created.
# This path must be mounted into this service container (see docker-compose-all.yml).
RESULTS_HOST_BASE_PATH: str = os.environ.get(
    "HAI_RESULTS_HOST_PATH", "/tmp/hai_sessions"
)

# Path inside THIS container where RESULTS_HOST_BASE_PATH is mounted.
RESULTS_CONTAINER_BASE_PATH: str = os.environ.get(
    "HAI_RESULTS_CONTAINER_PATH", "/hai-sessions"
)

# Docker network the InteractiveAI container is attached to.
DOCKER_NETWORK: str = os.environ.get("HAI_DOCKER_NETWORK", "ai-effect-services")

# Filename written by InteractiveAI when the operator submits the survey.
# TODO (OQ-1): Confirm this path with the InteractiveAI results-export hook.
RESULTS_FILENAME: str = os.environ.get("HAI_RESULTS_FILENAME", "session_result.json")

# How often (in seconds) the background thread checks for the results file.
POLL_INTERVAL_SECONDS: float = 5.0

# ---------------------------------------------------------------------------
# Lazy proto import — modules are generated at startup by ensure_generated().
# ---------------------------------------------------------------------------
ensure_generated("human_ai_interaction_testing.proto")
import human_ai_interaction_testing_pb2 as hai_pb2  # type: ignore  # noqa: E402
import human_ai_interaction_testing_pb2_grpc as hai_pb2_grpc  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Result parsing helpers
# ---------------------------------------------------------------------------

def _metric_value_from_any(value: Any) -> hai_pb2.MetricValue:
    """Convert a Python value to a MetricValue protobuf message.

    Mirrors the same function in the benchmarking service so result structures
    are consistent across WP3 evaluation services.

    Args:
        value: Any scalar, list of numbers, dict, or other value from the
               results JSON.

    Returns:
        A MetricValue with the appropriate oneof field set.
    """
    metric = hai_pb2.MetricValue()

    if isinstance(value, bool):
        metric.text = str(value)
        return metric

    if isinstance(value, (int, float)):
        metric.scalar = float(value)
        return metric

    if isinstance(value, list) and all(isinstance(item, (int, float)) for item in value):
        metric.series.values.extend(float(item) for item in value)
        return metric

    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(nested, (dict, list)):
                metric.attributes.values[str(key)] = json.dumps(nested)
            else:
                metric.attributes.values[str(key)] = str(nested)
        return metric

    metric.text = str(value)
    return metric


def _parse_results_file(results_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse the JSON results file written by the InteractiveAI container.

    The file is expected to have two top-level keys:
      - "kpis": dict of grid performance metric name → value
      - "survey_outcomes": dict of survey field name → value

    Keys are never hardcoded here — they are taken as-is from the JSON so the
    service remains forward-compatible with InteractiveAI and hmisurveys updates.

    Args:
        results_path: Absolute path to the results JSON file inside this container.

    Returns:
        A tuple (kpis, survey_outcomes) where each is a dict of string keys to
        arbitrary Python values.

    Raises:
        ValueError: If the file is not valid JSON or missing expected top-level keys.
    """
    try:
        raw = results_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Results file is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Results file must contain a JSON object; got {type(data).__name__}"
        )

    kpis: dict[str, Any] = data.get("kpis", {})
    survey_outcomes: dict[str, Any] = data.get("survey_outcomes", {})

    if not isinstance(kpis, dict):
        raise ValueError(
            f"results JSON 'kpis' must be an object; got {type(kpis).__name__}"
        )
    if not isinstance(survey_outcomes, dict):
        raise ValueError(
            f"results JSON 'survey_outcomes' must be an object; "
            f"got {type(survey_outcomes).__name__}"
        )

    return kpis, survey_outcomes


# ---------------------------------------------------------------------------
# Docker container management
# ---------------------------------------------------------------------------

def _get_docker_client():
    """Return a Docker SDK client connected to the local Docker daemon.

    Raises:
        RuntimeError: If the docker package is not installed or the socket is
                      not accessible.
    """
    try:
        import docker  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "The 'docker' package is required. "
            "Install it with: pip install docker"
        ) from exc

    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception as exc:
        raise RuntimeError(
            "Cannot connect to Docker daemon. "
            "Ensure /var/run/docker.sock is mounted into this container "
            "and the Docker socket is accessible."
        ) from exc


def _create_session_results_directory(session_id: str) -> tuple[str, str]:
    """Create the host-side results directory for a session.

    Returns both the host-absolute path (passed to Docker SDK when launching
    the InteractiveAI container) and the container-local path (used by this
    service to poll for the results file).

    Args:
        session_id: Unique session identifier used as the directory name.

    Returns:
        A tuple (host_path, container_path) — both are absolute path strings.
    """
    host_session_dir = os.path.join(RESULTS_HOST_BASE_PATH, session_id)
    container_session_dir = os.path.join(RESULTS_CONTAINER_BASE_PATH, session_id)

    # Create the directory on the container-visible mount so it exists before
    # the InteractiveAI container tries to write to it.
    Path(container_session_dir).mkdir(parents=True, exist_ok=True)

    return host_session_dir, container_session_dir


def _launch_interactive_ai_container(
    session_id: str,
    host_results_path: str,
    spec: Any,
) -> tuple[str, str]:
    """Launch the InteractiveAI Docker container for a testing session.

    The container is started in web-app / browser-served simulator mode (FR-15)
    as this mode is more stable than alternative launch modes. The session results
    directory is mounted so the container can write the results JSON on survey
    submit (FR-08).

    Args:
        session_id: Unique identifier for this session (used for container naming).
        host_results_path: Absolute host-side path for the results volume mount.
        spec: The parsed HumanAISessionSpec protobuf message.

    Returns:
        A tuple (container_id, gui_url) — the Docker container ID and the URL
        the operator should open in a browser.

    Raises:
        RuntimeError: If INTERACTIVE_AI_IMAGE is not configured or Docker fails.
    """
    if not INTERACTIVE_AI_IMAGE:
        raise RuntimeError(
            "HAI_INTERACTIVE_AI_IMAGE environment variable is not set. "
            "Set it to the InteractiveAI Docker image name/tag before starting "
            "the service. See README.md for setup instructions."
        )

    docker_client = _get_docker_client()

    container_name = f"hai-interactive-ai-{session_id[:8]}"

    # Build environment variables to pass to InteractiveAI.
    # These configure the scenario, agent, and survey selection.
    # TODO (OQ-1, OQ-2, OQ-3): Align these env var names with the actual
    # InteractiveAI configuration interface once confirmed with AI4REALNET.
    interactive_ai_env: dict[str, str] = {
        "HAI_SESSION_ID": session_id,
        "HAI_RESULTS_PATH": "/results",  # mount point inside InteractiveAI container
        "HAI_RESULTS_FILENAME": RESULTS_FILENAME,
    }

    scenario_name = spec.scenario.name if spec.scenario.name else ""
    agent_name = spec.agent.name if spec.agent.name else ""
    survey_id = spec.survey.survey_id if spec.survey.survey_id else ""

    if scenario_name:
        interactive_ai_env["HAI_SCENARIO"] = scenario_name
    if agent_name:
        interactive_ai_env["HAI_AGENT"] = agent_name
    if survey_id:
        interactive_ai_env["HAI_SURVEY_ID"] = survey_id

    if spec.kpis:
        interactive_ai_env["HAI_KPIS"] = ",".join(spec.kpis)

    logger.info(
        "Launching InteractiveAI container: session=%s image=%s port=%s",
        session_id,
        INTERACTIVE_AI_IMAGE,
        INTERACTIVE_AI_HOST_PORT,
    )

    # Launch in web-app mode — more stable than other InteractiveAI modes (FR-15, NFR-02).
    container = docker_client.containers.run(
        INTERACTIVE_AI_IMAGE,
        detach=True,
        name=container_name,
        network=DOCKER_NETWORK,
        environment=interactive_ai_env,
        volumes={
            host_results_path: {"bind": "/results", "mode": "rw"},
        },
        ports={
            "8080/tcp": INTERACTIVE_AI_HOST_PORT,
        },
        labels={
            "hai.session_id": session_id,
            "hai.service": "interactive-ai-testing",
        },
    )

    logger.info(
        "InteractiveAI container started: id=%s name=%s session=%s",
        container.id[:12],
        container_name,
        session_id,
    )

    return container.id, GUI_BASE_URL


def _stop_and_remove_container(container_id: str, session_id: str) -> None:
    """Stop and remove the InteractiveAI container to prevent resource leaks.

    Called when a session reaches COMPLETED or FAILED (FR-13). Failures during
    cleanup are logged but not re-raised so they do not mask the primary result.

    Args:
        container_id: Docker container ID to stop and remove.
        session_id: Session ID for log context.
    """
    if not container_id:
        return

    try:
        docker_client = _get_docker_client()
        container = docker_client.containers.get(container_id)
        container.stop(timeout=10)
        container.remove()
        logger.info(
            "Cleaned up InteractiveAI container: id=%s session=%s",
            container_id[:12],
            session_id,
        )
    except Exception as exc:
        logger.warning(
            "Container cleanup failed (non-fatal): id=%s session=%s error=%s",
            container_id[:12] if container_id else "unknown",
            session_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Background polling thread
# ---------------------------------------------------------------------------

def _run_session_polling_thread(
    session_id: str,
    container_id: str,
    container_session_dir: str,
    session_timeout_seconds: int,
) -> None:
    """Background thread that monitors a session and drives phase transitions.

    Responsibilities:
    1. Polls the results directory for the results JSON file (FR-09).
    2. On file detection, parses results and transitions to COMPLETED (FR-08).
    3. Monitors the session timeout; transitions to FAILED on expiry (FR-10).
    4. Cleans up the Docker container on terminal phase (FR-13).

    This function is run in a daemon thread; it exits as soon as the session
    reaches a terminal phase.

    Args:
        session_id: Session to monitor.
        container_id: Docker container ID for cleanup.
        container_session_dir: Path (inside this container) to the per-session
                               results directory.
        session_timeout_seconds: Seconds after which a non-completed session
                                 is transitioned to FAILED.
    """
    session_manager = get_session_manager()
    results_file_path = Path(container_session_dir) / RESULTS_FILENAME
    deadline = time.monotonic() + session_timeout_seconds

    logger.info(
        "Session polling started: session=%s timeout=%ds results_path=%s",
        session_id,
        session_timeout_seconds,
        results_file_path,
    )

    try:
        while True:
            current_time = time.monotonic()

            # Check for timeout before anything else (FR-10).
            if current_time >= deadline:
                logger.warning(
                    "Session timed out: session=%s timeout=%ds",
                    session_id,
                    session_timeout_seconds,
                )
                session_manager.advance_phase(
                    session_id,
                    SessionPhase.FAILED,
                    error_message=(
                        f"Session timed out after {session_timeout_seconds} seconds "
                        "without survey submission."
                    ),
                )
                break

            # Check if results file has appeared (FR-09).
            if results_file_path.exists():
                logger.info(
                    "Results file detected: session=%s path=%s",
                    session_id,
                    results_file_path,
                )

                try:
                    kpis, survey_outcomes = _parse_results_file(results_file_path)
                    session_manager.advance_phase(
                        session_id,
                        SessionPhase.COMPLETED,
                        kpis=kpis,
                        survey_outcomes=survey_outcomes,
                        session_metadata={"results_file": str(results_file_path)},
                    )
                    logger.info(
                        "Session completed: session=%s kpi_count=%d survey_count=%d",
                        session_id,
                        len(kpis),
                        len(survey_outcomes),
                    )
                    break

                except ValueError as exc:
                    logger.error(
                        "Failed to parse results file: session=%s error=%s",
                        session_id,
                        exc,
                    )
                    session_manager.advance_phase(
                        session_id,
                        SessionPhase.FAILED,
                        error_message=f"Results file parse error: {exc}",
                    )
                    break

            time.sleep(POLL_INTERVAL_SECONDS)

    except Exception as exc:
        logger.exception(
            "Unexpected error in polling thread: session=%s", session_id
        )
        try:
            session_manager.advance_phase(
                session_id,
                SessionPhase.FAILED,
                error_message=f"Internal polling error: {exc}",
            )
        except Exception:
            pass  # Session may already be terminal; ignore secondary errors.

    finally:
        # Always clean up the container when exiting the polling thread (FR-13).
        _stop_and_remove_container(container_id, session_id)


# ---------------------------------------------------------------------------
# gRPC servicer
# ---------------------------------------------------------------------------

class HumanAIInteractionTestingServicer(
    hai_pb2_grpc.HumanAIInteractionTestingServiceServicer
):
    """gRPC servicer implementing the three Human-AI Interaction Testing RPCs.

    Spec coverage: FR-01, FR-02, FR-03
    """

    def StartHumanAISession(self, request, context):
        """Launch a new human-AI interaction testing session.

        Validates the incoming HumanAISessionSpec, checks that no other session
        is currently active (v1 constraint, NFR-03), launches the InteractiveAI
        Docker container, starts the background polling thread, and returns the
        session_id and gui_url immediately.

        Args:
            request: HumanAISessionSpec protobuf message from the caller.
            context: gRPC server context for setting status codes.

        Returns:
            StartSessionResponse with session_id and gui_url on success.
        """
        session_manager = get_session_manager()

        # Validate required fields (FR-04).
        if request.session_timeout_seconds <= 0:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(
                "session_timeout_seconds must be > 0"
            )
            return hai_pb2.StartSessionResponse(
                success=False,
                message="session_timeout_seconds must be > 0",
            )

        # Enforce single-session-at-a-time constraint (NFR-03 / v1 limitation).
        if session_manager.has_active_session():
            context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
            context.set_details(
                "A session is already active. Only one session at a time is "
                "supported in v1. Wait for the current session to complete or fail."
            )
            return hai_pb2.StartSessionResponse(
                success=False,
                message="Another session is currently active.",
            )

        session_id = uuid.uuid4().hex
        logger.info("Starting new session: session=%s", session_id)

        try:
            # Create the session record before launching the container so the
            # polling thread can reference it immediately.
            session_manager.create(session_id)

            host_results_path, container_results_path = (
                _create_session_results_directory(session_id)
            )

            container_id, gui_url = _launch_interactive_ai_container(
                session_id,
                host_results_path,
                request,
            )

            # Transition to GUI_READY now that the container is running (FR-01).
            session_manager.advance_phase(
                session_id,
                SessionPhase.GUI_READY,
                gui_url=gui_url,
                container_id=container_id,
                volume_name=session_id,
            )

            # Start the background thread that polls for results and enforces timeout.
            polling_thread = threading.Thread(
                target=_run_session_polling_thread,
                args=(
                    session_id,
                    container_id,
                    container_results_path,
                    int(request.session_timeout_seconds),
                ),
                daemon=True,
                name=f"hai-poll-{session_id[:8]}",
            )
            polling_thread.start()

            logger.info(
                "Session started: session=%s gui_url=%s timeout=%ds",
                session_id,
                gui_url,
                request.session_timeout_seconds,
            )

            return hai_pb2.StartSessionResponse(
                success=True,
                message="Session started. Share the gui_url with the operator.",
                session_id=session_id,
                gui_url=gui_url,
            )

        except Exception as exc:
            logger.exception("Failed to start session: session=%s", session_id)

            # Mark the session failed if it was created before the error.
            try:
                session_manager.advance_phase(
                    session_id,
                    SessionPhase.FAILED,
                    error_message=str(exc),
                )
            except Exception:
                pass

            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return hai_pb2.StartSessionResponse(
                success=False,
                message=str(exc),
            )

    def GetSessionStatus(self, request, context):
        """Return the current phase of a session (FR-02).

        Args:
            request: SessionStatusRequest with session_id.
            context: gRPC server context.

        Returns:
            SessionStatusResponse with current phase and optional error_message.
        """
        session_manager = get_session_manager()
        state = session_manager.get(request.session_id)

        if state is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Session not found: {request.session_id}")
            return hai_pb2.SessionStatusResponse(
                success=False,
                message=f"Session not found: {request.session_id}",
                session_id=request.session_id,
            )

        # Map the internal SessionPhase enum to the proto enum value.
        proto_phase = hai_pb2.SessionPhase.Value(
            f"SESSION_PHASE_{state.phase.name}"
        )

        return hai_pb2.SessionStatusResponse(
            success=True,
            message=f"Session phase: {state.phase.name}",
            session_id=request.session_id,
            phase=proto_phase,
            error_message=state.error_message,
        )

    def GetSessionResult(self, request, context):
        """Return grid KPIs and survey outcomes for a completed session (FR-03).

        Returns a FAILED_PRECONDITION error if the session has not yet reached
        COMPLETED, and NOT_FOUND if the session_id is unknown.

        Args:
            request: SessionResultRequest with session_id.
            context: gRPC server context.

        Returns:
            SessionResultResponse with kpis and survey_outcomes maps.
        """
        session_manager = get_session_manager()
        state = session_manager.get(request.session_id)

        if state is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Session not found: {request.session_id}")
            return hai_pb2.SessionResultResponse(
                success=False,
                message=f"Session not found: {request.session_id}",
                session_id=request.session_id,
            )

        if state.phase != SessionPhase.COMPLETED:
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details(
                f"Session {request.session_id} is not yet completed "
                f"(current phase: {state.phase.name}). "
                "Poll GetSessionStatus until phase == COMPLETED."
            )
            return hai_pb2.SessionResultResponse(
                success=False,
                message=f"Session not yet completed (phase: {state.phase.name})",
                session_id=request.session_id,
            )

        # Build the response by converting the raw Python dicts to proto MetricValue maps.
        response = hai_pb2.SessionResultResponse(
            success=True,
            message="Session results available.",
            session_id=request.session_id,
        )

        for key, value in state.kpis.items():
            response.kpis[str(key)].CopyFrom(_metric_value_from_any(value))

        for key, value in state.survey_outcomes.items():
            response.survey_outcomes[str(key)].CopyFrom(_metric_value_from_any(value))

        for key, value in state.session_metadata.items():
            response.metadata[str(key)] = str(value)

        response.metadata["session_phase"] = state.phase.name

        return response


# ---------------------------------------------------------------------------
# gRPC server startup
# ---------------------------------------------------------------------------

def start_grpc_server():
    """Start the Human-AI Interaction Testing gRPC data plane server.

    Reads the port from the GRPC_PORT environment variable (default: 50051).
    The server runs in a background thread pool.

    Returns:
        The running grpc.Server instance.
    """
    grpc_port = os.environ.get("GRPC_PORT", "50051")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    hai_pb2_grpc.add_HumanAIInteractionTestingServiceServicer_to_server(
        HumanAIInteractionTestingServicer(), server
    )
    server.add_insecure_port(f"[::]:{grpc_port}")
    server.start()
    logger.info("Human-AI Interaction Testing gRPC server started on port %s", grpc_port)
    return server


# ---------------------------------------------------------------------------
# HTTP control plane handler (for WP3 orchestrator integration)
# ---------------------------------------------------------------------------

def _execute_start_session(request) -> dict[str, Any]:
    """HTTP control plane handler for 'StartHumanAISession'.

    Called by the WP3 orchestrator via POST /control/execute. Launches the
    session asynchronously and returns immediately so the orchestrator can poll
    GET /control/status/{task_id} until complete.

    Args:
        request: ExecuteRequest from the FastAPI control router.

    Returns:
        A dict with 'status', 'output' reference, and optional 'error'.
    """
    import base64

    from .control_interface import DataReference, ExecuteResponse, get_data_url
    from .session_manager import get_session_manager as _get_mgr

    try:
        # Decode the inline JSON payload carrying the session spec fields.
        if not request.inputs:
            return ExecuteResponse(
                status="failed",
                error="No input provided. Expected inline JSON with session spec.",
                task_id=request.task_id,
            )

        raw_input = request.inputs[0]
        protocol = str(raw_input.get("protocol", "")).lower()

        if protocol == "inline":
            encoded = raw_input.get("uri", "")
            spec_dict = json.loads(base64.b64decode(encoded).decode("utf-8"))
        else:
            return ExecuteResponse(
                status="failed",
                error=f"Unsupported input protocol: {protocol}. Use 'inline'.",
                task_id=request.task_id,
            )

        timeout_seconds = int(spec_dict.get("session_timeout_seconds", 0))
        if timeout_seconds <= 0:
            return ExecuteResponse(
                status="failed",
                error="session_timeout_seconds must be > 0",
                task_id=request.task_id,
            )

        session_manager = get_session_manager()
        if session_manager.has_active_session():
            return ExecuteResponse(
                status="failed",
                error="Another session is currently active. Only one at a time in v1.",
                task_id=request.task_id,
            )

        session_id = request.task_id  # use task_id as session_id for traceability
        session_manager.create(session_id)

        host_results_path, container_results_path = (
            _create_session_results_directory(session_id)
        )

        # Build a minimal spec object from the dict for container launch.
        class _SpecProxy:
            """Minimal duck-type proxy so _launch_interactive_ai_container
            can read fields without requiring the full proto message."""
            def __init__(self, d: dict):
                scenario = d.get("scenario", {})
                agent = d.get("agent", {})
                survey = d.get("survey", {})

                class _Sub:
                    def __init__(self, name="", survey_id=""):
                        self.name = name
                        self.survey_id = survey_id

                self.scenario = _Sub(name=scenario.get("name", ""))
                self.agent = _Sub(name=agent.get("name", ""))
                self.survey = _Sub(survey_id=survey.get("survey_id", ""))
                self.kpis = d.get("kpis", [])
                self.session_timeout_seconds = d.get("session_timeout_seconds", 0)

        spec_proxy = _SpecProxy(spec_dict)

        container_id, gui_url = _launch_interactive_ai_container(
            session_id, host_results_path, spec_proxy
        )

        session_manager.advance_phase(
            session_id,
            SessionPhase.GUI_READY,
            gui_url=gui_url,
            container_id=container_id,
            volume_name=session_id,
        )

        polling_thread = threading.Thread(
            target=_run_session_polling_thread,
            args=(session_id, container_id, container_results_path, timeout_seconds),
            daemon=True,
            name=f"hai-poll-{session_id[:8]}",
        )
        polling_thread.start()

        grpc_host = os.environ.get("GRPC_HOST", "hai-testing-service")
        grpc_port = os.environ.get("GRPC_PORT", "50051")

        return ExecuteResponse(
            status="pending",
            output=DataReference(
                protocol="grpc",
                uri=f"{grpc_host}:{grpc_port}",
                format="GetSessionResult",
            ),
            task_id=request.task_id,
        )

    except Exception as exc:
        logger.exception("HTTP StartHumanAISession failed")
        return ExecuteResponse(
            status="failed",
            error=str(exc),
            task_id=request.task_id,
        )


# Handler registry — consumed by the FastAPI control router.
session_handlers: dict = {
    "StartHumanAISession": _execute_start_session,
}
