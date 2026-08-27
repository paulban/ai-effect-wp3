"""Batch execution tests for the benchmark service.

The benchmark used to run inline and return ``complete`` with a gRPC reference
to its own port. It now returns a task id immediately and stores a result the
submitting caller can fetch over HTTP.

The grid2op run itself is stubbed: these tests are about the job lifecycle, not
about grid physics.

Spec coverage: FR-24, FR-25, FR-26
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import pytest

SERVICE_DIRECTORY = Path(__file__).resolve().parents[1]
USE_CASES_DIRECTORY = SERVICE_DIRECTORY.parents[2]
for import_path in (str(SERVICE_DIRECTORY), str(USE_CASES_DIRECTORY)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

from common.artifacts import FileArtifactStore  # noqa: E402
from common.batch_jobs import BatchJobRunner, build_job_id  # noqa: E402
from common.concurrent import ExecuteRequest, task_manager  # noqa: E402

SELF_URL = "http://benchmark-runner:8080"
WORKFLOW_ID = "workflow-1"


@pytest.fixture
def artifact_store() -> FileArtifactStore:
    """An artifact store in a throwaway directory."""
    return FileArtifactStore(tempfile.mkdtemp(prefix="benchmark-artifacts-"))


@pytest.fixture
def job_runner(artifact_store: FileArtifactStore) -> BatchJobRunner:
    """A runner allowing one job at a time, matching the deployment default."""
    return BatchJobRunner(artifact_store, self_url=SELF_URL, max_concurrent_jobs=1)


def _request(task_id: str) -> ExecuteRequest:
    """Build a minimal execute request."""
    return ExecuteRequest(method="RunBenchmark", workflow_id=WORKFLOW_ID, task_id=task_id)


def _job(task_id: str) -> str:
    """The id the service reports back for a task of the standard workflow."""
    return build_job_id(WORKFLOW_ID, task_id)


def _wait_for_terminal_status(job_id: str, timeout_seconds: float = 5.0) -> dict:
    """
    Poll job status until it leaves the running state.

    Args:
        job_id: Job to poll, as reported back by the service.
        timeout_seconds: How long to wait before giving up.

    Returns:
        The final status mapping.

    Raises:
        AssertionError: If the job is still running when the timeout expires.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = task_manager.get_status(job_id)
        if status and status["status"] != "running":
            return status
        time.sleep(0.01)

    raise AssertionError(f"Job {job_id} did not finish within {timeout_seconds}s")


def test_submit_returns_immediately_with_a_task_id(job_runner):
    """FR-24: the caller is not held open for the duration of the run.

    The work here blocks briefly; the response must arrive before it finishes.
    """
    def slow_work(report_progress):
        time.sleep(0.2)
        return {"score": 1.0}

    submitted_at = time.time()
    response = job_runner.submit(_request("task-immediate"), slow_work, "BenchmarkResult")
    elapsed_seconds = time.time() - submitted_at

    assert response.status == "running"
    assert response.task_id == _job("task-immediate")
    assert elapsed_seconds < 0.15


def test_completed_job_stores_a_fetchable_reference(job_runner, artifact_store):
    """FR-25: the output reference is an HTTP URL, not a gRPC self-reference."""
    job_runner.submit(_request("task-complete"), lambda report: {"score": 0.9}, "BenchmarkResult")

    job_id = _job("task-complete")
    status = _wait_for_terminal_status(job_id)

    assert status["status"] == "complete"
    reference = task_manager.get_output(job_id)
    assert reference["protocol"] == "http"
    assert reference["uri"] == f"{SELF_URL}/control/data/{job_id}"
    assert artifact_store.load(job_id) is not None


def test_reference_format_is_one_the_orchestrator_accepts(job_runner):
    """The orchestrator validates `format` against a closed set of encodings.

    A reference naming the payload — "BenchmarkResult" — is rejected there, so
    the whole workflow fails on a benchmark that in fact ran to completion. The
    logical name belongs in metadata.
    """
    job_runner.submit(_request("task-format"), lambda report: {"score": 0.9}, "BenchmarkResult")
    _wait_for_terminal_status(_job("task-format"))

    reference = task_manager.get_output(_job("task-format"))

    assert reference["format"] == "json"
    assert reference["metadata"]["data_format"] == "BenchmarkResult"


def test_progress_is_reported_while_the_job_runs(job_runner):
    """FR-24: /control/status reflects progress rather than a binary state."""
    observed_progress = []

    def reporting_work(report_progress):
        report_progress(10)
        observed_progress.append(task_manager.get_status(_job("task-progress"))["progress"])
        report_progress(90)
        return {}

    job_runner.submit(_request("task-progress"), reporting_work, "BenchmarkResult")
    _wait_for_terminal_status(_job("task-progress"))

    assert observed_progress == [10]
    assert task_manager.get_status(_job("task-progress"))["progress"] == 100


def test_a_failing_job_is_recorded_as_failed(job_runner):
    """A job that raises must fail its task rather than disappear."""
    def failing_work(report_progress):
        raise RuntimeError("grid2op exploded")

    job_runner.submit(_request("task-failure"), failing_work, "BenchmarkResult")

    status = _wait_for_terminal_status(_job("task-failure"))
    assert status["status"] == "failed"
    assert "grid2op exploded" in status["error"]


def test_second_job_is_refused_while_one_is_running(job_runner):
    """FR-26: concurrency is capped so a benchmark cannot starve live sessions."""
    release_first_job = False

    def blocking_work(report_progress):
        while not release_first_job:
            time.sleep(0.01)
        return {}

    job_runner.submit(_request("task-first"), blocking_work, "BenchmarkResult")
    refused = job_runner.submit(_request("task-second"), lambda report: {}, "BenchmarkResult")

    assert refused.status == "failed"
    assert "capacity" in refused.error.lower()

    release_first_job = True
    _wait_for_terminal_status(_job("task-first"))


def test_capacity_is_released_after_a_failed_job(job_runner):
    """FR-26: a crashing job must not permanently consume a capacity slot."""
    job_runner.submit(
        _request("task-crash"),
        lambda report: (_ for _ in ()).throw(RuntimeError("boom")),
        "BenchmarkResult",
    )
    _wait_for_terminal_status(_job("task-crash"))

    accepted = job_runner.submit(_request("task-after"), lambda report: {}, "BenchmarkResult")

    assert accepted.status == "running"
    _wait_for_terminal_status(_job("task-after"))


def test_runner_rejects_a_nonsensical_capacity(artifact_store):
    """A zero-capacity runner would accept nothing; fail loudly at construction."""
    with pytest.raises(ValueError, match="at least 1"):
        BatchJobRunner(artifact_store, self_url=SELF_URL, max_concurrent_jobs=0)


def test_two_workflows_do_not_overwrite_each_others_results(job_runner, artifact_store):
    """The orchestrator hands both runs the same task id, hashed from the node key.

    Keyed by that alone the second run overwrites the first, and the URL handed
    to the first caller starts returning the second caller's result.
    """
    shared_task_id = "task_fd91ec69"

    for workflow_id, score in (("wf-first", 0.1), ("wf-second", 0.2)):
        request = ExecuteRequest(
            method="RunBenchmark", workflow_id=workflow_id, task_id=shared_task_id
        )
        job_runner.submit(request, lambda report, s=score: {"score": s}, "BenchmarkResult")
        _wait_for_terminal_status(build_job_id(workflow_id, shared_task_id))

    first = artifact_store.load(build_job_id("wf-first", shared_task_id))
    second = artifact_store.load(build_job_id("wf-second", shared_task_id))

    assert b"0.1" in first.content
    assert b"0.2" in second.content
