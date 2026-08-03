# Data Provision service with AI-Effect control interface (integrated)
#
# Based on the original Dockerfile, modified for context: . (parent dir)
# and to include the common/ module + httpx dependency.

# ---- Build Stage ----
FROM python:3.11 AS builder

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY data_provision/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir httpx

# ---- Final Stage ----
FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get -y install curl && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home appuser
USER appuser

COPY data_provision/ .
COPY common/ ./common/

EXPOSE 600

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:600/ || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "600"]
