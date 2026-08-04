"""Transport adapters for the human-AI session operations.

Everything here is translation: decode what arrived over HTTP, hand it to
``session_service``, and encode what comes back. The session logic itself lives
in that module so the control plane and the result-collection endpoints cannot
drift apart — which is what happened in the previous design, where the HTTP
handler and the gRPC handler each carried a copy of the session-start sequence.

Two things this module deliberately no longer does:

* It does not create containers. Sessions run on pre-declared pool slots, so
  the Docker SDK, the socket mount and the per-session host port bindings are
  gone, and with them the reason the service needed root-equivalent access.
* It does not run a gRPC server. The results data plane is now the HTTP
  artifact endpoint, whose DataReference the submitting caller can actually
  fetch. Nothing called the gRPC endpoints, and not binding the port keeps it
  off the network entirely. ``human_ai_interaction_testing.proto`` is unchanged
  and remains the portal's interface description.

Spec coverage: FR-01, FR-04, FR-05, FR-19, FR-20, FR-21
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any

from common.concurrent import DataReference, ExecuteRequest, ExecuteResponse

from .session_service import HumanAISessionSpec, get_session_service

logger = logging.getLogger(__name__)

# Status strings the orchestrator understands, returned from /control/execute.
STATUS_PENDING = "pending"
STATUS_FAILED = "failed"

# Fields that exist to route and authorise a posted result, not to describe it.
# They are removed before the result is stored, so a session credential never
# ends up inside the participant data the artifact endpoint serves (FR-16).
TRANSPORT_ONLY_FIELDS = frozenset({"session_token", "wp3_session_id", "session_id"})


def _decode_inline_input(inputs: list[dict]) -> dict[str, Any]:
    """
    Decode the session specification carried in an execute request.

    The orchestrator passes workflow inputs as DataReferences. A session
    specification is small, so it arrives inline — either base64-encoded in the
    reference's ``uri``, which is how the orchestrator's submit script writes
    it, or as plain JSON for hand-written calls.

    Args:
        inputs: The request's inputs list. Only the first entry is read.

    Returns:
        The decoded specification mapping, empty when no input was supplied, in
        which case the session starts on the slot's configured defaults.

    Raises:
        ValueError: If an input is present but decodes as neither
            base64-encoded JSON nor plain JSON. Silently falling back to
            defaults would hide the caller's mistake behind a session that ran
            the wrong scenario.
    """
    if not inputs:
        return {}

    first_input = inputs[0]
    protocol = first_input.get("protocol", "inline")
    if protocol != "inline":
        raise ValueError(
            f"Unsupported input protocol {protocol!r}; this service accepts 'inline' only"
        )

    raw_value = first_input.get("uri", "")
    if not raw_value:
        return {}

    for decode_attempt in (
        lambda value: json.loads(base64.b64decode(value).decode("utf-8")),
        lambda value: json.loads(value),
    ):
        try:
            decoded = decode_attempt(raw_value)
        except Exception:  # noqa: BLE001 - both decoders are tried before failing
            continue

        if isinstance(decoded, dict):
            return decoded

    raise ValueError("Inline input is neither base64-encoded JSON nor plain JSON")


def execute_StartHumanAISession(request: ExecuteRequest) -> ExecuteResponse:  # noqa: N802
    """Start a human-AI testing session and return the participant's links.

    Named to match the operation in the proto, because the shared control plane
    dispatches by looking up ``execute_<MethodName>`` on the handler module.

    The response status is ``pending`` rather than ``complete`` on purpose: the
    session has been prepared, but it is not finished until a participant has
    worked through it and both results have been posted back. Callers poll
    ``/control/status/{task_id}`` and read the artifact from
    ``/control/output/{task_id}`` once that reports complete.

    Args:
        request: Orchestrator execute request. Its ``task_id`` becomes the
            session id, so status and output are addressable by it.

    Returns:
        An ExecuteResponse carrying the participant URLs, or a failed response
        explaining why no session could be started.
    """
    session_id = request.task_id or uuid.uuid4().hex

    try:
        specification_payload = _decode_inline_input(request.inputs)
    except ValueError as decode_error:
        logger.warning("Rejected session request %s: %s", session_id, decode_error)
        return ExecuteResponse(status=STATUS_FAILED, task_id=session_id, error=str(decode_error))

    outcome = get_session_service().start_session(
        session_id, HumanAISessionSpec.from_mapping(specification_payload)
    )

    if not outcome.succeeded:
        return ExecuteResponse(status=STATUS_FAILED, task_id=session_id, error=outcome.message)

    # The links are the actionable part of the response, so they travel inline
    # rather than behind another fetch: whoever submitted the workflow needs
    # them immediately in order to send a participant to them.
    session_links = json.dumps(
        {
            "session_id": outcome.session_id,
            "gui_url": outcome.gui_url,
            "survey_url": outcome.survey_url,
            "message": outcome.message,
        }
    )

    return ExecuteResponse(
        status=STATUS_PENDING,
        task_id=session_id,
        output=DataReference(
            protocol="inline",
            uri=base64.b64encode(session_links.encode("utf-8")).decode("ascii"),
            format="json",
        ),
    )


# Handler registry consumed by the control plane layer.
session_handlers: dict = {
    "StartHumanAISession": execute_StartHumanAISession,
}


def _handle_collect(body: dict, result_kind: str) -> tuple[dict, int]:
    """
    Accept a result posted by one of the session tools.

    Both producers run outside this service, and one of them runs inside the
    participant's browser, so neither can hold the service API key. The session
    token issued when the session started is what authorises them (FR-04).

    Args:
        body: Parsed request body. Carries the session id as ``wp3_session_id``
            or ``session_id``, and must carry ``session_token``.
        result_kind: Which producer this is — "trace" or "survey".

    Returns:
        A (response_body, http_status) pair: 401 when the token is missing or
        invalid, 404 when the session is unknown or the active session is
        ambiguous, 200 on success.
    """
    service = get_session_service()

    session_id, rejection_reason = service.resolve_result_session(
        body.get("wp3_session_id") or body.get("session_id"),
        body.get("session_token"),
    )

    if session_id is None:
        status_code = 401 if "token" in rejection_reason.lower() else 404
        logger.warning("Rejected %s result: %s", result_kind, rejection_reason)
        return {"error": rejection_reason}, status_code

    result_document = {
        field: value for field, value in body.items() if field not in TRANSPORT_ONLY_FIELDS
    }

    try:
        if result_kind == "trace":
            state = service.record_session_trace(session_id, result_document)
        else:
            state = service.record_survey_outcome(session_id, result_document)
    except KeyError:
        return {"error": f"Session not found: {session_id}"}, 404

    return {
        "status": "recorded",
        "session_id": session_id,
        "phase": state.phase.name,
        "complete": state.has_all_results(),
    }, 200


def collect_session_trace(body: dict) -> tuple[dict, int]:
    """Receive the simulator session trace posted by the participant's browser.

    Args:
        body: Trace document produced by traceSessionExport.ts, carrying the
            session id and token.

    Returns:
        A (response_body, http_status) pair.
    """
    return _handle_collect(body, result_kind="trace")


def collect_survey_outcome(body: dict) -> tuple[dict, int]:
    """Receive the questionnaire outcome posted by the survey wrapper.

    Args:
        body: Questionnaire answers, carrying the session id and token.

    Returns:
        A (response_body, http_status) pair.
    """
    return _handle_collect(body, result_kind="survey")
