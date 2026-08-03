# Knowledge Store service with AI-Effect control interface (integrated)
#
# Based on the original Dockerfile, modified for context: . (parent dir)
# and to include the common/ module.

# --- Stage 1: Build Stage ---
FROM python:3.11-slim as builder

WORKDIR /app

COPY knowledge_store/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir httpx

COPY knowledge_store/src ./src

# --- Stage 2: Final Stage ---
FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /usr/local/ /usr/local/
COPY --from=builder /app/src ./src

COPY common/ ./common/

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
