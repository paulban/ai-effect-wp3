FROM python:3.11-slim

WORKDIR /app

COPY protobuf_based_energy_pipeline/services/input_provider/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY protobuf_based_energy_pipeline/services/input_provider/service.py .

ENV HOST=0.0.0.0
ENV PORT=8080

EXPOSE 8080

CMD ["python", "service.py"]
