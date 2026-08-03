"""gRPC servicer, Docker container management, and result polling for Human-AI Testing.

This module owns the three gRPC RPCs and all supporting logic for the
two-instances design: powergrid-simulator-app (grid simulation) and
hai-survey-wrapper (survey) run as **independent Docker containers** launched
together at session start.  The operator receives two browser URLs — gui_url
for the grid GUI and survey_url for the questionnaire — and navigates between
them manually.

Result files land in the shared per-session directory on the host volume:
  - kpis.json            written by POST /collect/session-trace (FR-08, FR-12)
                         when the InteractiveAI frontend POSTs the session trace
                         to WP3 on operator logout (FR-13)
  - survey_outcomes.json written by the hai-survey-wrapper Flask sidecar (FR-09, FR-18)
                         when the operator submits the survey and the wrapper page
                         POSTs to its local /api/save_results endpoint

The background polling thread checks for BOTH files (FR-10) and only
transitions to COMPLETED when both are present. Either file alone is not
sufficient.

Environment variables consumed (all optional, defaults documented below):

  powergrid-simulator-app container (FR-01):
    HAI_INTERACTIVE_AI_IMAGE    Docker image for powergrid-simulator-app
                                (no default — must be set; build from InteractiveAI repo)
    HAI_INTERACTIVE_AI_PORT     Host port for the grid GUI Flask app (default: 8090)
                                Maps to container port 5000 (Flask default)
    HAI_GUI_BASE_URL            Returned as gui_url (default: http://host.docker.internal:8090)
    HAI_CAB_URL                 URL of the pre-running CAB platform passed to the
                                simulator container (FR-19, default: http://frontend:80)

  hai-survey-wrapper container (FR-02):
    HAI_HMISURVEYS_IMAGE        Docker image for hai-survey-wrapper
                                (no default — must be set; build from hai-survey-wrapper/)
    HAI_HMISURVEYS_PORT         Host port for the survey wrapper nginx (default: 8091)
                                Maps to container port 80 (nginx default)
    HAI_SURVEY_BASE_URL         Returned as survey_url (default: http://host.docker.internal:8091)

  Shared:
    HAI_RESULTS_HOST_PATH       Absolute host path for per-session result dirs
                                (default: /tmp/hai_sessions)
    HAI_RESULTS_CONTAINER_PATH  Mount point inside this service container
                                (default: /hai-sessions)
    HAI_DOCKER_NETWORK          Docker network for both sub-containers
                                (default: ai-effect-services)
    GRPC_PORT                   gRPC server port (default: 50051)

  Result filenames:
    HAI_KPIS_FILENAME           Written by POST /collect/session-trace (FR-12)
                                (default: kpis.json)
    HAI_SURVEY_FILENAME         Written by hai-survey-wrapper /api/save_results (FR-18)
                                (default: survey_outcomes.json)

Spec coverage: FR-01, FR-02, FR-03, FR-07, FR-08, FR-09, FR-10, FR-11,
               FR-12, FR-14, FR-16, FR-19
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
# Environment-driven configuration — InteractiveAI container
# ---------------------------------------------------------------------------

# Docker image for InteractiveAI — build locally from AI4REALNET/InteractiveAI.
# TODO (OQ-5): Confirm the image tag after building.
INTERACTIVE_AI_IMAGE: str = os.environ.get("HAI_INTERACTIVE_AI_IMAGE", "")

# Host port where the InteractiveAI web-app GUI is reachable from the operator's browser.
# TODO (OQ-6): Confirm the port by running InteractiveAI locally.
INTERACTIVE_AI_HOST_PORT: int = int(os.environ.get("HAI_INTERACTIVE_AI_PORT", "8090"))

# URL returned to callers as gui_url. Must be reachable from the operator's browser.
# On Docker Desktop (Mac/Windows) host.docker.internal resolves to the host machine.
# On Linux, replace with the host IP or a hostname the operator can reach.
GUI_BASE_URL: str = os.environ.get(
    "HAI_GUI_BASE_URL",
    f"http://host.docker.internal:{INTERACTIVE_AI_HOST_PORT}",
)

# URL of the pre-running InteractiveAI CAB platform, forwarded to the simulator
# container so it can connect to the recommendation, event, and historic services.
# Default points to the CAB frontend container on the shared Docker network (FR-19).
CAB_PLATFORM_URL: str = os.environ.get("HAI_CAB_URL", "http://frontend:80")

# ---------------------------------------------------------------------------
# Environment-driven configuration — hai-survey-wrapper container
# ---------------------------------------------------------------------------

# Docker image for hmisurveys — build locally from AI4REALNET/hmisurveys.
# TODO (OQ-5): Confirm the image tag after building.
HMISURVEYS_IMAGE: str = os.environ.get("HAI_HMISURVEYS_IMAGE", "")

# Host port where the hmisurveys survey UI is reachable from the operator's browser.
# TODO (OQ-7): Confirm the port by running hmisurveys locally.
HMISURVEYS_HOST_PORT: int = int(os.environ.get("HAI_HMISURVEYS_PORT", "8091"))

# URL returned to callers as survey_url.
SURVEY_BASE_URL: str = os.environ.get(
    "HAI_SURVEY_BASE_URL",
    f"http://host.docker.internal:{HMISURVEYS_HOST_PORT}",
)

# ---------------------------------------------------------------------------
# Environment-driven configuration — shared
# ---------------------------------------------------------------------------

# Host-side absolute path where per-session result subdirectories are created.
# Both containers mount their session subdirectory under this base path.
RESULTS_HOST_BASE_PATH: str = os.environ.get(
    "HAI_RESULTS_HOST_PATH", "/tmp/hai_sessions"
)

# Path inside THIS service container where RESULTS_HOST_BASE_PATH is mounted.
RESULTS_CONTAINER_BASE_PATH: str = os.environ.get(
    "HAI_RESULTS_CONTAINER_PATH", "/hai-sessions"
)

# Docker network both sub-containers are attached to.
DOCKER_NETWORK: str = os.environ.get("HAI_DOCKER_NETWORK", "ai-effect-services")

# Filename written by InteractiveAI on grid episode end (FR-08).
# TODO (OQ-1): Confirm this with the InteractiveAI export hook implementation.
KPIS_FILENAME: str = os.environ.get("HAI_KPIS_FILENAME", "kpis.json")

# Filename written by hmisurveys on survey submit (FR-09).
# TODO (OQ-2): Confirm this with the hmisurveys export hook implementation.
SURVEY_OUTCOMES_FILENAME: str = os.environ.get(
    "HAI_SURVEY_FILENAME", "survey_outcomes.json"
)

# How often (seconds) the background polling thread checks for result files (FR-10).
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


def _parse_results_file(results_path: Path) -> dict[str, Any]:
    """Parse a single JSON result file written by InteractiveAI or hmisurveys.

    The file must contain a JSON object at the root level. Keys are taken as-is
    so the service remains forward-compatible with tool updates (FR-08, FR-09).

    Args:
        results_path: Absolute path to the JSON file inside this container.

    Returns:
        A dict of string keys to arbitrary Python values.

    Raises:
        ValueError: If the file is not valid JSON or the root is not a JSON object.
    """
    try:
        raw = results_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Results file is not valid JSON: {results_path.name} — {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Results file must contain a JSON object at the root; "
            f"got {type(data).__name__} in {results_path.name}"
        )

    return data


# ---------------------------------------------------------------------------
# Docker container management
# ---------------------------------------------------------------------------

def _get_docker_client():
    """Return a Docker SDK client connected to the local Docker daemon.

    Raises:
        RuntimeError: If the docker package is not installed or the socket is
                      not accessible (OQ-8).
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
            "Ensure /var/run/docker.sock is mounted into this container. "
            "See README.md OQ-8 for setup instructions."
        ) from exc


