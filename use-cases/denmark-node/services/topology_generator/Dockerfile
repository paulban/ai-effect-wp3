# Danish Node — topology_generator (dummy service)
# Build context is the denmark-node root so that common/ can be copied in.
#   docker build -f services/topology_generator/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

COPY services/topology_generator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY services/topology_generator/handler.py services/topology_generator/service.py \
	services/topology_generator/dh_network_generator.py \
	services/topology_generator/syslab_heat_topology.yaml ./

ENV HOST=0.0.0.0
ENV PORT=8080
ENV DATA_DIR=/data

EXPOSE 8080

CMD ["python", "service.py"]
