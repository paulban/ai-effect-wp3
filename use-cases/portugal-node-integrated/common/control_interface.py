"""AI-Effect Control Interface for TEF adapters.

This module provides the FastAPI components for the AI-Effect control interface.
Supports both integrated (router) and sidecar (standalone app) deployment approaches.

NOTE: Uses `from __future__ import annotations` for Python 3.9 compatibility.

Usage (Integrated - add router to existing FastAPI app):
    from common import create_control_router

    app.include_router(create_control_router(execute_handlers))

Usage (Sidecar - standalone service):
    from common import run

    if __name__ == "__main__":
        run(execute_handlers, "Service Name")
"""

from __future__ import annotations

import logging
import os
from typing import Callable

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Response
from pydantic import BaseModel

from .task_manager import task_manager

logger = logging.getLogger(__name__)

# Self URL for generating HTTP data references
SELF_URL = os.environ.get("SELF_URL", "http://localhost:8080")


def get_data_url(task_id: str) -> str:
    """Get the HTTP URL for serving task data."""
    return f"{SELF_URL}/control/data/{task_id}"


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


class ExecuteResponse(BaseModel):
    """Execute response to orchestrator."""
    status: str
    output: DataReference | None = None
    error: str | None = None
    task_id: str | None = None


class StatusResponse(BaseModel):
    """Status response for async tasks."""
    status: str
    progress: int | None = None
    error: str | None = None


class OutputResponse(BaseModel):
    """Output response for async tasks."""
    output: DataReference | None = None


def _get_content_type(data_format: str) -> str:
    """Get content type for data format."""
    content_types = {
        "csv": "text/csv",
        "json": "application/json",
        "text": "text/plain",
    }
    return content_types.get(data_format, "application/octet-stream")


def create_control_router(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]]
) -> APIRouter:
    """Create a control router with the given execute handlers.

    Use this for the integrated approach - add router to existing FastAPI app.

    Args:
        execute_handlers: Dict mapping method names to handler functions.
            Each handler receives ExecuteRequest and returns ExecuteResponse.

    Returns:
        FastAPI APIRouter with /control/* endpoints.
    """
    router = APIRouter()

    @router.post("/execute", response_model=ExecuteResponse)
    def execute(request: ExecuteRequest) -> ExecuteResponse:
        logger.info(f"Execute: method={request.method}, task={request.task_id}")

        handler = execute_handlers.get(request.method)
        if handler is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown method: {request.method}"
            )

        try:
            response = handler(request)
            task_manager.register(
                request.task_id,
                status=response.status,
                output=response.output,
                error=response.error,
                progress=100 if response.status == "complete" else 0,
            )
            response.task_id = request.task_id
            return response
        except Exception as e:
            logger.error(f"Execute failed: {e}")
            task_manager.register(request.task_id, status="failed", error=str(e))
            return ExecuteResponse(status="failed", error=str(e), task_id=request.task_id)

    @router.get("/status/{task_id}", response_model=StatusResponse)
    def status(task_id: str) -> StatusResponse:
        task = task_manager.get_status(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

        return StatusResponse(
            status=task["status"],
            progress=task.get("progress"),
            error=task.get("error"),
        )

    @router.get("/output/{task_id}", response_model=OutputResponse)
    def output(task_id: str) -> OutputResponse:
        task = task_manager.get_status(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

        output = task_manager.get_output(task_id)
        if output is None:
            raise HTTPException(
                status_code=400,
                detail=f"Task not complete: {task['status']}"
            )

        return OutputResponse(output=output)

    @router.get("/data/{task_id}")
    def get_data(task_id: str) -> Response:
        """Serve raw data for HTTP URL references."""
        result = task_manager.get_data(task_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Data not found: {task_id}")

        data, data_format = result
        content_type = _get_content_type(data_format)

        if isinstance(data, str):
            data = data.encode()

        return Response(content=data, media_type=content_type)

    return router


def create_app(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
    service_name: str = "Service"
) -> FastAPI:
    """Create FastAPI app with control endpoints.

    Use this for the sidecar approach - standalone adapter service.

    Args:
        execute_handlers: Dict mapping method names to handler functions.
        service_name: Service name for documentation.

    Returns:
        FastAPI application with control endpoints.
    """
    app = FastAPI(title=f"{service_name} Adapter", version="1.0.0")

    # Include control router with /control prefix
    router = create_control_router(execute_handlers)
    app.include_router(router, prefix="/control")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


def run(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
    service_name: str = "Service"
) -> None:
    """Run the sidecar service.

    Args:
        execute_handlers: Dict mapping method names to handler functions.
        service_name: Service name for logging.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    logger.info(f"Starting {service_name} on {host}:{port}")
    app = create_app(execute_handlers, service_name)
    uvicorn.run(app, host=host, port=port)