def _create_session_results_directory(session_id: str) -> tuple[str, str]:
    """Create the per-session results directory and return both host and container paths.

    The host path is passed to the Docker SDK when mounting volumes into the
    InteractiveAI and hmisurveys containers. The container path is used by the
    polling thread running inside this service container.

    Args:
        session_id: Unique session identifier used as the subdirectory name.

    Returns:
        A tuple (host_path, container_path) — both are absolute path strings.
    """
    host_session_dir = os.path.join(RESULTS_HOST_BASE_PATH, session_id)
    container_session_dir = os.path.join(RESULTS_CONTAINER_BASE_PATH, session_id)

    # Create the directory on the container-visible path so it exists before
    # either sub-container tries to write into it.
    Path(container_session_dir).mkdir(parents=True, exist_ok=True)

    return host_session_dir, container_session_dir


def _launch_session_containers(
    session_id: str,
    host_results_path: str,
    spec: Any,
) -> tuple[str, str, str, str]:
    """Launch both the InteractiveAI and hmisurveys Docker containers for a session.

    InteractiveAI is launched in web-app / browser-served simulator mode (FR-16)
    as this mode is more stable than alternative launch modes. hmisurveys is
    launched as an independent container on the same Docker network.

    Both containers receive the shared results directory as a volume mount at
    /results so each tool can write its own result file independently (FR-08,
    FR-09). They do NOT communicate with each other.

    Args:
        session_id: Unique identifier for this session (used in container names).
        host_results_path: Absolute host-side path for the shared volume mount.
        spec: The parsed HumanAISessionSpec protobuf message (or duck-type proxy).

    Returns:
        A tuple (ia_container_id, hmisurveys_container_id, gui_url, survey_url).

    Raises:
        RuntimeError: If either image env var is unset or Docker cannot start a container.
    """
    if not INTERACTIVE_AI_IMAGE:
        raise RuntimeError(
            "HAI_INTERACTIVE_AI_IMAGE is not set. "
            "Build the InteractiveAI image locally and set this env var. "
            "See README.md for instructions."
        )
    if not HMISURVEYS_IMAGE:
        raise RuntimeError(
            "HAI_HMISURVEYS_IMAGE is not set. "
            "Build the hmisurveys image locally and set this env var. "
            "See README.md for instructions."
        )

    docker_client = _get_docker_client()

    # Shared volume mount: both containers write to /results inside the container,
    # which maps to host_results_path on the host filesystem (FR-08, FR-09).
    shared_volume_mount = {host_results_path: {"bind": "/results", "mode": "rw"}}

    # --- powergrid-simulator-app container (FR-01, FR-19) ---
    # The simulator Flask app runs on port 5000 inside the container.
    # It connects to the pre-running CAB platform via CAB_PLATFORM_URL (FR-19).
    interactive_ai_env: dict[str, str] = {
        "HAI_SESSION_ID": session_id,
        "HAI_RESULTS_PATH": "/results",
        "HAI_KPIS_FILENAME": KPIS_FILENAME,
        # CAB platform URL so the simulator can reach recommendation, event,
        # and historic services on the shared Docker network (FR-19).
        "CAB_API_URL": CAB_PLATFORM_URL,
    }

    scenario_name = spec.scenario.name if spec.scenario.name else ""
    agent_name = spec.agent.name if spec.agent.name else ""

    if scenario_name:
        interactive_ai_env["HAI_SCENARIO"] = scenario_name
    if agent_name:
        interactive_ai_env["HAI_AGENT"] = agent_name
    if spec.kpis:
        interactive_ai_env["HAI_KPIS"] = ",".join(spec.kpis)

    logger.info(
        "Launching powergrid-simulator-app container: session=%s image=%s port=%s",
        session_id, INTERACTIVE_AI_IMAGE, INTERACTIVE_AI_HOST_PORT,
    )
    # powergrid-simulator-app (Flask) listens on port 5000 inside the container
    # (confirmed from InteractiveAI/usecases_examples/PowerGrid/docker-compose.yml:
    # "5100:5000"). Mapped to INTERACTIVE_AI_HOST_PORT on the host (OQ-2).
    interactive_ai_container = docker_client.containers.run(
        INTERACTIVE_AI_IMAGE,
        detach=True,
        name=f"hai-ia-{session_id[:8]}",
        network=DOCKER_NETWORK,
        environment=interactive_ai_env,
        volumes=shared_volume_mount,
        ports={"5000/tcp": INTERACTIVE_AI_HOST_PORT},
        labels={"hai.session_id": session_id, "hai.role": "interactive-ai"},
    )
    logger.info(
        "InteractiveAI container started: id=%s session=%s",
        interactive_ai_container.id[:12], session_id,
    )

    # --- hai-survey-wrapper container (FR-02, FR-16) ---
    # The WP3-built wrapper image runs nginx on port 80 (static survey files)
    # with a Flask sidecar on port 5000 (internal only, proxied via nginx /api/).
    survey_env: dict[str, str] = {
        "HAI_SESSION_ID": session_id,
        "HAI_RESULTS_PATH": "/results",
        "HAI_SURVEY_FILENAME": SURVEY_OUTCOMES_FILENAME,
    }

    survey_id = spec.survey.survey_id if spec.survey.survey_id else ""
    if survey_id:
        survey_env["HAI_SURVEY_ID"] = survey_id

    logger.info(
        "Launching hai-survey-wrapper container: session=%s image=%s port=%s",
        session_id, HMISURVEYS_IMAGE, HMISURVEYS_HOST_PORT,
    )
    # hai-survey-wrapper nginx listens on port 80 inside the container.
    # Mapped to HMISURVEYS_HOST_PORT on the host (OQ-3).
    hmisurveys_container = docker_client.containers.run(
        HMISURVEYS_IMAGE,
        detach=True,
        name=f"hai-survey-{session_id[:8]}",
        network=DOCKER_NETWORK,
        environment=survey_env,
        volumes=shared_volume_mount,
        ports={"80/tcp": HMISURVEYS_HOST_PORT},
        labels={"hai.session_id": session_id, "hai.role": "hmisurveys"},
    )
    logger.info(
        "hmisurveys container started: id=%s session=%s",
        hmisurveys_container.id[:12], session_id,
    )

    return (
        interactive_ai_container.id,
        hmisurveys_container.id,
        GUI_BASE_URL,
        SURVEY_BASE_URL,
    )


