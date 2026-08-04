"""Orchestrator handler for concurrent (multithreaded) services.

Import this module and implement your methods in service.py.
Supports multiple simultaneous long-running tasks with progress tracking.

Note: Task state is stored in a module-level global (in-memory). This works
for single-process containers. For multi-process deployments (e.g., multiple
uvicorn workers), use external state storage like Redis instead.
"""

import logging
import os
import threading
from typing import Any, Callable, Optional

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Security
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .artifacts import FileArtifactStore

_bearer = HTTPBearer(auto_error=False)


def _check_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> None:
    api_key = os.environ.get("SERVICE_API_KEY")
    if not api_key:
        return
    if not credentials or credentials.credentials != api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

logger = logging.getLogger(__name__)


class DataReference(BaseModel):
    """Reference to data location."""

    protocol: str
    uri: str
    format: str


class ExecuteRequest(BaseModel):
    """Execute request from orchestrator."""

    method: str
    workflow_id: str
    task_id: str
    inputs: list[dict] = []
    parameters: dict = {}


class ExecuteResponse(BaseModel):
    """Execute response to orchestrator."""

    status: str
    task_id: str | None = None
    output: DataReference | None = None
    error: str | None = None


class StatusResponse(BaseModel):
    """Status response for async tasks."""

    status: str
    progress: int = 0
    error: str | None = None


class OutputResponse(BaseModel):
    """Output response for completed async tasks."""

    output: DataReference


class TaskManager:
    """Thread-safe task state manager."""

    def __init__(self):
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def register_task(self, task_id: str, request: ExecuteRequest) -> None:
        """Register a task for tracking. Uses orchestrator's task_id."""
        with self._lock:
            self._tasks[task_id] = {
                "status": "running",
                "progress": 0,
                "request": request.model_dump(),
                "output": None,
                "error": None,
            }

    def update_progress(self, task_id: str, progress: int) -> None:
        """Update task progress (0-100)."""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["progress"] = min(max(progress, 0), 100)

    def complete_task(self, task_id: str, output: dict) -> None:
        """Mark task as complete with output."""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = "complete"
                self._tasks[task_id]["progress"] = 100
                self._tasks[task_id]["output"] = output

    def fail_task(self, task_id: str, error: str) -> None:
        """Mark task as failed with error."""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = "failed"
                self._tasks[task_id]["error"] = error

    def get_status(self, task_id: str) -> dict | None:
        """Get task status."""
        with self._lock:
            if task_id not in self._tasks:
                return None
            task = self._tasks[task_id]
            return {
                "status": task["status"],
                "progress": task["progress"],
                "error": task["error"],
            }

    def get_output(self, task_id: str) -> dict | None:
        """Get task output if complete."""
        with self._lock:
            if task_id not in self._tasks:
                return None
            task = self._tasks[task_id]
            if task["status"] != "complete":
                return None
            return task["output"]


# Global task manager instance
task_manager = TaskManager()


def run_in_background(
    task_id: str,
    worker_fn: Callable[[str, ExecuteRequest, TaskManager], None],
    request: ExecuteRequest,
) -> None:
    """Run a worker function in a background thread.

    Args:
        task_id: Task identifier for progress updates.
        worker_fn: Function(task_id, request, task_manager) to run.
        request: Original execute request.
    """
    thread = threading.Thread(
        target=worker_fn,
        args=(task_id, request, task_manager),
        daemon=True,
    )
    thread.start()


def create_app(
    service_module,
    *,
    extra_routers: list[APIRouter] | None = None,
    status_provider: Callable[[str], dict | None] | None = None,
    output_provider: Callable[[str], dict | None] | None = None,
    artifact_store: FileArtifactStore | None = None,
) -> FastAPI:
    """Create FastAPI app that dispatches to service methods.

    All extension points are keyword-only with defaults, so existing callers
    that pass only a service module are unaffected.

    Args:
        service_module: Module containing execute_<MethodName> functions.
        extra_routers: Routers mounted alongside the control plane, letting a
            service add its own endpoints without forking this module. The
            Dutch node uses it for the /collect router that receives session
            results pushed by browser-side tools (FR-29).
        status_provider: Overrides where task status is read from. Services
            whose progress lives somewhere other than the in-memory
            TaskManager — a session store in Redis, for instance — pass their
            own lookup. Receives a task id, returns a mapping with `status`,
            `progress` and `error`, or None when the task is unknown.
        output_provider: Overrides where the output DataReference is read from.
            Same contract as `status_provider`, returning a DataReference
            mapping or None.
        artifact_store: Backs `/control/data/{task_id}`, which serves the bytes
            a DataReference points at. Without it, that endpoint reports that
            this service stores no artifacts (FR-25).

    Returns:
        FastAPI application.
    """
    app = FastAPI(
        title="Concurrent Service",
        description="Multithreaded service using orchestrator control interface",
        version="1.0.0",
    )

    read_status = status_provider or task_manager.get_status
    read_output = output_provider or task_manager.get_output

    @app.post("/control/execute", response_model=ExecuteResponse, dependencies=[Depends(_check_api_key)])
    def execute(request: ExecuteRequest) -> ExecuteResponse:
        """Execute a task by dispatching to service method."""
        logger.info(f"Execute: method={request.method}, task={request.task_id}")

        handler_name = f"execute_{request.method}"
        handler = getattr(service_module, handler_name, None)

        if handler is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown method: {request.method}",
            )

        try:
            return handler(request)
        except Exception as e:
            logger.error(f"Execute failed: {e}")
            return ExecuteResponse(status="failed", error=str(e))

    @app.get("/control/status/{task_id}", response_model=StatusResponse, dependencies=[Depends(_check_api_key)])
    def get_status(task_id: str) -> StatusResponse:
        """Get status of an async task."""
        status = read_status(task_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return StatusResponse(**status)

    @app.get("/control/output/{task_id}", response_model=OutputResponse, dependencies=[Depends(_check_api_key)])
    def get_output(task_id: str) -> OutputResponse:
        """Get output of a completed async task."""
        output = read_output(task_id)
        if output is None:
            status = read_status(task_id)
            if status is None:
                raise HTTPException(status_code=404, detail="Task not found")
            raise HTTPException(status_code=400, detail="Task not complete")
        return OutputResponse(output=DataReference(**output))

    @app.get("/control/data/{task_id}", dependencies=[Depends(_check_api_key)])
    def get_data(task_id: str) -> Response:
        """Serve the bytes a DataReference points at.

        This is what makes a standalone service's result retrievable: the
        DataReference returned by /control/output names this URL, and the party
        that submitted the workflow fetches it here (FR-05).
        """
        if artifact_store is None:
            raise HTTPException(
                status_code=404,
                detail="This service stores no artifacts",
            )

        try:
            artifact = artifact_store.load(task_id)
        except ValueError as invalid_task_id:
            raise HTTPException(status_code=400, detail=str(invalid_task_id))

        if artifact is None:
            raise HTTPException(status_code=404, detail="No artifact for this task")

        return Response(content=artifact.content, media_type=artifact.media_type)

    @app.get("/health")
    def health() -> dict:
        """Health check."""
        return {"status": "ok"}

    for router in extra_routers or []:
        app.include_router(router)

    return app


def run(service_module, **create_app_options) -> None:
    """Run the service.

    Args:
        service_module: Module containing execute_<MethodName> functions.
        **create_app_options: Forwarded to create_app, so a service can supply
            extra routers, status/output providers or an artifact store without
            building the app itself.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    logger.info(f"Starting concurrent service on {host}:{port}")
    app = create_app(service_module, **create_app_options)
    uvicorn.run(app, host=host, port=port)
