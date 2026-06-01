"""Common modules for AI-Effect control interface adapters.

This package provides shared components for the AI-Effect orchestrator integration.
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
from .synth_operations import (
    execute_ConfigureGrid,
    execute_SynthesizeGrid,
    start_grpc_server,
    synth_handlers,
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
    # Synth operations
    "execute_ConfigureGrid",
    "execute_SynthesizeGrid",
    "start_grpc_server",
    "synth_handlers",
]
