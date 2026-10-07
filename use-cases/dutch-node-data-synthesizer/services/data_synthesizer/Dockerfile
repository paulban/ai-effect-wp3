# Synthetic power grid service.
#
# The build context is use-cases/, so the shared control-plane package can be
# copied in alongside this service's own code. The service imports the shared
# package as `common` and its own modules as `synth`.
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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared control plane: dispatch, authentication, artifact endpoint, job runner.
COPY --from=common / ./common/

# The synthesizer still builds protobuf config objects internally, so its proto
# and the stub generation stay.
#
# The proto lands at /app/proto because proto_runtime.ensure_generated() derives
# the directory as <package>/../proto. The previous Dockerfile put it elsewhere
# and pre-compiled the stubs at build time, which meant that runtime path was
# never exercised — and was wrong. Stubs are still pre-compiled below so startup
# does no work, but the runtime fallback now resolves correctly too.
# COPY dutch-node-data-synthesizer/services/data_synthesizer/proto/ /app/proto/
COPY synth/ ./synth

# RUN mkdir -p /app/synth/_generated && \
#     python -m grpc_tools.protoc \
#       -I/app/proto \
#       --python_out=/app/synth/_generated \
#       --grpc_python_out=/app/synth/_generated \
#       /app/proto/data_synthesizer.proto

COPY handler.py ./handler.py
COPY service.py ./service.py

# ENV HOST=0.0.0.0
# ENV PORT=8080
# ENV SELF_URL=http://synthetic-data:8080
ENV ARTIFACT_DIR=/artifacts
# One synthesis at a time: grid generation is CPU-bound, and an uncapped
# service would starve the live human-AI sessions sharing the host.
ENV MAX_CONCURRENT_JOBS=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "service.py"]
