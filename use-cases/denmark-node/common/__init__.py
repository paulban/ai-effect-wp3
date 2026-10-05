"""Shared control interface handler library for AI-Effect services.

Sequential handler: for services that complete immediately.
Concurrent handler: for services with long-running background tasks.
"""

from .sequential import (
    DataReference,
    ExecuteRequest,
    ExecuteResponse,
    create_app as create_sequential_app,
    run as run_sequential,
)

from .concurrent import (
    ExecuteResponse as AsyncExecuteResponse,
    StatusResponse,
    OutputResponse,
    TaskManager,
    task_manager,
    run_in_background,
    create_app as create_concurrent_app,
    run as run_concurrent,
)
