"""FastAPI HTTP control plane for the Human-AI Interaction Testing service.

Provides the /control/execute, /control/status/{task_id}, /control/output/{task_id},
and /health endpoints consumed by the WP3 orchestrator. The session state is
tracked by the SessionManager; the HTTP layer delegates to the same session
machinery used by the gRPC data plane.

Adapted from the benchmarking service's control_interface.py with minimal
changes — session status is read from SessionManager instead of TaskManager.

Spec coverage: FR-12 (orchestrator integration), NFR-04 (health endpoint)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from .session_manager import SessionPhase, get_session_manager

logger = logging.getLogger(__name__)

SELF_URL: str = os.environ.get("SELF_URL", "http://localhost:8005")


def get_data_url(task_id: str) -> str:
    """Build the URL for the /control/data/{task_id} endpoint.

    Args:
        task_id: Task or session identifier.

    Returns:
        Absolute URL string for the data endpoint.
    """
    return f"{SELF_URL}/control/data/{task_id}"


class DataReference(BaseModel):
    """Pointer to a result artifact, following the WP3 data reference schema."""

    protocol: str
    uri: str
    format: str


class ExecuteRequest(BaseModel):
    """Orchestrator request to execute a named method on this service."""

    method: str
    workflow_id: str
    task_id: str
    inputs: list[dict] = []


class ExecuteResponse(BaseModel):
    """Response to an /execute request, carrying status and optional output reference."""

    status: str
    output: DataReference | None = None
    error: str | None = None
    task_id: str | None = None


class StatusResponse(BaseModel):
    """Response to a /status/{task_id} polling request."""

    status: str
    progress: int | None = None
    error: str | None = None


class OutputResponse(BaseModel):
    """Response to an /output/{task_id} request, carrying the result reference."""

    output: DataReference | None = None


def _session_phase_to_orchestrator_status(phase: SessionPhase) -> tuple[str, int]:
    """Map a SessionPhase to the WP3 orchestrator status string and progress value.

    The orchestrator expects 'pending', 'running', 'complete', or 'failed'.

    Args:
        phase: Current session phase from SessionManager.

    Returns:
        A tuple (status_string, progress_int) for the StatusResponse.
    """
    mapping = {
        SessionPhase.PENDING:     ("pending",  0),
        SessionPhase.GUI_READY:   ("running", 20),
        SessionPhase.IN_PROGRESS: ("running", 50),
        SessionPhase.SURVEY:      ("running", 80),
        SessionPhase.COMPLETED:   ("complete", 100),
        SessionPhase.FAILED:      ("failed",    0),
    }
    return mapping.get(phase, ("pending", 0))


def create_control_router(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
) -> APIRouter:
    """Build the /control FastAPI router with execute, status, output, and data endpoints.

    Args:
        execute_handlers: Mapping of method name → handler function.

    Returns:
        Configured APIRouter to include in the FastAPI application.
    """
    router = APIRouter()

    @router.post("/execute", response_model=ExecuteResponse)
    def execute(request: ExecuteRequest) -> ExecuteResponse:
        handler = execute_handlers.get(request.method)
        if handler is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown method: {request.method}. "
                    f"Available: {list(execute_handlers.keys())}"
                ),
            )
        try:
            response = handler(request)
            response.task_id = request.task_id
            return response
        except Exception as exc:
            logger.exception("Execute failed: method=%s task=%s", request.method, request.task_id)
            return ExecuteResponse(
                status="failed",
                error=str(exc),
                task_id=request.task_id,
            )

    @router.get("/status/{task_id}", response_model=StatusResponse)
    def status(task_id: str) -> StatusResponse:
        state = get_session_manager().get(task_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {task_id}")

        orchestrator_status, progress = _session_phase_to_orchestrator_status(state.phase)
        return StatusResponse(
            status=orchestrator_status,
            progress=progress,
            error=state.error_message or None,
        )

    @router.get("/output/{task_id}", response_model=OutputResponse)
    def output(task_id: str) -> OutputResponse:
        state = get_session_manager().get(task_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {task_id}")

        if state.phase != SessionPhase.COMPLETED:
            raise HTTPException(
                status_code=400,
                detail=f"Session not yet completed (phase: {state.phase.name})",
            )

        grpc_host = os.environ.get("GRPC_HOST", "hai-testing-service")
        grpc_port = os.environ.get("GRPC_PORT", "50051")

        return OutputResponse(
            output=DataReference(
                protocol="grpc",
                uri=f"{grpc_host}:{grpc_port}",
                format="GetSessionResult",
            )
        )

    return router


def create_collect_router(
    collect_session_trace_handler: Callable[[dict], tuple[dict, int]],
) -> APIRouter:
    """Build the /collect FastAPI router for receiving session results from tools.

    Exposes POST /session-trace, called by the InteractiveAI frontend on operator
    logout to deliver the historic-session JSON to WP3 for server-side storage (FR-12).

    Args:
        collect_session_trace_handler: Function that receives the parsed request body
            and returns a (response_dict, http_status_code) tuple.

    Returns:
        Configured APIRouter to mount at the /collect prefix.
    """
    router = APIRouter()

    @router.post("/session-trace")
    async def collect_session_trace(request: Request) -> JSONResponse:
        """Receive a historic-session JSON from the InteractiveAI frontend (FR-12, FR-13).

        The InteractiveAI frontend (traceSessionExport.ts) POSTs the full session
        trace here on operator logout, alongside the existing browser download.
        WP3 writes it as kpis.json to the shared volume so the polling thread can
        detect session completion (FR-10).
        """
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return JSONResponse(
                content={"error": "Request body is not valid JSON"},
                status_code=400,
            )

        response_body, status_code = collect_session_trace_handler(body)
        return JSONResponse(content=response_body, status_code=status_code)

    return router


def create_app(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
    service_name: str = "Human-AI Interaction Testing Service",
    collect_session_trace: Callable[[dict], tuple[dict, int]] | None = None,
) -> FastAPI:
    """Build the FastAPI application with control plane, collect endpoint, and health.

    Args:
        execute_handlers: Method name → handler mapping for /control/execute.
        service_name: Title shown in the auto-generated OpenAPI docs.
        collect_session_trace: Optional handler for POST /collect/session-trace (FR-12).
            If provided, the /collect router is mounted. Receives the parsed JSON body
            and returns (response_dict, status_code).

    Returns:
        Configured FastAPI application ready to serve.
    """
    app = FastAPI(title=service_name, version="0.1.0")
    app.include_router(create_control_router(execute_handlers), prefix="/control")

    if collect_session_trace is not None:
        app.include_router(
            create_collect_router(collect_session_trace),
            prefix="/collect",
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    return app


def run(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
    service_name: str = "Human-AI Interaction Testing Service",
    collect_session_trace: Callable[[dict], tuple[dict, int]] | None = None,
) -> None:
    """Configure logging and start the uvicorn HTTP server.

    Reads HOST and PORT from environment variables (defaults: 0.0.0.0 / 8005).

    Args:
        execute_handlers: Method name → handler mapping.
        service_name: Service name for logs and OpenAPI docs.
        collect_session_trace: Optional handler for POST /collect/session-trace (FR-12).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8005"))

    logger.info("Starting %s on %s:%s", service_name, host, port)
    uvicorn.run(
        create_app(execute_handlers, service_name, collect_session_trace),
        host=host,
        port=port,
    )
