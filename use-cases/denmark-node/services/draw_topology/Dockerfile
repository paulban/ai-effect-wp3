FROM python:3.12-slim

WORKDIR /app

COPY services/draw_topology/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY services/draw_topology/handler.py services/draw_topology/service.py ./
COPY services/draw_topology/draw_network.py ./

ENV HOST=0.0.0.0
ENV PORT=8080
ENV DATA_DIR=/data

EXPOSE 8080

CMD ["python", "service.py"]
