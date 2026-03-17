"""Thread-safe task state manager for AI-Effect benchmark adapter."""

from __future__ import annotations

import threading
from typing import Any


class TaskManager:
    """Thread-safe task state manager."""

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
        with self._lock:
            self._tasks[task_id] = {
                "status": status,
                "output": output,
                "error": error,
                "progress": progress,
            }

    def update_progress(self, task_id: str, progress: int) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["progress"] = min(max(progress, 0), 100)

    def complete(self, task_id: str, output: Any) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = "complete"
                self._tasks[task_id]["progress"] = 100
                self._tasks[task_id]["output"] = output

    def fail(self, task_id: str, error: str) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = "failed"
                self._tasks[task_id]["error"] = error

    def get_status(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task is not None else None

    def get_output(self, task_id: str) -> Any | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task["status"] == "complete":
                return task["output"]
            return None

    def store_data(self, task_id: str, data: str | bytes, data_format: str) -> None:
        with self._lock:
            self._data[task_id] = (data, data_format)

    def get_data(self, task_id: str) -> tuple[str | bytes, str] | None:
        with self._lock:
            return self._data.get(task_id)


# Global instance for app process
_task_manager = TaskManager()


def get_task_manager() -> TaskManager:
    return _task_manager
