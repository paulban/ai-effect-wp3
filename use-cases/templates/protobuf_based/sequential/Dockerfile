FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy proto files and compile (with gRPC stubs)
COPY proto/ ./proto/
RUN python -m grpc_tools.protoc -I./proto --python_out=. --grpc_python_out=. proto/*.proto

COPY common/ ./common/
COPY handler.py service.py ./

ENV HOST=0.0.0.0
ENV PORT=8080
ENV GRPC_PORT=50051

EXPOSE 8080 50051

CMD ["python", "service.py"]
