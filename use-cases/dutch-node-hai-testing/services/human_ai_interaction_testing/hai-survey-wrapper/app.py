"""Flask sidecar for the hai-survey-wrapper container.

The wrapper serves a questionnaire to a participant and reports the answers
back. It used to report them by writing ``survey_outcomes.json`` into a host
directory shared with the WP3 service, which then polled for the file to
appear. That required a bind mount into both containers, coupled the two
through host filesystem paths, and made session completion a matter of
scheduling luck rather than an event.

The wrapper now POSTs the answers to the WP3 service instead. Nothing is
written to disk and no directory is shared, so this container is disposable and
can be reassigned to the next participant.

Because the container is reassigned rather than recreated, it also has to be
told which session it is serving. That is what the session endpoints below are
for: WP3 configures a slot before sending a participant to it, and resets it
afterwards.

Endpoints, all reached through nginx at /api/:

  GET  /api/healthz      — health check
  POST /api/session      — bind this wrapper to a session (called by WP3)
  POST /api/reset        — return to idle (called by WP3)
  GET  /api/state        — report the bound session (called by WP3)
  POST /api/save_results — receive answers from the wrapper page and forward
                           them to WP3

Spec coverage: FR-20, FR-21, FR-22
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

import requests
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Port Flask listens on inside the container (nginx proxies from port 80).
FLASK_LISTEN_PORT: int = int(os.environ.get("FLASK_PORT", "5000"))

# How long to wait when forwarding answers to WP3. Generous: losing a
# participant's answers because a collector was briefly slow would mean asking
# them to sit the questionnaire again.
COLLECT_TIMEOUT_SECONDS: float = float(os.environ.get("HAI_COLLECT_TIMEOUT", "30"))


@dataclass
class BoundSession:
    """The session this wrapper is currently serving.

    Attributes:
        session_id: WP3 session identifier.
        survey_id: Which questionnaire to present.
        collect_url: Where to POST the answers when the participant finishes.
        session_token: Credential proving this result belongs to this session.
            WP3's collect endpoint rejects results without it, because the
            callers that post results cannot hold the service API key.
    """

    session_id: str
    survey_id: str
    collect_url: str
    session_token: str


# Guarded by a lock because Flask serves requests from several threads: WP3 can
# be resetting a slot at the same moment a participant submits answers.
_session_lock = threading.Lock()
_bound_session: BoundSession | None = None


@app.route("/api/healthz", methods=["GET"])
def health_check() -> tuple:
    """Return HTTP 200 to signal the Flask sidecar is up.

    Returns:
        JSON body ``{"status": "ok"}`` with HTTP 200.
    """
    return jsonify({"status": "ok"}), 200


@app.route("/api/session", methods=["POST"])
def bind_session() -> tuple:
    """Bind this wrapper to a session before a participant is sent to it.

    Returns 409 when a session is already bound, so WP3 cannot silently
    overwrite a participant who is mid-questionnaire; the slot must be reset
    first.

    Returns:
        The bound session's public fields with HTTP 200, or an error with 400
        or 409.
    """
    global _bound_session

    request_body = request.get_json(silent=True)
    if not isinstance(request_body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    missing_fields = [
        field_name
        for field_name in ("session_id", "collect_url", "session_token")
        if not request_body.get(field_name)
    ]
    if missing_fields:
        return jsonify({"error": f"Missing required fields: {', '.join(missing_fields)}"}), 400

    with _session_lock:
        if _bound_session is not None:
            return (
                jsonify(
                    {
                        "error": "This wrapper is already bound to a session",
                        "session_id": _bound_session.session_id,
                    }
                ),
                409,
            )

        _bound_session = BoundSession(
            session_id=request_body["session_id"],
            survey_id=request_body.get("survey_id", ""),
            collect_url=request_body["collect_url"],
            session_token=request_body["session_token"],
        )
        bound = _bound_session

    logger.info("Bound to session %s (survey_id=%s)", bound.session_id, bound.survey_id or "-")
    return jsonify({"session_id": bound.session_id, "survey_id": bound.survey_id}), 200


@app.route("/api/reset", methods=["POST"])
def reset_session() -> tuple:
    """Unbind the current session and return this wrapper to the pool.

    Idempotent, so a reset racing with a timeout sweep is harmless.

    Returns:
        The released session id, or null when none was bound, with HTTP 200.
    """
    global _bound_session

    with _session_lock:
        released_session = _bound_session
        _bound_session = None

    released_session_id = released_session.session_id if released_session else None
    logger.info("Released session %s", released_session_id or "(none)")
    return jsonify({"released_session_id": released_session_id}), 200


@app.route("/api/state", methods=["GET"])
def read_state() -> tuple:
    """Report which session this wrapper is serving.

    Returns:
        JSON with `bound` and, when bound, the session and survey ids.
    """
    with _session_lock:
        current_session = _bound_session

    if current_session is None:
        return jsonify({"bound": False}), 200

    return (
        jsonify(
            {
                "bound": True,
                "session_id": current_session.session_id,
                "survey_id": current_session.survey_id,
            }
        ),
        200,
    )


@app.route("/api/save_results", methods=["POST"])
def save_survey_results() -> tuple:
    """Receive the participant's answers and forward them to WP3.

    The wrapper page listens for window.postMessage from the surveychainer
    iframe and POSTs the payload here. This endpoint attaches the session id
    and token, then forwards it to WP3's collect endpoint — which completes the
    session if the simulator trace has also arrived (FR-20, FR-21).

    Expected body: the raw postMessage payload, of the shape::

        {
            "success": true,
            "participantData": {"id": "...", "condition": "..."},
            "allResults": {"timestamp": "...", "surveys": [...]}
        }

    Returns:
        ``{"status": "forwarded"}`` with HTTP 200; 400 for a malformed body,
        409 when no session is bound, 502 when WP3 could not be reached.
    """
    survey_payload = request.get_json(force=True, silent=True)

    if not isinstance(survey_payload, dict):
        logger.warning("POST /api/save_results: body is not a JSON object")
        return jsonify({"error": "Request body must be a JSON object"}), 400

    with _session_lock:
        current_session = _bound_session

    if current_session is None:
        # Answers with nowhere to go. Reported rather than dropped silently,
        # because the previous design's silent failure is exactly how a session
        # could appear to succeed while producing no results.
        logger.error("POST /api/save_results: no session bound; answers cannot be attributed")
        return jsonify({"error": "No session is bound to this wrapper"}), 409

    forwarded_body = dict(survey_payload)
    forwarded_body["wp3_session_id"] = current_session.session_id
    forwarded_body["session_token"] = current_session.session_token

    try:
        collect_response = requests.post(
            current_session.collect_url,
            json=forwarded_body,
            timeout=COLLECT_TIMEOUT_SECONDS,
        )
        collect_response.raise_for_status()
    except requests.RequestException as forward_error:
        logger.exception(
            "Could not forward answers for session %s to %s",
            current_session.session_id,
            current_session.collect_url,
        )
        return jsonify({"error": f"Could not deliver results to WP3: {forward_error}"}), 502

    logger.info(
        "Forwarded answers for session %s (%d survey sections)",
        current_session.session_id,
        len(survey_payload.get("allResults", {}).get("surveys", [])),
    )
    return jsonify({"status": "forwarded", "session_id": current_session.session_id}), 200


if __name__ == "__main__":
    logger.info("hai-survey-wrapper Flask sidecar starting on port %d", FLASK_LISTEN_PORT)
    app.run(host="0.0.0.0", port=FLASK_LISTEN_PORT)
