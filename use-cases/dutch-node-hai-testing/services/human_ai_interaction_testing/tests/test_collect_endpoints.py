"""Tests for the endpoints that receive results pushed by the session tools.

These replace the previous filesystem-polling arrangement: results are posted
in, authorised by the session token, and the second one to arrive completes the
session.

Spec coverage: FR-04, FR-16, FR-20, FR-21
"""

from __future__ import annotations

from hai import session_operations
from hai.session_manager import SessionPhase
from hai.session_service import HumanAISessionSpec, set_session_service
from tests.conftest import token_from_url


def _install(session_service):
    """Point the module-level accessor at a test service, returning a cleanup token."""
    set_session_service(session_service)
    return session_service


def test_trace_and_survey_together_complete_the_session(session_service, session_manager):
    """FR-21: the second result to arrive completes the session, with no polling."""
    _install(session_service)
    outcome = session_service.start_session("session-one", HumanAISessionSpec())
    session_token = token_from_url(outcome.gui_url)

    trace_body, trace_status = session_operations.collect_session_trace(
        {"wp3_session_id": "session-one", "session_token": session_token, "traces": [1, 2]}
    )
    assert trace_status == 200
    assert trace_body["complete"] is False

    survey_body, survey_status = session_operations.collect_survey_outcome(
        {"wp3_session_id": "session-one", "session_token": session_token, "answers": ["a"]}
    )

    assert survey_status == 200
    assert survey_body["complete"] is True
    assert session_manager.get("session-one").phase == SessionPhase.COMPLETED
    set_session_service(None)


def test_collect_without_a_token_is_unauthorised(session_service):
    """FR-04: a result with no session token is refused with 401."""
    _install(session_service)
    session_service.start_session("session-one", HumanAISessionSpec())

    response_body, status_code = session_operations.collect_session_trace(
        {"wp3_session_id": "session-one", "traces": []}
    )

    assert status_code == 401
    assert "token" in response_body["error"].lower()
    set_session_service(None)


def test_collect_for_an_unknown_session_is_not_found(session_service):
    """FR-04: a result naming a session that does not exist is a 404."""
    _install(session_service)

    response_body, status_code = session_operations.collect_session_trace(
        {"wp3_session_id": "no-such-session", "session_token": "irrelevant"}
    )

    assert status_code == 404
    assert "not found" in response_body["error"].lower()
    set_session_service(None)


def test_stored_results_do_not_contain_the_session_token(session_service, session_manager):
    """FR-16: a credential must not be persisted inside participant data.

    The token routes and authorises the request; storing it would put a working
    session credential into the artifact the results endpoint serves.
    """
    _install(session_service)
    outcome = session_service.start_session("session-one", HumanAISessionSpec())
    session_token = token_from_url(outcome.gui_url)

    session_operations.collect_session_trace(
        {"wp3_session_id": "session-one", "session_token": session_token, "traces": [1]}
    )

    stored_trace = session_manager.get("session-one").kpis
    assert "session_token" not in stored_trace
    assert "wp3_session_id" not in stored_trace
    assert stored_trace["traces"] == [1]
    set_session_service(None)


def test_single_active_session_may_omit_its_id(session_service):
    """FR-04: with exactly one session live, an unlabelled result is unambiguous.

    The InteractiveAI frontend omits the session id when the GUI URL carried no
    session_id query parameter, so this path has to keep working — but only
    while it cannot be misattributed.
    """
    _install(session_service)
    outcome = session_service.start_session("session-one", HumanAISessionSpec())
    session_token = token_from_url(outcome.gui_url)

    _, status_code = session_operations.collect_session_trace(
        {"session_token": session_token, "traces": []}
    )

    assert status_code == 200
    set_session_service(None)


def test_unlabelled_result_is_refused_when_two_sessions_are_live(session_service):
    """FR-04: ambiguity is refused rather than guessed.

    With a pool, guessing would write one participant's data onto another's
    record — which the previous single-session fallback would have done.
    """
    _install(session_service)
    outcome = session_service.start_session("session-one", HumanAISessionSpec())
    session_service.start_session("session-two", HumanAISessionSpec())
    session_token = token_from_url(outcome.gui_url)

    response_body, status_code = session_operations.collect_session_trace(
        {"session_token": session_token, "traces": []}
    )

    assert status_code == 404
    assert "ambiguous" in response_body["error"].lower()
    set_session_service(None)
