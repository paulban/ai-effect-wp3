#
# AI-EFFECT - Synthetic power grid service.
#
# This image, when built, will contain all necessary code
# and packages to run the service.
#

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# When building this image without the aid of Docker compose,
# be sure to add the --build-context parameter as follows:
#
# --build-context=common=use-cases/common
#
# This assumes you are building the image from the root of
# this repository. Adjust the path based on your current working directory.
COPY --from=common / ./common/

COPY handler.py ./handler.py
COPY service.py ./service.py

# ENV HOST=0.0.0.0
# ENV PORT=8080
# ENV SELF_URL=http://synthetic-data:8080
ENV ARTIFACT_DIR=/artifacts
# One synthesis at a time: grid generation is CPU-bound, and an uncapped
# service would starve the live human-AI sessions sharing the host.
ENV MAX_CONCURRENT_JOBS=1

# EXPOSE 8080

# HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
#     CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "service.py"]
