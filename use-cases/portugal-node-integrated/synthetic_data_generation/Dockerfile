# Synthetic Data Generation service with AI-Effect control interface (integrated)
#
# Based on the original Dockerfile, modified for context: . (parent dir)
# and to include the common/ module + httpx + eval_type_backport dependencies.
#
# Uses Python 3.9 due to gretel-synthetics dependency constraints.

FROM python:3.9-slim

WORKDIR /app

RUN apt-get update && apt-get -y install curl && rm -rf /var/lib/apt/lists/*

COPY synthetic_data_generation/requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt
RUN pip install --no-cache-dir httpx eval_type_backport

COPY synthetic_data_generation/ .
COPY common/ ./common/

CMD ["python", "main.py"]
