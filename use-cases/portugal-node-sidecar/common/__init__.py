"""Common modules for TEF integration adapters.

This package provides shared components for both integrated and sidecar adapter approaches.
"""

from .task_manager import TaskManager, task_manager, get_task_manager
from .control_interface import (
    DataReference,
    ExecuteRequest,
    ExecuteResponse,
    StatusResponse,
    OutputResponse,
    create_control_router,
    create_app,
    run,
    get_data_url,
)
from .tef_operations import (
    fetch_http_data,
    execute_ExecuteQuery,
    execute_GetSchema,
    execute_ApplyFunction,
    execute_TrainModel,
    execute_GenerateData,
    data_provision_handlers,
    knowledge_store_handlers,
    synthetic_data_handlers,
)

__all__ = [
    # Task manager
    "TaskManager",
    "task_manager",
    "get_task_manager",
    # Control interface
    "DataReference",
    "ExecuteRequest",
    "ExecuteResponse",
    "StatusResponse",
    "OutputResponse",
    "create_control_router",
    "create_app",
    "run",
    "get_data_url",
    # TEF operations
    "fetch_http_data",
    "execute_ExecuteQuery",
    "execute_GetSchema",
    "execute_ApplyFunction",
    "execute_TrainModel",
    "execute_GenerateData",
    "data_provision_handlers",
    "knowledge_store_handlers",
    "synthetic_data_handlers",
]
