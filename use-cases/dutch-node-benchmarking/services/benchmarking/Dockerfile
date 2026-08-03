FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY benchmarking/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY benchmarking/proto /app/benchmarking/proto
# Vendored copy of the data synthesizer's interface — the benchmark calls it
# over gRPC. See external_proto/README.md.
COPY benchmarking/external_proto /app/benchmarking/external_proto
COPY benchmarking/common /app/common
COPY benchmarking/algorithms /app/algorithms
COPY benchmarking/main.py /app/main.py
COPY benchmarking/run_workflow.sh /app/run_workflow.sh

RUN mkdir -p /app/common/_generated && \
    python -m grpc_tools.protoc -I/app/benchmarking/proto -I/app/benchmarking/external_proto --python_out=/app/common/_generated --grpc_python_out=/app/common/_generated /app/benchmarking/external_proto/data_synthesizer.proto /app/benchmarking/proto/benchmarking.proto

ENV GRPC_HOST=benchmark-runner
ENV GRPC_PORT=50051
# Without these the app falls back to port 8005, contradicting EXPOSE 8080
# and the generated dockerinfo.json that the portal dials.
ENV PORT=8080
ENV SELF_URL=http://benchmark-runner:8080

EXPOSE 8080
EXPOSE 50051

CMD ["python", "main.py"]
