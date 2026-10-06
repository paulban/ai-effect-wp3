# Synthetic power grid service.
#
# The build context is use-cases/, so the shared control-plane package can be
# copied in alongside this service's own code. The service imports the shared
# package as `common` and its own modules as `synth`.
#
# No published port: results are served as artifacts over
# HTTP at /control/data, reachable only from the orchestrator's workers on the
# shared Docker network.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY dutch-node-data-synthesizer/services/data_synthesizer/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Shared control plane: dispatch, authentication, artifact endpoint, job runner.
COPY common/ /app/common/

COPY dutch-node-data-synthesizer/services/data_synthesizer/synth/ /app/synth/
# COPY dutch-node-data-synthesizer/services/data_synthesizer/main.py /app/main.py
COPY dutch-node-data-synthesizer/services/data_synthesizer/service.py /app/service.py

ENV HOST=0.0.0.0
ENV PORT=8080
ENV SELF_URL=http://synthetic-data:8080
ENV ARTIFACT_DIR=/artifacts
# One synthesis at a time: grid generation is CPU-bound, and an uncapped
# service would starve the live human-AI sessions sharing the host.
ENV MAX_CONCURRENT_JOBS=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py"]
