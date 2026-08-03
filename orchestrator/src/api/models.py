"""Request and response models for REST API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class WorkflowSubmitRequest(BaseModel):
    """Request to submit a workflow."""

    model_config = ConfigDict(extra="forbid")

    blueprint: dict
    dockerinfo: dict
    inputs: list[dict] = []  # Initial DataReference objects for start nodes
    services_api_key: str | None = None  # Optional bearer token sent to services

    @field_validator("blueprint")
    @classmethod
    def blueprint_not_empty(cls, v: dict) -> dict:
        if not v:
            raise ValueError("blueprint is required")
        return v

    @field_validator("dockerinfo")
    @classmethod
    def dockerinfo_not_empty(cls, v: dict) -> dict:
        if not v:
            raise ValueError("dockerinfo is required")
        return v


class WorkflowSubmitResponse(BaseModel):
    """Response from workflow submission."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    status: str


class WorkflowStatusResponse(BaseModel):
    """Response for workflow status."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    error: str | None = None


class DataReferenceResponse(BaseModel):
    """Slim representation of a DataReference for API responses."""

    model_config = ConfigDict(frozen=True)

    protocol: str
    uri: str
    format: str
    metadata: dict[str, Any] = {}


class TaskStatusResponse(BaseModel):
    """Response for task status."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    node_key: str
    status: str
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    input_refs: list[DataReferenceResponse] = []
    output_refs: list[DataReferenceResponse] = []


class TaskListResponse(BaseModel):
    """Response for task list."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    tasks: list[TaskStatusResponse]


class ErrorResponse(BaseModel):
    """Error response."""

    model_config = ConfigDict(frozen=True)

    detail: str


class HealthResponse(BaseModel):
    """Health check response."""

    model_config = ConfigDict(frozen=True)

    status: str
