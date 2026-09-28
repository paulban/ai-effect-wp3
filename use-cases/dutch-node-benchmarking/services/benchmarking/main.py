"""Grid2Op benchmark service entrypoint.

A benchmark run is minutes of CPU-bound work, so the service accepts a job and
returns a task id rather than holding the request open. The result is served as an artifact over HTTP, which the party that
submitted the workflow can fetch.
"""

from common.batch_jobs import build_runner
from common.concurrent import run

from benchmark import benchmark_operations

if __name__ == "__main__":
    job_runner = build_runner()
    benchmark_operations.set_job_runner(job_runner)
    run(benchmark_operations, artifact_store=job_runner.artifact_store)
