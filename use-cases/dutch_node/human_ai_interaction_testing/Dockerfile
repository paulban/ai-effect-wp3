FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY human_ai_interaction_testing/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY human_ai_interaction_testing/proto /app/human_ai_interaction_testing/proto
COPY human_ai_interaction_testing/common /app/common
COPY human_ai_interaction_testing/main.py /app/main.py
COPY human_ai_interaction_testing/blueprint.json /app/blueprint.json
COPY human_ai_interaction_testing/dockerinfo.json /app/dockerinfo.json

RUN mkdir -p /app/common/_generated && \
    python -m grpc_tools.protoc \
      -I/app/human_ai_interaction_testing/proto \
      --python_out=/app/common/_generated \
      --grpc_python_out=/app/common/_generated \
      /app/human_ai_interaction_testing/proto/human_ai_interaction_testing.proto

# Docker socket is required for container management (see README.md for setup).
# Mount it at runtime: -v /var/run/docker.sock:/var/run/docker.sock
# The results directory must also be mounted: -v <HOST_PATH>:/hai-sessions

ENV GRPC_HOST=hai-testing-service
ENV GRPC_PORT=50051
ENV HOST=0.0.0.0
ENV PORT=8080

EXPOSE 8080
EXPOSE 50051

CMD ["python", "main.py"]
