# Grid2Op benchmark service.
#
# The build context is use-cases/, so the shared control-plane package can be
# copied in alongside this service's own code. The service imports the shared
# package as `common`, through handler.py like the onboarding templates.
#
# Results are served as artifacts over HTTP at /control/data. The orchestrator's
# workers reach the service on the shared Docker network; docker-compose.yml
# additionally publishes a loopback port for fetching results on this machine.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY dutch-node-benchmarking/services/benchmarking/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
# grid2op's EnvRecorder needs pyarrow, and pyarrow 20+ only imports with
# NumPy 2, while pypowsybl2grid pins numpy==1.26.4. pyarrow 19.0.1 is the
# newest that works with NumPy 1.x, so it replaces the version grid2evaluate
# declares (>=20); pip reports that conflict, and it is deliberate.
RUN pip install --no-cache-dir --no-deps pyarrow==19.0.1

# Shared control plane: dispatch, authentication, artifact endpoint, job runner.
COPY common/ /app/common/

COPY dutch-node-benchmarking/services/benchmarking/handler.py /app/handler.py
COPY dutch-node-benchmarking/services/benchmarking/service.py /app/service.py

# Algorithms and scenarios are selected by name from these directories. The
# image ships the default algorithm; docker-compose.yml bind-mounts both
# directories so new ones can be dumped in without a rebuild.
COPY dutch-node-benchmarking/services/benchmarking/algorithms/ /app/algorithms/
RUN mkdir -p /app/scenarios
ENV ALGORITHM_DIRECTORY=/app/algorithms
ENV SCENARIO_DIRECTORY=/app/scenarios

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

CMD ["python", "service.py"]
