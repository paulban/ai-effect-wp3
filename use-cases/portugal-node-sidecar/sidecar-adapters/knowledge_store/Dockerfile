FROM python:3.12-slim

WORKDIR /app

COPY sidecar-adapters/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy common module
COPY common/ /app/common/

# Copy service
COPY sidecar-adapters/knowledge_store/service.py /app/

ENV HOST=0.0.0.0
ENV PORT=8080

CMD ["python", "service.py"]
