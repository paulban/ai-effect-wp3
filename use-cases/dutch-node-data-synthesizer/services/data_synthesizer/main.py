"""Synthetic power grid service entrypoint.

Grid synthesis is minutes of work, so the service accepts a job and returns a
task id rather than holding the request open. There is no gRPC server: the
synthesized grid is served as an artifact over HTTP.
"""

from common.batch_jobs import build_runner
from common.concurrent import run

from synth import synth_operations

if __name__ == "__main__":
    job_runner = build_runner()
    synth_operations.set_job_runner(job_runner)
    run(synth_operations, artifact_store=job_runner.artifact_store)
