"""AI-Effect control interface for Grid2Op benchmark service."""

from __future__ import annotations

import logging
import os
from typing import Callable

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .task_manager import get_task_manager

logger = logging.getLogger(__name__)
SELF_URL = os.environ.get("SELF_URL", "http://localhost:8080")


def get_data_url(task_id: str) -> str:
    return f"{SELF_URL}/control/data/{task_id}"


class DataReference(BaseModel):
    protocol: str
    uri: str
    format: str


class ExecuteRequest(BaseModel):
    method: str
    workflow_id: str
    task_id: str
    inputs: list[dict] = []


class ExecuteResponse(BaseModel):
    status: str
    output: DataReference | None = None
    error: str | None = None
    task_id: str | None = None


class StatusResponse(BaseModel):
    status: str
    progress: int | None = None
    error: str | None = None


class OutputResponse(BaseModel):
    output: DataReference | None = None


def _get_content_type(data_format: str) -> str:
    content_types = {
        "csv": "text/csv",
        "json": "application/json",
        "text": "text/plain",
        "png": "image/png",
    }
    return content_types.get(data_format, "application/octet-stream")


def create_control_router(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
) -> APIRouter:
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
            get_task_manager().register(
                request.task_id,
                status=response.status,
                output=response.output,
                error=response.error,
                progress=100 if response.status == "complete" else 0,
            )
            response.task_id = request.task_id
            return response
        except Exception as exc:
            logger.exception("Benchmark execute failed")
            get_task_manager().register(
                request.task_id, status="failed", error=str(exc)
            )
            return ExecuteResponse(
                status="failed", error=str(exc), task_id=request.task_id
            )

    @router.get("/status/{task_id}", response_model=StatusResponse)
    def status(task_id: str) -> StatusResponse:
        task = get_task_manager().get_status(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

        return StatusResponse(
            status=task["status"],
            progress=task.get("progress"),
            error=task.get("error"),
        )

    @router.get("/output/{task_id}", response_model=OutputResponse)
    def output(task_id: str) -> OutputResponse:
        task = get_task_manager().get_status(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

        output_ref = get_task_manager().get_output(task_id)
        if output_ref is None:
            raise HTTPException(
                status_code=400,
                detail=f"Task not complete: {task['status']}",
            )
        return OutputResponse(output=output_ref)

    @router.get("/data/{task_id}")
    def get_data(task_id: str) -> Response:
        result = get_task_manager().get_data(task_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Data not found: {task_id}")

        data, data_format = result
        if isinstance(data, str):
            data = data.encode("utf-8")
        return Response(content=data, media_type=_get_content_type(data_format))

    return router


def create_app(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
    service_name: str = "Grid2Op Benchmark Service",
) -> FastAPI:
    app = FastAPI(title=service_name, version="0.1.0")
    app.include_router(create_control_router(execute_handlers), prefix="/control")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    return app


def run(
    execute_handlers: dict[str, Callable[[ExecuteRequest], ExecuteResponse]],
    service_name: str = "Grid2Op Benchmark Service",
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    logger.info("Starting %s on %s:%s", service_name, host, port)
    uvicorn.run(create_app(execute_handlers, service_name), host=host, port=port)
