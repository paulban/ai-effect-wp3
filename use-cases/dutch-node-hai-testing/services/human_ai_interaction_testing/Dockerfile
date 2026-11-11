# Human-AI interaction session service (RunHAISession).
#
# The build context is use-cases/, so the shared control-plane package can be
# copied in alongside this service's own code, as for benchmarking.
#
# InteractiveAI is NOT part of this image. It runs as its own stack, set up by
# scripts/setup_interactiveai.sh; this service reaches it over the
# ai-effect-services network.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY dutch-node-hai-testing/services/human_ai_interaction_testing/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Shared control plane: dispatch, authentication, artifact endpoint, job runner.
COPY common/ /app/common/

COPY dutch-node-hai-testing/services/human_ai_interaction_testing/*.py /app/

# Algorithms and scenarios are selected by name from these directories. The
# image ships the default algorithm; docker-compose.yml bind-mounts both so new
# ones can be added without a rebuild.
COPY dutch-node-hai-testing/services/human_ai_interaction_testing/algorithms/ /app/algorithms/
RUN mkdir -p /app/scenarios /app/runtime
ENV ALGORITHM_DIRECTORY=/app/algorithms
ENV SCENARIO_DIRECTORY=/app/scenarios
# The simulator's CONFIG.toml, shared by a single-file bind mount.
ENV HAI_SIMULATOR_CONFIG_PATH=/app/runtime/CONFIG.toml

ENV HOST=0.0.0.0
ENV PORT=8080
ENV SELF_URL=http://hai-session-runner:8080
ENV ARTIFACT_DIR=/artifacts
# One session at a time: the simulator, the agent slot and the test account
# are shared (spec FR-05).
ENV MAX_CONCURRENT_JOBS=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "service.py"]
