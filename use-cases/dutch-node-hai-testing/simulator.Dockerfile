# syntax=docker/dockerfile:1
#
# Build of InteractiveAI's PowerGrid simulator, used instead of its
# usecases_examples/PowerGrid/Dockerfile.app (selected by simulator.override.yml;
# the build context is still that unmodified folder).
#
# Same base image, packages, requirements and command as Dockerfile.app at
# InteractiveAI 600a1ce, with two changes:
#
# 1. Debian 11 (bullseye) is end of life. deb.debian.org's security index still
#    lists updates whose files are gone, so `apt-get install` fails with 404s.
#    The security repository is switched to archive.debian.org, which still
#    serves them. Upstream fix: move the image to a supported Debian release
#    (InteractiveAI change).
# 2. Dependencies are installed before the source is copied, so a source change
#    does not repeat the multi-GB install (the running container uses the
#    bind-mounted source anyway).

FROM python:3.9-slim-bullseye
EXPOSE 5000

RUN sed -i 's#http://deb.debian.org/debian-security#http://archive.debian.org/debian-security#' /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y ffmpeg libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code
COPY requirements-app.txt /code/requirements-app.txt
RUN pip3 install --no-cache-dir -r requirements-app.txt

COPY . /code/

CMD ["python3", "PowerGrid_poc_simulator_app.py"]
