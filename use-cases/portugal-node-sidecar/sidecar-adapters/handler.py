"""AI-Effect Control Interface Handler.

Re-exports from common module for backward compatibility.
"""

import sys
from pathlib import Path

# Add parent directory for common module import
sys.path.insert(0, str(Path(__file__).parent.parent))

from common import (
    TaskManager,
    task_manager,
    get_task_manager,
    DataReference,
    ExecuteRequest,
    ExecuteResponse,
    StatusResponse,
    OutputResponse,
    create_app,
    run,
    get_data_url,
)

__all__ = [
    "TaskManager",
    "task_manager",
    "get_task_manager",
    "DataReference",
    "ExecuteRequest",
    "ExecuteResponse",
    "StatusResponse",
    "OutputResponse",
    "create_app",
    "run",
    "get_data_url",
]
