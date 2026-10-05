FROM python:3.12-slim

WORKDIR /app

COPY services/web_ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/web_ui/app.py ./

ENV HOST=0.0.0.0
ENV PORT=8080
ENV DATA_DIR=/data
ENV ORCHESTRATOR_URL=http://host.docker.internal:18000

EXPOSE 8080

CMD ["python", "app.py"]
