FROM python:3.12-slim

WORKDIR /app

COPY germany-node/services/villas_chronics/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY germany-node/services/villas_chronics/service.py .
COPY germany-node/chronics.conf.template .

EXPOSE 8080

CMD ["python", "service.py"]
