FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY data_synthesizer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/proto ./shared/proto
COPY data_synthesizer/common ./common
COPY data_synthesizer/main.py ./main.py
COPY data_synthesizer/blueprint.json ./blueprint.json
COPY data_synthesizer/dockerinfo.json ./dockerinfo.json

RUN mkdir -p ./common/_generated && \
	python -m grpc_tools.protoc -I./shared/proto --python_out=./common/_generated --grpc_python_out=./common/_generated ./shared/proto/data_synthesizer.proto

ENV SELF_URL=http://synthetic-data:8080
ENV HOST=0.0.0.0
ENV PORT=8080
ENV GRPC_HOST=synthetic-data
ENV GRPC_PORT=50051

EXPOSE 8080
EXPOSE 50051

CMD ["python", "main.py"]