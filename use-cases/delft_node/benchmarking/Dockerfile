FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY benchmarking/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY shared/proto /app/shared/proto
COPY benchmarking/common /app/common
COPY benchmarking/algorithms /app/algorithms
COPY benchmarking/main.py /app/main.py
COPY benchmarking/blueprint.json /app/blueprint.json
COPY benchmarking/dockerinfo.json /app/dockerinfo.json
COPY benchmarking/run_workflow.sh /app/run_workflow.sh

RUN mkdir -p /app/common/_generated && \
    python -m grpc_tools.protoc -I/app/shared/proto --python_out=/app/common/_generated --grpc_python_out=/app/common/_generated /app/shared/proto/common.proto /app/shared/proto/data_synthesizer.proto /app/shared/proto/benchmarking.proto

ENV GRPC_HOST=benchmark-runner
ENV GRPC_PORT=50051

EXPOSE 8080
EXPOSE 50051

CMD ["python", "main.py"]
