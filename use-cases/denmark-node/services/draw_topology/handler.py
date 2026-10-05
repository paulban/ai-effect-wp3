"""Concurrent handler — re-exported from the shared common library.

Do not modify. Build context must include common/ (see docker-compose.yml).
"""

from common.concurrent import (  # noqa: F401
    DataReference,
    ExecuteRequest,
    ExecuteResponse,
    TaskManager,
    create_app,
    run,
    run_in_background,
    task_manager,
)
