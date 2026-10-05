FROM python:3.12-slim

WORKDIR /app

COPY services/config_assistant/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Common can also use the shared WP3 common files
COPY common/ ./common/
COPY services/config_assistant/handler.py services/config_assistant/service.py services/config_assistant/ollama_client.py ./

ENV HOST=0.0.0.0
ENV PORT=8080
ENV DATA_DIR=/data
ENV OLLAMA_URL=http://ollama:11434
ENV OLLAMA_MODEL=qwen2.5:1.5b

EXPOSE 8080

CMD ["python", "service.py"]
