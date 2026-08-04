# Grid2Op benchmark service.
#
# The build context is use-cases/, so the shared control-plane package can be
# copied in alongside this service's own code. The service imports the shared
# package as `common` and its own modules as `benchmark`.
#
# No published port and no gRPC server: results are served as artifacts over
# HTTP at /control/data, reachable only from the orchestrator's workers on the
# shared Docker network.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY dutch-node-benchmarking/services/benchmarking/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Shared control plane: dispatch, authentication, artifact endpoint, job runner.
COPY common/ /app/common/

COPY dutch-node-benchmarking/services/benchmarking/benchmark/ /app/benchmark/
COPY dutch-node-benchmarking/services/benchmarking/algorithms/ /app/algorithms/
COPY dutch-node-benchmarking/services/benchmarking/main.py /app/main.py

ENV HOST=0.0.0.0
ENV PORT=8080
ENV SELF_URL=http://benchmark-runner:8080
ENV ARTIFACT_DIR=/artifacts
# One benchmark at a time: a grid2op run saturates the cores it is given, and
# an uncapped service would starve the live human-AI sessions on the same host.
ENV MAX_CONCURRENT_JOBS=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py"]
