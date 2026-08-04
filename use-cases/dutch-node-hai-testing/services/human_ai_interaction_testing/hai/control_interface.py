"""HTTP control plane for the Human-AI Interaction Testing service.

This module used to be a fork of the shared control interface, and the fork had
drifted. It never checked ``SERVICE_API_KEY``, so the ``services_api_key`` the
orchestrator passes per workflow was accepted by the orchestrator and then
ignored here, leaving ``/control/*`` open to anyone who could reach the port. It
advertised a ``/control/data/{task_id}`` URL through ``get_data_url`` while
never registering that route. And it returned a gRPC DataReference pointing at
its own port, which nothing in a standalone deployment resolves.

It is now a *layer* over ``common.concurrent`` rather than a copy of it. The
shared module owns the control plane, its authentication and the artifact
endpoint. This module supplies only what is specific to human-AI sessions:

* how a SessionPhase maps onto the orchestrator's status vocabulary,
* where status and output are read from — the session store, not a TaskManager,
* the ``/collect`` router that receives results pushed by session tools.

Spec coverage: FR-03, FR-05, FR-28, FR-29
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from common.artifacts import FileArtifactStore
from common.concurrent import ExecuteRequest, ExecuteResponse
from common.concurrent import create_app as create_shared_app
from common.concurrent import run as run_shared_service

from .session_manager import SessionPhase, get_session_manager

logger = logging.getLogger(__name__)

# Base URL at which this service is reachable by whoever fetches its results.
# Behind the session proxy this is the node's public URL, so the DataReference
# returned from /control/output is fetchable by the party that submitted the
# workflow and not only from inside the Docker network.
SELF_URL: str = os.environ.get("SELF_URL", "http://hai-testing-service:8080")

# Directory backing the artifact store — a named volume in deployment, so
# results survive a container restart (FR-23).
ARTIFACT_DIRECTORY: str = os.environ.get("HAI_ARTIFACT_DIR", "/artifacts")

# Logical format name carried in the DataReference for a completed session.
SESSION_RESULT_FORMAT = "HumanAISessionResult"

# Session phases mapped to the orchestrator's four status strings and a coarse
# progress percentage. The orchestrator understands only pending, running,
# complete and failed; the finer phase model stays internal to this service.
_PHASE_TO_ORCHESTRATOR_STATUS: dict[SessionPhase, tuple[str, int]] = {
    SessionPhase.PENDING: ("pending", 0),
    SessionPhase.GUI_READY: ("running", 20),
    SessionPhase.IN_PROGRESS: ("running", 50),
    SessionPhase.SURVEY: ("running", 80),
    SessionPhase.COMPLETED: ("complete", 100),
    SessionPhase.FAILED: ("failed", 0),
}


class _HandlerModule:
    """Adapts a name-to-handler mapping to the shared dispatcher's interface.

    ``common.concurrent`` dispatches by looking up ``execute_<MethodName>`` as
    an attribute of a module. This service has always kept its handlers in a
    dictionary instead, which is the more convenient shape for registering the
    operations declared in the proto. Rather than restructure the handlers or
    fork the dispatcher, this shim exposes the dictionary as attributes.
    """

    def __init__(self, execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]]):
        """
        Wrap a handler mapping.

        Args:
            execute_handlers: Method name (as named in the proto, e.g.
                "StartHumanAISession") mapped to its handler function.
        """
        for method_name, handler in execute_handlers.items():
            setattr(self, f"execute_{method_name}", handler)

        self._method_names = sorted(execute_handlers)

    def __repr__(self) -> str:
        """Render the registered methods, which is what matters when debugging a 400."""
        return f"<handlers: {', '.join(self._method_names)}>"


def build_artifact_store() -> FileArtifactStore:
    """
    Create the artifact store this service serves session results from.

    Returns:
        Store rooted at HAI_ARTIFACT_DIR.
    """
    return FileArtifactStore(ARTIFACT_DIRECTORY)


def read_session_status(session_id: str) -> dict[str, Any] | None:
    """
    Report a session's progress in the orchestrator's vocabulary.

    Supplied to the shared control plane as its status provider, so
    ``/control/status/{id}`` reflects the session state machine rather than the
    in-memory TaskManager the shared module uses by default (FR-29).

    Args:
        session_id: Session identifier, which is also the orchestrator task id.

    Returns:
        Mapping with `status`, `progress` and `error`, or None when no such
        session exists — which the shared layer turns into a 404.
    """
    session_state = get_session_manager().get(session_id)
    if session_state is None:
        return None

    orchestrator_status, progress = _PHASE_TO_ORCHESTRATOR_STATUS.get(
        session_state.phase, ("pending", 0)
    )
    return {
        "status": orchestrator_status,
        "progress": progress,
        "error": session_state.error_message or None,
    }


def read_session_output(session_id: str) -> dict[str, Any] | None:
    """
    Return the fetchable reference to a completed session's results.

    Replaces the previous gRPC self-reference with an HTTP URL served by
    ``/control/data/{session_id}``, so the caller that submitted the workflow
    can actually retrieve the result (FR-05).

    Args:
        session_id: Session identifier.

    Returns:
        A DataReference mapping, or None when the session is unknown, has not
        completed, or completed without storing an artifact. The shared layer
        distinguishes "unknown" from "not complete" by also consulting the
        status provider.
    """
    session_state = get_session_manager().get(session_id)
    if session_state is None or session_state.phase != SessionPhase.COMPLETED:
        return None

    return build_artifact_store().build_reference(session_id, SELF_URL)


async def _dispatch_collect(
    request: Request,
    handler: Callable[[dict], tuple[dict, int]],
) -> JSONResponse:
    """
    Parse a collect request body and hand it to its handler.

    Args:
        request: Incoming request, whose body must be a JSON object.
        handler: Function receiving the parsed body and returning a
            (response_body, http_status) pair.

    Returns:
        The handler's response, or 400 when the body is not a JSON object.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - every parse failure is the same 400
        return JSONResponse(content={"error": "Request body is not valid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse(
            content={"error": "Request body must be a JSON object"}, status_code=400
        )

    response_body, status_code = handler(body)
    return JSONResponse(content=response_body, status_code=status_code)


def create_collect_router(
    collect_session_trace: Callable[[dict], tuple[dict, int]],
    collect_survey_outcome: Callable[[dict], tuple[dict, int]] | None = None,
) -> APIRouter:
    """
    Build the /collect router that receives results pushed by session tools.

    Two producers post here: the InteractiveAI frontend sends the session trace
    when the operator logs out, and the survey wrapper sends the questionnaire
    outcome when the participant submits it. Together they replace a shared host
    directory that a background thread polled for files (FR-21).

    Neither producer can hold the service API key — one of them runs in the
    participant's browser — so both are authenticated by the session token,
    which the handlers verify.

    Args:
        collect_session_trace: Receives the parsed trace body, returns a
            (response_body, http_status) pair.
        collect_survey_outcome: Same contract for the questionnaire outcome.
            Optional so a deployment without the survey wrapper still starts.

    Returns:
        Router to mount at the /collect prefix.
    """
    router = APIRouter(prefix="/collect", tags=["collect"])

    @router.post("/session-trace")
    async def receive_session_trace(request: Request) -> JSONResponse:
        """Receive the InteractiveAI session trace posted on operator logout."""
        return await _dispatch_collect(request, collect_session_trace)

    if collect_survey_outcome is not None:

        @router.post("/survey-outcome")
        async def receive_survey_outcome(request: Request) -> JSONResponse:
            """Receive the questionnaire outcome posted by the survey wrapper."""
            return await _dispatch_collect(request, collect_survey_outcome)

    return router


def _shared_app_options(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
    collect_session_trace: Callable[[dict], tuple[dict, int]],
    collect_survey_outcome: Callable[[dict], tuple[dict, int]] | None,
) -> tuple[_HandlerModule, dict[str, Any]]:
    """
    Assemble the arguments handed to the shared control plane.

    Kept in one place so ``create_app`` and ``run`` cannot drift apart — the
    previous fork had exactly that problem, with the app builder and the runner
    wiring different sets of routes.

    Args:
        execute_handlers: Method name to handler mapping.
        collect_session_trace: Handler for POST /collect/session-trace.
        collect_survey_outcome: Handler for POST /collect/survey-outcome.

    Returns:
        The handler module shim and the keyword options for create_app/run.
    """
    handler_module = _HandlerModule(execute_handlers)
    options: dict[str, Any] = {
        "extra_routers": [create_collect_router(collect_session_trace, collect_survey_outcome)],
        "status_provider": read_session_status,
        "output_provider": read_session_output,
        "artifact_store": build_artifact_store(),
    }
    return handler_module, options


def create_app(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
    collect_session_trace: Callable[[dict], tuple[dict, int]],
    collect_survey_outcome: Callable[[dict], tuple[dict, int]] | None = None,
) -> FastAPI:
    """
    Build the service application on top of the shared control plane.

    Args:
        execute_handlers: Method name to handler mapping for /control/execute.
        collect_session_trace: Handler for POST /collect/session-trace.
        collect_survey_outcome: Handler for POST /collect/survey-outcome.

    Returns:
        FastAPI application carrying the authenticated control plane, the
        collect router, the artifact endpoint and /health.
    """
    handler_module, options = _shared_app_options(
        execute_handlers, collect_session_trace, collect_survey_outcome
    )
    return create_shared_app(handler_module, **options)


def run(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
    collect_session_trace: Callable[[dict], tuple[dict, int]],
    collect_survey_outcome: Callable[[dict], tuple[dict, int]] | None = None,
) -> None:
    """
    Configure logging and serve the application with uvicorn.

    Host and port come from the shared runner's HOST and PORT variables. The
    service listens on the container port the orchestrator addresses; it is no
    longer published to the host (FR-19).

    Args:
        execute_handlers: Method name to handler mapping.
        collect_session_trace: Handler for POST /collect/session-trace.
        collect_survey_outcome: Handler for POST /collect/survey-outcome.
    """
    handler_module, options = _shared_app_options(
        execute_handlers, collect_session_trace, collect_survey_outcome
    )
    logger.info("Starting Human-AI Interaction Testing control plane")
    run_shared_service(handler_module, **options)
