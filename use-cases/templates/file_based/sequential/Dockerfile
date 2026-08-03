FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY handler.py service.py ./

ENV HOST=0.0.0.0
ENV PORT=8080

EXPOSE 8080

CMD ["python", "service.py"]
