"""Thread-safe task state manager for AI-Effect control interface.

NOTE: Uses `from __future__ import annotations` for Python 3.9 compatibility.

This module provides a TaskManager class for tracking async task state and data storage.
Used by both integrated and sidecar adapter approaches.

Usage:
    from common import task_manager, get_task_manager

    # Register a new task
    task_manager.register(task_id, status="running")

    # Update progress
    task_manager.update_progress(task_id, 50)

    # Complete or fail
    task_manager.complete(task_id, output)
    task_manager.fail(task_id, "Error message")

    # Query state
    status = task_manager.get_status(task_id)
    output = task_manager.get_output(task_id)

    # Store and retrieve raw data for HTTP serving
    task_manager.store_data(task_id, csv_data, "csv")
    data, format = task_manager.get_data(task_id)
"""

from __future__ import annotations

import threading
from typing import Any


class TaskManager:
    """Thread-safe task state manager.

    Provides methods to register, update, and query task state.
    Also stores raw data for HTTP URL reference serving.
    Uses threading.Lock for safe concurrent access from background threads.
    """

    def __init__(self):
        self._tasks: dict[str, dict[str, Any]] = {}
        self._data: dict[str, tuple[str | bytes, str]] = {}
        self._lock = threading.Lock()

    def register(
        self,
        task_id: str,
        status: str,
        output: Any = None,
        error: str | None = None,
        progress: int = 0,
    ) -> None:
        """Register a new task."""
        with self._lock:
            self._tasks[task_id] = {
                "status": status,
                "output": output,
                "error": error,
                "progress": progress,
            }

    def update_progress(self, task_id: str, progress: int) -> None:
        """Update task progress (0-100)."""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["progress"] = min(max(progress, 0), 100)

    def complete(self, task_id: str, output: Any) -> None:
        """Mark task as complete with output."""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = "complete"
                self._tasks[task_id]["progress"] = 100
                self._tasks[task_id]["output"] = output

    def fail(self, task_id: str, error: str) -> None:
        """Mark task as failed with error."""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = "failed"
                self._tasks[task_id]["error"] = error

    def get_status(self, task_id: str) -> dict | None:
        """Get task status dict or None if not found."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return dict(task)

    def get_output(self, task_id: str) -> Any | None:
        """Get task output if complete, else None."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task["status"] == "complete":
                return task["output"]
            return None

    def store_data(self, task_id: str, data: str | bytes, data_format: str) -> None:
        """Store raw data for HTTP serving."""
        with self._lock:
            self._data[task_id] = (data, data_format)

    def get_data(self, task_id: str) -> tuple[str | bytes, str] | None:
        """Get stored data and format, or None if not found."""
        with self._lock:
            return self._data.get(task_id)


# Global instance
task_manager = TaskManager()


def get_task_manager() -> TaskManager:
    """Get the global task manager instance."""
    return task_manager
