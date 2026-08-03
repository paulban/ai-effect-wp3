# hai-survey-wrapper — WP3-built Docker image for the hmisurveys survey container.
#
# Build context: use-cases/dutch-node/human_ai_interaction_testing/
# Build command (from that directory):
#   docker build -f hai-survey-wrapper/Dockerfile -t hai-survey-wrapper:latest .
#
# Architecture (FR-16):
#   - nginx on port 80 serves the hmisurveys static HTML/JS files
#   - Flask sidecar on port 5000 (internal only, proxied via /api/ by nginx):
#       GET  /api/healthz      → HTTP 200 (FR-17)
#       POST /api/save_results → writes survey_outcomes.json to /results (FR-18)
#   - Custom wrapper index.html intercepts window.postMessage from the
#     surveychainer iframe and POSTs survey results to /api/save_results
#   - supervisord starts both nginx and Flask
#
# Volume mount expected at /results (shared with WP3 polling thread, FR-03).

FROM python:3.11-slim

# Install nginx and supervisor for process management
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx supervisor \
    && rm -rf /var/lib/apt/lists/*

# Install Flask for the survey-results sidecar
COPY hai-survey-wrapper/requirements.txt /tmp/hai-survey-requirements.txt
RUN pip install --no-cache-dir -r /tmp/hai-survey-requirements.txt

# Copy all hmisurveys static files into the nginx serve root under /html/
# so the wrapper index.html can embed them at /html/surveychainer.html (FR-16)
COPY hmisurveys/html/ /usr/share/nginx/html/html/

# Copy the WP3 wrapper index.html (served at /) and any additional static assets
COPY hai-survey-wrapper/static/ /usr/share/nginx/html/

# Nginx configuration: serve static files + proxy /api/ to Flask
COPY hai-survey-wrapper/nginx.conf /etc/nginx/conf.d/default.conf

# Remove default nginx site config to avoid conflicts
RUN rm -f /etc/nginx/sites-enabled/default

# Flask sidecar application
COPY hai-survey-wrapper/app.py /app/app.py

# Supervisord configuration to run both nginx and Flask
COPY hai-survey-wrapper/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create the results directory; in practice it is overridden by the WP3 volume mount
RUN mkdir -p /results

EXPOSE 80

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
