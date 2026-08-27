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

# Separates the workflow id from the task id in a job id. Anything but a path
# separator will do; the artifact store rejects those.
JOB_ID_SEPARATOR = "."


def build_job_id(workflow_id: str, task_id: str) -> str:
    """
    Combine a workflow and task id into an id unique to one submission.

    The orchestrator derives its task ids by hashing the node key, so every
    workflow submitted against the same package receives the *same* task id.
    Keying artifacts by it alone means each run silently overwrites the last,
    and a result URL handed to one caller starts returning another caller's
    data. The workflow id is what distinguishes them.

    The orchestrator treats the id this service reports back as opaque — it
    polls status, output and data with whatever ``/control/execute`` returned —
    so widening it here needs no change on that side.

    Args:
        workflow_id: Workflow the task belongs to.
        task_id: Task id assigned by the orchestrator.

    Returns:
        An id unique to this submission, safe as a single path segment.
    """
    if not workflow_id:
        return task_id
    return f"{workflow_id}{JOB_ID_SEPARATOR}{task_id}"


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
            An ExecuteResponse with status ``running`` and the job id, or
            ``failed`` when the service is already at capacity. The job id is
            what the caller polls with; see ``build_job_id`` for why it is not
            simply the orchestrator's task id.
        """
        job_id = build_job_id(request.workflow_id, request.task_id)

        if not self._capacity.acquire(blocking=False):
            logger.warning(
                "Refusing job %s: already running %d job(s)",
                job_id,
                self._max_concurrent_jobs,
            )
            return ExecuteResponse(
                status=STATUS_FAILED,
                task_id=job_id,
                error=(
                    f"Service is at capacity ({self._max_concurrent_jobs} concurrent job(s)). "
                    "Retry once the running job completes."
                ),
            )

        task_manager.register_task(job_id, request)

        worker_thread = threading.Thread(
            target=self._run_job,
            args=(job_id, work, data_format),
            daemon=True,
            name=f"job-{job_id[-8:]}",
        )
        worker_thread.start()

        return ExecuteResponse(status=STATUS_RUNNING, task_id=job_id)

    def _run_job(
        self,
        job_id: str,
        work: Callable[[Callable[[int], None]], Any],
        data_format: str,
    ) -> None:
        """
        Execute one job and record its outcome.

        Runs on the worker thread. Every exit path releases the capacity
        semaphore, because a job that fails without releasing it would shrink
        the service's capacity permanently.

        Args:
            job_id: Identifier the caller polls and fetches the artifact with.
            work: The computation.
            data_format: Logical format for the stored artifact.
        """
        try:
            def report_progress(percent: int) -> None:
                """Publish progress for /control/status."""
                task_manager.update_progress(job_id, percent)

            result = work(report_progress)

            self._artifacts.store_json(job_id, result, data_format=data_format)
            task_manager.complete_task(
                job_id, self._artifacts.build_reference(job_id, self._self_url)
            )
            logger.info("Job %s completed", job_id)

        except Exception as job_error:  # noqa: BLE001 - recorded on the job, not raised
            logger.exception("Job %s failed", job_id)
            task_manager.fail_task(job_id, str(job_error))

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
