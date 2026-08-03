FROM python:3.11-slim

WORKDIR /app

COPY file_based_energy_pipeline/services/report_generator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY file_based_energy_pipeline/services/report_generator/service.py .

EXPOSE 8080

CMD ["python", "service.py"]
