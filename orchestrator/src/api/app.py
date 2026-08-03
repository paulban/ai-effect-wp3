"""FastAPI REST API for orchestration platform."""

import os
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis import Redis

_bearer = HTTPBearer(auto_error=False)


def _verify_orchestrator_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> None:
    api_key = os.environ.get("ORCHESTRATOR_API_KEY")
    if not api_key:
        return
    if not credentials or credentials.credentials != api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

from api.models import (
    DataReferenceResponse,
    ErrorResponse,
    HealthResponse,
    TaskListResponse,
    TaskStatusResponse,
    WorkflowStatusResponse,
    WorkflowSubmitRequest,
    WorkflowSubmitResponse,
)
from models.data_reference import DataReference
from services.blueprint_parser import BlueprintParseError, BlueprintParser
from services.dockerinfo_parser import DockerInfoParseError, DockerInfoParser
from services.state_store import WorkflowNotFoundError, TaskNotFoundError
from services.workflow_engine import WorkflowEngine


class OrchestratorAPI:
    """REST API for orchestration platform."""

    def __init__(
        self,
        engine: WorkflowEngine,
        blueprint_parser: BlueprintParser,
        dockerinfo_parser: DockerInfoParser,
        redis_client: Redis,
    ):
        """Initialize API with dependencies."""
        if engine is None:
            raise ValueError("engine is required")
        if blueprint_parser is None:
            raise ValueError("blueprint_parser is required")
        if dockerinfo_parser is None:
            raise ValueError("dockerinfo_parser is required")
        if redis_client is None:
            raise ValueError("redis_client is required")

        self._engine = engine
        self._blueprint_parser = blueprint_parser
        self._dockerinfo_parser = dockerinfo_parser
        self._redis = redis_client

    def create_app(self) -> FastAPI:
        """Create FastAPI application."""
        app = FastAPI(
            title="Orchestrator API",
            description="REST API for AI-Effect orchestration platform",
            version="1.0.0",
        )

        @app.post(
            "/workflows",
            response_model=WorkflowSubmitResponse,
            responses={400: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def submit_workflow(request: WorkflowSubmitRequest) -> WorkflowSubmitResponse:
            """Submit a new workflow."""
            # Parse blueprint
            try:
                graph = self._blueprint_parser.parse_json(request.blueprint)
            except BlueprintParseError as e:
                raise HTTPException(status_code=400, detail=f"Invalid blueprint: {e}")

            # Parse dockerinfo
            try:
                endpoints = self._dockerinfo_parser.parse_json(request.dockerinfo)
            except DockerInfoParseError as e:
                raise HTTPException(status_code=400, detail=f"Invalid dockerinfo: {e}")

            # Generate workflow ID
            workflow_id = f"wf-{uuid.uuid4().hex[:12]}"

            # Store services API key for worker to use when calling services
            if request.services_api_key:
                self._redis.set(f"services_key:{workflow_id}", request.services_api_key)

            # Store endpoints for worker lookup
            endpoints_key = f"endpoints:{workflow_id}"
            endpoints_data = {
                name: endpoint.model_dump_json()
                for name, endpoint in endpoints.items()
            }
            if endpoints_data:
                self._redis.hset(endpoints_key, mapping=endpoints_data)

            # Parse initial inputs for start nodes
            initial_inputs: list[DataReference] | None = None
            if request.inputs:
                try:
                    initial_inputs = [DataReference(**inp) for inp in request.inputs]
                except Exception as e:
                    raise HTTPException(
                        status_code=400, detail=f"Invalid inputs: {e}"
                    )

            # Initialize and start workflow
            self._engine.initialize_workflow(workflow_id, graph)
            self._engine.start_workflow(workflow_id, initial_inputs)

            return WorkflowSubmitResponse(workflow_id=workflow_id, status="running")

        @app.get(
            "/workflows/{workflow_id}",
            response_model=WorkflowStatusResponse,
            responses={404: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def get_workflow_status(workflow_id: str) -> WorkflowStatusResponse:
            """Get workflow status."""
            try:
                state = self._engine.get_workflow_status(workflow_id)
            except WorkflowNotFoundError:
                raise HTTPException(status_code=404, detail="Workflow not found")

            return WorkflowStatusResponse(
                workflow_id=state.workflow_id,
                status=state.status.value,
                created_at=state.created_at,
                updated_at=state.updated_at,
                error=state.error,
            )

        @app.get(
            "/workflows/{workflow_id}/tasks",
            response_model=TaskListResponse,
            responses={404: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def get_workflow_tasks(workflow_id: str) -> TaskListResponse:
            """Get all tasks for a workflow."""
            # Check workflow exists
            try:
                self._engine.get_workflow_status(workflow_id)
            except WorkflowNotFoundError:
                raise HTTPException(status_code=404, detail="Workflow not found")

            # Get all tasks
            tasks = self._engine.get_all_tasks(workflow_id)
            task_responses = [
                TaskStatusResponse(
                    task_id=task.task_id,
                    node_key=task.node_key,
                    status=task.status.value,
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                    error=task.error,
                    input_refs=[
                        DataReferenceResponse(
                            protocol=r.protocol.value,
                            uri=r.uri,
                            format=r.format if isinstance(r.format, str) else r.format.value,
                            metadata=r.metadata,
                        )
                        for r in task.input_refs
                    ],
                    output_refs=[
                        DataReferenceResponse(
                            protocol=r.protocol.value,
                            uri=r.uri,
                            format=r.format if isinstance(r.format, str) else r.format.value,
                            metadata=r.metadata,
                        )
                        for r in task.output_refs
                    ],
                )
                for task in tasks
            ]

            return TaskListResponse(workflow_id=workflow_id, tasks=task_responses)

        @app.get(
            "/workflows/{workflow_id}/tasks/{task_id}",
            response_model=TaskStatusResponse,
            responses={404: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def get_task_status(workflow_id: str, task_id: str) -> TaskStatusResponse:
            """Get task status."""
            try:
                task = self._engine._state_store.get_task(workflow_id, task_id)
            except (WorkflowNotFoundError, TaskNotFoundError):
                raise HTTPException(status_code=404, detail="Task not found")

            return TaskStatusResponse(
                task_id=task.task_id,
                node_key=task.node_key,
                status=task.status.value,
                created_at=task.created_at,
                updated_at=task.updated_at,
                error=task.error,
                input_refs=[
                    DataReferenceResponse(
                        protocol=r.protocol.value,
                        uri=r.uri,
                        format=r.format if isinstance(r.format, str) else r.format.value,
                        metadata=r.metadata,
                    )
                    for r in task.input_refs
                ],
                output_refs=[
                    DataReferenceResponse(
                        protocol=r.protocol.value,
                        uri=r.uri,
                        format=r.format if isinstance(r.format, str) else r.format.value,
                        metadata=r.metadata,
                    )
                    for r in task.output_refs
                ],
            )

        @app.delete(
            "/workflows/{workflow_id}",
            responses={404: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def delete_workflow(workflow_id: str) -> dict:
            """Delete a workflow."""
            try:
                self._engine.get_workflow_status(workflow_id)
            except WorkflowNotFoundError:
                raise HTTPException(status_code=404, detail="Workflow not found")

            # Delete workflow state
            self._engine._state_store.delete_workflow(workflow_id)

            # Delete endpoints
            self._redis.delete(f"endpoints:{workflow_id}")

            # Clear queue
            self._engine._task_queue.clear_queue(workflow_id)

            return {"status": "deleted"}

        @app.get("/health", response_model=HealthResponse)
        def health_check() -> HealthResponse:
            """Health check endpoint."""
            return HealthResponse(status="ok")

        return app
