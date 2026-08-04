"""Running long computations as background jobs with fetchable results.

A benchmark run or a grid synthesis takes minutes. Executing it inline holds the
orchestrator's HTTP request open for its whole duration, which ties up a worker,
gives the caller no progress, and turns any network hiccup into a lost result.

This module runs the work in a background thread instead: ``/control/execute``
returns a task id immediately, ``/control/status`` reports progress, and the
finished artifact is stored where ``/control/data`` can serve it. The
DataReference the caller finally receives points at that URL — something they
can fetch — rather than at an internal endpoint only a peer service could
resolve.

Concurrency is capped. These jobs are CPU-heavy, and an uncapped service will
happily accept a fourth benchmark while three are running and starve the live
human-AI sessions sharing the machine.

Spec coverage: FR-24, FR-25, FR-26
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

from .artifacts import FileArtifactStore
from .concurrent import DataReference, ExecuteRequest, ExecuteResponse, task_manager

logger = logging.getLogger(__name__)

# Default ceiling on simultaneous jobs. One means a service runs a single
# computation at a time, which is the right default for work that saturates the
# cores it is given.
DEFAULT_MAX_CONCURRENT_JOBS = 1

# Status strings the orchestrator understands.
STATUS_RUNNING = "running"
STATUS_FAILED = "failed"


class BatchJobRunner:
    """Accepts long computations, runs them in the background, stores results.

    Example:
        >>> runner = BatchJobRunner(FileArtifactStore("/artifacts"), self_url="http://svc:8080")
        >>> runner.submit(request, lambda report: {"score": 1.0}, "BenchmarkResult")
        ExecuteResponse(status='running', ...)
    """

    def __init__(
        self,
        artifact_store: FileArtifactStore,
        self_url: str,
        max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS,
    ) -> None:
        """
        Args:
            artifact_store: Where finished results are written.
            self_url: Base URL the fetching party reaches this service at, used
                to build the result reference.
            max_concurrent_jobs: How many jobs may run at once. Further
                submissions are refused rather than queued, so the caller
                learns immediately instead of waiting behind work it cannot see.

        Raises:
            ValueError: If max_concurrent_jobs is less than 1.
        """
        if max_concurrent_jobs < 1:
            raise ValueError(f"max_concurrent_jobs must be at least 1, got {max_concurrent_jobs}")

        self._artifacts = artifact_store
        self._self_url = self_url
        self._capacity = threading.Semaphore(max_concurrent_jobs)
        self._max_concurrent_jobs = max_concurrent_jobs

    @property
    def artifact_store(self) -> FileArtifactStore:
        """The store finished results are written to.

        Exposed so the service can hand the same store to ``create_app``, which
        serves those results at ``/control/data``. One store, one directory.
        """
        return self._artifacts

    def submit(
        self,
        request: ExecuteRequest,
        work: Callable[[Callable[[int], None]], Any],
        data_format: str,
    ) -> ExecuteResponse:
        """
        Accept a job and start it in the background.

        Args:
            request: The orchestrator's execute request; its ``task_id``
                identifies the job.
            work: The computation. Receives a ``report_progress(percent)``
                callable and returns a JSON-serialisable result.
            data_format: Logical format recorded on the stored artifact and
                carried in the DataReference.

        Returns:
            An ExecuteResponse with status ``running`` and the task id, or
            ``failed`` when the service is already at capacity.
        """
        if not self._capacity.acquire(blocking=False):
            logger.warning(
                "Refusing task %s: already running %d job(s)",
                request.task_id,
                self._max_concurrent_jobs,
            )
            return ExecuteResponse(
                status=STATUS_FAILED,
                task_id=request.task_id,
                error=(
                    f"Service is at capacity ({self._max_concurrent_jobs} concurrent job(s)). "
                    "Retry once the running job completes."
                ),
            )

        task_manager.register_task(request.task_id, request)

        worker_thread = threading.Thread(
            target=self._run_job,
            args=(request.task_id, work, data_format),
            daemon=True,
            name=f"job-{request.task_id[:8]}",
        )
        worker_thread.start()

        return ExecuteResponse(status=STATUS_RUNNING, task_id=request.task_id)

    def _run_job(
        self,
        task_id: str,
        work: Callable[[Callable[[int], None]], Any],
        data_format: str,
    ) -> None:
        """
        Execute one job and record its outcome.

        Runs on the worker thread. Every exit path releases the capacity
        semaphore, because a job that fails without releasing it would shrink
        the service's capacity permanently.

        Args:
            task_id: Job identifier.
            work: The computation.
            data_format: Logical format for the stored artifact.
        """
        try:
            def report_progress(percent: int) -> None:
                """Publish progress for /control/status."""
                task_manager.update_progress(task_id, percent)

            result = work(report_progress)

            self._artifacts.store_json(task_id, result, data_format=data_format)
            task_manager.complete_task(
                task_id, self._artifacts.build_reference(task_id, self._self_url)
            )
            logger.info("Task %s completed", task_id)

        except Exception as job_error:  # noqa: BLE001 - recorded on the task, not raised
            logger.exception("Task %s failed", task_id)
            task_manager.fail_task(task_id, str(job_error))

        finally:
            self._capacity.release()


def build_runner(artifact_store_path: str | None = None) -> BatchJobRunner:
    """
    Build a runner configured from the process environment.

    Args:
        artifact_store_path: Overrides ARTIFACT_DIR, for tests.

    Returns:
        A runner writing to the configured artifact directory.
    """
    return BatchJobRunner(
        FileArtifactStore(artifact_store_path or os.environ.get("ARTIFACT_DIR", "/artifacts")),
        self_url=os.environ.get("SELF_URL", "http://localhost:8080"),
        max_concurrent_jobs=int(
            os.environ.get("MAX_CONCURRENT_JOBS", DEFAULT_MAX_CONCURRENT_JOBS)
        ),
    )
