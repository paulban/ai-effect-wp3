"""Flask sidecar for the hai-survey-wrapper container.

Provides two HTTP endpoints (both proxied via nginx at /api/):

  GET  /api/healthz      — health check, returns HTTP 200 (FR-17)
  POST /api/save_results — receives the survey results JSON from the wrapper
                           page's window.postMessage listener and writes it
                           to /results/survey_outcomes.json on the shared
                           host-mounted volume (FR-18, FR-09)

The Flask app runs on port 5000 inside the container. External traffic reaches
it only through nginx (port 80), which proxies /api/ requests here.

Environment variables:
  HAI_RESULTS_PATH     Absolute path to the per-session results directory
                       (default: /results — overridden by WP3 volume mount, FR-03)
  HAI_SURVEY_FILENAME  Output filename written to HAI_RESULTS_PATH
                       (default: survey_outcomes.json)
  FLASK_PORT           Port Flask listens on inside the container (default: 5000)

Spec coverage: FR-09, FR-17, FR-18
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Directory where survey_outcomes.json is written. WP3 mounts the per-session
# results host directory here (FR-03). Configurable for testing.
RESULTS_DIRECTORY: Path = Path(os.environ.get("HAI_RESULTS_PATH", "/results"))

# Output filename expected by the WP3 polling thread (FR-09).
SURVEY_OUTCOMES_FILENAME: str = os.environ.get(
    "HAI_SURVEY_FILENAME", "survey_outcomes.json"
)

# Port Flask listens on inside the container (nginx proxies from port 80).
FLASK_LISTEN_PORT: int = int(os.environ.get("FLASK_PORT", "5000"))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/healthz", methods=["GET"])
def health_check() -> tuple:
    """Return HTTP 200 to signal the Flask sidecar is up and running (FR-17).

    Used by Docker healthcheck and optionally by the WP3 service before
    directing the operator to the survey URL.

    Returns:
        JSON body ``{"status": "ok"}`` with HTTP 200.
    """
    return jsonify({"status": "ok"}), 200


@app.route("/api/save_results", methods=["POST"])
def save_survey_results() -> tuple:
    """Receive survey results and write survey_outcomes.json to the shared volume (FR-18).

    The wrapper page (static/index.html) listens for window.postMessage from the
    surveychainer iframe and POSTs the payload here via fetch(). This endpoint
    writes the JSON to the WP3-mounted results directory, making it visible to
    the WP3 polling thread so the session can transition to COMPLETED (FR-10).

    Expected request body: the raw window.postMessage payload from surveychainer,
    which has the shape::

        {
            "success": true,
            "participantData": {"id": "...", "condition": "..."},
            "allResults": {
                "timestamp": "...",
                "surveys": [{"surveyName": "...", "timestamp": "...", "payload": {...}}]
            }
        }

    Returns:
        JSON body ``{"status": "saved"}`` with HTTP 200, or an error dict with
        HTTP 400 / 500.
    """
    survey_payload: dict | None = request.get_json(force=True, silent=True)

    if survey_payload is None:
        logger.warning("POST /api/save_results: request body is not valid JSON")
        return jsonify({"error": "Request body must be a JSON object"}), 400

    if not isinstance(survey_payload, dict):
        logger.warning(
            "POST /api/save_results: expected JSON object, got %s",
            type(survey_payload).__name__,
        )
        return jsonify({"error": "Request body must be a JSON object, not an array or scalar"}), 400

    output_path = RESULTS_DIRECTORY / SURVEY_OUTCOMES_FILENAME

    try:
        output_path.write_text(json.dumps(survey_payload, indent=2), encoding="utf-8")
        logger.info(
            "survey_outcomes.json written: path=%s survey_count=%d",
            output_path,
            len(survey_payload.get("allResults", {}).get("surveys", [])),
        )
        return jsonify({"status": "saved"}), 200

    except OSError as exc:
        logger.exception("Failed to write survey_outcomes.json: path=%s", output_path)
        return jsonify({"error": f"Failed to write results: {exc}"}), 500


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "hai-survey-wrapper Flask sidecar starting on port %d, results dir: %s",
        FLASK_LISTEN_PORT,
        RESULTS_DIRECTORY,
    )
    app.run(host="0.0.0.0", port=FLASK_LISTEN_PORT)
