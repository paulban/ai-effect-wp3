FROM python:3.11-slim

WORKDIR /app

COPY protobuf_based_energy_pipeline/services/data_analyzer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all needed protos
COPY protobuf_based_energy_pipeline/shared/proto/common.proto ./proto/
COPY protobuf_based_energy_pipeline/services/data_analyzer/proto/ ./proto/
COPY protobuf_based_energy_pipeline/services/data_generator/proto/data_generator.proto ./proto/

# Compile protos (with gRPC stubs)
RUN python -m grpc_tools.protoc -I./proto --python_out=. --grpc_python_out=. proto/common.proto
RUN python -m grpc_tools.protoc -I./proto --python_out=. --grpc_python_out=. proto/data_analyzer.proto
RUN python -m grpc_tools.protoc -I./proto --python_out=. --grpc_python_out=. proto/data_generator.proto

COPY common/ ./common/
COPY protobuf_based_energy_pipeline/services/data_analyzer/service.py .

ENV HOST=0.0.0.0
ENV PORT=8080
ENV GRPC_PORT=50051

EXPOSE 8080 50051

CMD ["python", "service.py"]