def _stop_and_remove_container(container_id: str, role: str, session_id: str) -> None:
    """Stop and remove a single Docker container to prevent resource leaks.

    Called from the finally block of the polling thread for both sub-containers
    (FR-14). Failures are logged but not re-raised.

    Args:
        container_id: Docker container ID to stop and remove.
        role: Human-readable name for logging ('interactive-ai' or 'hmisurveys').
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
            "Container cleaned up: role=%s id=%s session=%s",
            role, container_id[:12], session_id,
        )
    except Exception as exc:
        logger.warning(
            "Container cleanup failed (non-fatal): role=%s id=%s session=%s error=%s",
            role, container_id[:12] if container_id else "unknown", session_id, exc,
        )


# ---------------------------------------------------------------------------
# Background polling thread
# ---------------------------------------------------------------------------

def _run_session_polling_thread(
    session_id: str,
    ia_container_id: str,
    hmisurveys_container_id: str,
    container_session_dir: str,
    session_timeout_seconds: int,
) -> None:
    """Background thread that monitors a session and drives phase transitions.

    Waits for BOTH result files to appear in the shared directory (FR-10):
      - kpis.json            written by InteractiveAI on episode end (FR-08)
      - survey_outcomes.json written by hmisurveys on survey submit  (FR-09)

    The session only transitions to COMPLETED when both files are present.
    Either file alone leaves the session in progress. On timeout, transitions
    to FAILED regardless of which files have or have not appeared (FR-11).
    Cleans up both containers on exit regardless of outcome (FR-14).

    Args:
        session_id: Session to monitor.
        ia_container_id: Docker container ID for InteractiveAI (for cleanup).
        hmisurveys_container_id: Docker container ID for hmisurveys (for cleanup).
        container_session_dir: Path inside this container to the per-session
                               results directory.
        session_timeout_seconds: Seconds after which a non-completed session
                                 is transitioned to FAILED.
    """
    session_manager = get_session_manager()
    kpis_path = Path(container_session_dir) / KPIS_FILENAME
    survey_path = Path(container_session_dir) / SURVEY_OUTCOMES_FILENAME
    deadline = time.monotonic() + session_timeout_seconds

    logger.info(
        "Session polling started: session=%s timeout=%ds kpis_path=%s survey_path=%s",
        session_id, session_timeout_seconds, kpis_path, survey_path,
    )

    try:
        while True:
            current_time = time.monotonic()

            # Timeout check comes first so a simultaneous file + timeout is safe (FR-11).
            if current_time >= deadline:
                kpis_present = kpis_path.exists()
                survey_present = survey_path.exists()
                missing = [
                    name for name, present in [
                        (KPIS_FILENAME, kpis_present),
                        (SURVEY_OUTCOMES_FILENAME, survey_present),
                    ]
                    if not present
                ]
                logger.warning(
                    "Session timed out: session=%s timeout=%ds missing_files=%s",
                    session_id, session_timeout_seconds, missing,
                )
                session_manager.advance_phase(
                    session_id,
                    SessionPhase.FAILED,
                    error_message=(
                        f"Session timed out after {session_timeout_seconds} seconds. "
                        f"Missing result files: {missing}"
                    ),
                )
                break

            kpis_present = kpis_path.exists()
            survey_present = survey_path.exists()

            if kpis_present and survey_present:
                # Both files present — parse each independently and complete (FR-10).
                logger.info(
                    "Both result files detected: session=%s", session_id
                )
                try:
                    kpis = _parse_results_file(kpis_path)
                    survey_outcomes = _parse_results_file(survey_path)

                    session_manager.advance_phase(
                        session_id,
                        SessionPhase.COMPLETED,
                        kpis=kpis,
                        survey_outcomes=survey_outcomes,
                        session_metadata={
                            "kpis_file": str(kpis_path),
                            "survey_file": str(survey_path),
                        },
                    )
                    logger.info(
                        "Session completed: session=%s kpi_count=%d survey_count=%d",
                        session_id, len(kpis), len(survey_outcomes),
                    )
                    break

                except ValueError as exc:
                    logger.error(
                        "Failed to parse result files: session=%s error=%s",
                        session_id, exc,
                    )
                    session_manager.advance_phase(
                        session_id,
                        SessionPhase.FAILED,
                        error_message=f"Result file parse error: {exc}",
                    )
                    break

            # Log partial completion at most once per file to avoid log spam.
            if kpis_present and not survey_present:
                logger.debug(
                    "KPIs ready, awaiting survey outcomes: session=%s", session_id
                )
            elif survey_present and not kpis_present:
                logger.debug(
                    "Survey outcomes ready, awaiting KPIs: session=%s", session_id
                )

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
            pass  # Session may already be terminal; secondary errors are ignored.

    finally:
        # Always clean up both containers when the polling thread exits (FR-14).
        _stop_and_remove_container(ia_container_id, "interactive-ai", session_id)
        _stop_and_remove_container(
            hmisurveys_container_id, "hmisurveys", session_id
        )


# ---------------------------------------------------------------------------
# gRPC servicer
# ---------------------------------------------------------------------------

class HumanAIInteractionTestingServicer(
    hai_pb2_grpc.HumanAIInteractionTestingServiceServicer
):
    """gRPC servicer implementing the three Human-AI Interaction Testing RPCs.

    Spec coverage: FR-01, FR-02, FR-03, FR-07
    """

    def StartHumanAISession(self, request, context):
        """Launch a new human-AI interaction testing session.

        Launches both the InteractiveAI and hmisurveys Docker containers,
        starts the background polling thread, and returns session_id,
        gui_url (InteractiveAI), and survey_url (hmisurveys) immediately.

        Args:
            request: HumanAISessionSpec protobuf message.
            context: gRPC server context for setting status codes.

        Returns:
            StartSessionResponse with session_id, gui_url, and survey_url.
        """
        session_manager = get_session_manager()

        if request.session_timeout_seconds <= 0:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("session_timeout_seconds must be > 0")
            return hai_pb2.StartSessionResponse(
                success=False,
                message="session_timeout_seconds must be > 0",
            )

        if session_manager.has_active_session():
            context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
            context.set_details(
                "A session is already active. "
                "Only one session at a time is supported in v1."
            )
            return hai_pb2.StartSessionResponse(
                success=False,
                message="Another session is currently active.",
            )

        session_id = uuid.uuid4().hex
        logger.info("Starting new session: session=%s", session_id)

        try:
            session_manager.create(session_id)

            host_results_path, container_results_path = (
                _create_session_results_directory(session_id)
            )

            ia_container_id, hmisurveys_container_id, gui_url, survey_url = (
                _launch_session_containers(session_id, host_results_path, request)
            )

            session_manager.advance_phase(
                session_id,
                SessionPhase.GUI_READY,
                gui_url=gui_url,
                survey_url=survey_url,
                container_id=ia_container_id,
                survey_container_id=hmisurveys_container_id,
                volume_name=session_id,
            )

            polling_thread = threading.Thread(
                target=_run_session_polling_thread,
                args=(
                    session_id,
                    ia_container_id,
                    hmisurveys_container_id,
                    container_results_path,
                    int(request.session_timeout_seconds),
                ),
                daemon=True,
                name=f"hai-poll-{session_id[:8]}",
            )
            polling_thread.start()

            logger.info(
                "Session started: session=%s gui_url=%s survey_url=%s timeout=%ds",
                session_id, gui_url, survey_url, request.session_timeout_seconds,
            )

            return hai_pb2.StartSessionResponse(
                success=True,
                message=(
                    "Session started. Share gui_url with the operator for the grid "
                    "simulation, and survey_url for the questionnaire after the episode ends."
                ),
                session_id=session_id,
                gui_url=gui_url,
                survey_url=survey_url,
            )

        except Exception as exc:
            logger.exception("Failed to start session: session=%s", session_id)
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

        response = hai_pb2.SessionResultResponse(
            success=True,
            message="Session results available.",
            session_id=request.session_id,
        )

        for key, value in state.kpis.items():
            response.kpis[str(key)].CopyFrom(_metric_value_from_any(value))

        for key, value in state.survey_outcomes.items():
            response.survey_outcomes[str(key)].CopyFrom(
                _metric_value_from_any(value)
            )

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
    logger.info(
        "Human-AI Interaction Testing gRPC server started on port %s", grpc_port
    )
    return server


# ---------------------------------------------------------------------------
# HTTP control plane handler (for WP3 orchestrator integration)
# ---------------------------------------------------------------------------

def _execute_start_session(request) -> Any:
    """HTTP control plane handler for 'StartHumanAISession'.

    Called by the WP3 orchestrator via POST /control/execute. Launches both
    containers asynchronously and returns immediately so the orchestrator can
    poll GET /control/status/{task_id} until complete.

    Args:
        request: ExecuteRequest from the FastAPI control router.

    Returns:
        ExecuteResponse with status 'pending' and a gRPC DataReference.
    """
    import base64
    from .control_interface import DataReference, ExecuteResponse

    try:
        if not request.inputs:
            return ExecuteResponse(
                status="failed",
                error="No input provided. Expected inline JSON with session spec.",
                task_id=request.task_id,
            )

        raw_input = request.inputs[0]
        protocol = str(raw_input.get("protocol", "")).lower()

        if protocol != "inline":
            return ExecuteResponse(
                status="failed",
                error=f"Unsupported input protocol: {protocol}. Use 'inline'.",
                task_id=request.task_id,
            )

        encoded = raw_input.get("uri", "")
        spec_dict = json.loads(base64.b64decode(encoded).decode("utf-8"))

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

        session_id = request.task_id

        class _SpecProxy:
            """Duck-type proxy so _launch_session_containers can read spec fields
            without requiring a compiled proto message at HTTP-handler import time."""

            def __init__(self, source_dict: dict) -> None:
                scenario = source_dict.get("scenario", {})
                agent = source_dict.get("agent", {})
                survey = source_dict.get("survey", {})

                class _Sub:
                    def __init__(self, name: str = "", survey_id: str = "") -> None:
                        self.name = name
                        self.survey_id = survey_id

                self.scenario = _Sub(name=scenario.get("name", ""))
                self.agent = _Sub(name=agent.get("name", ""))
                self.survey = _Sub(survey_id=survey.get("survey_id", ""))
                self.kpis = source_dict.get("kpis", [])
                self.session_timeout_seconds = source_dict.get(
                    "session_timeout_seconds", 0
                )

        spec_proxy = _SpecProxy(spec_dict)
        session_manager.create(session_id)

        host_results_path, container_results_path = (
            _create_session_results_directory(session_id)
        )

        ia_container_id, hmisurveys_container_id, gui_url, survey_url = (
            _launch_session_containers(session_id, host_results_path, spec_proxy)
        )

        session_manager.advance_phase(
            session_id,
            SessionPhase.GUI_READY,
            gui_url=gui_url,
            survey_url=survey_url,
            container_id=ia_container_id,
            survey_container_id=hmisurveys_container_id,
            volume_name=session_id,
        )

        polling_thread = threading.Thread(
            target=_run_session_polling_thread,
            args=(
                session_id,
                ia_container_id,
                hmisurveys_container_id,
                container_results_path,
                timeout_seconds,
            ),
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


# ---------------------------------------------------------------------------
# POST /collect/session-trace handler (FR-12)
# ---------------------------------------------------------------------------

def _handle_collect_session_trace(body: dict) -> tuple[dict, int]:
    """Write kpis.json to the active session's results directory (FR-12, FR-08).

    Called when the InteractiveAI frontend POSTs the historic-session JSON to
    POST /collect/session-trace on operator logout (FR-13). The body is the full
    session trace produced by traceSessionExport.ts.

    Session ID resolution (OQ-1):
      - If the body contains a 'wp3_session_id' key (added by FR-20 once OQ-1
        is resolved), that session is used directly.
      - Fallback: the single non-terminal session is used (v1 single-session
        constraint means exactly one active session exists at a time).

    Args:
        body: Parsed JSON body from the POST request — the full InteractiveAI
              historic-session dict (sessionId, userLogin, startedAt, kpis, traces).

    Returns:
        A tuple (response_dict, http_status_code) — 200 on success, 404 if no
        active session is found, 500 on filesystem write failure.
    """
    session_manager = get_session_manager()

    # TODO (OQ-1): When FR-20 is implemented, the frontend includes 'wp3_session_id'
    # in the POST body (read from the ?session_id= query param on gui_url). Until then,
    # fall back to the single active session.
    wp3_session_id: str | None = body.get("wp3_session_id")

    if wp3_session_id:
        target_session_id = wp3_session_id
        state = session_manager.get(target_session_id)
        if state is None:
            logger.warning(
                "POST /collect/session-trace: session not found: %s", target_session_id
            )
            return {"error": f"Session not found: {target_session_id}"}, 404
    else:
        # v1 fallback: look up the single non-terminal session.
        target_session_id = session_manager.get_active_session_id()
        if target_session_id is None:
            logger.warning(
                "POST /collect/session-trace: no active session found "
                "(body had no wp3_session_id)"
            )
            return {"error": "No active session found"}, 404

    container_session_dir = Path(RESULTS_CONTAINER_BASE_PATH) / target_session_id
    kpis_output_path = container_session_dir / KPIS_FILENAME

    try:
        kpis_output_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
        logger.info(
            "kpis.json written: session=%s path=%s keys=%d",
            target_session_id,
            kpis_output_path,
            len(body),
        )
        return {"status": "saved", "session_id": target_session_id}, 200

    except OSError as exc:
        logger.exception(
            "Failed to write kpis.json: session=%s path=%s",
            target_session_id,
            kpis_output_path,
        )
        return {"error": f"Failed to write kpis.json: {exc}"}, 500


# Exported for registration in control_interface.create_app() via common/__init__.py.
collect_session_trace = _handle_collect_session_trace
