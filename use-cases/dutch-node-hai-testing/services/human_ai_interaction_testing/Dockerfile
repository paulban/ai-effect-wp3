# Human-AI Interaction Testing control service.
#
# The build context is use-cases/, so the shared control-plane package can be
# copied in alongside this service's own code. The service imports the shared
# package as `common`, exactly like every other WP3 service, and its own
# modules as `hai`.
#
# No Docker socket, no results bind mount and no gRPC port: sessions run on
# pre-declared pool slots configured over HTTP, and results are posted back to
# this service rather than left as files for it to poll for.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY dutch-node-hai-testing/services/human_ai_interaction_testing/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Shared control plane: dispatch, authentication, artifact endpoint.
COPY common/ /app/common/

# This service's own session logic.
COPY dutch-node-hai-testing/services/human_ai_interaction_testing/hai/ /app/hai/
COPY dutch-node-hai-testing/services/human_ai_interaction_testing/main.py /app/main.py

ENV HOST=0.0.0.0
ENV PORT=8080
# Address the orchestrator's workers reach this service at on the shared network.
ENV SELF_URL=http://hai-testing-service:8080
# Where completed session results are written for /control/data to serve.
ENV HAI_ARTIFACT_DIR=/artifacts

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py"]
