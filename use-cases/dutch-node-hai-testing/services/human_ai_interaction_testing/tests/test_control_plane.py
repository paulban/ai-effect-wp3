"""Control-plane tests: authentication, status, output and the proxy hook.

These cover the defect that motivated re-layering this service onto the shared
control plane. The forked control interface never read SERVICE_API_KEY, so the
``services_api_key`` the orchestrator passes per workflow was accepted by the
orchestrator and then ignored here — anyone who could reach the port could
start a session.

Spec coverage: FR-03, FR-05, FR-14, FR-28, FR-29
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from hai import control_interface
from hai.session_manager import SessionPhase
from hai.session_service import HumanAISessionSpec, set_session_service

SERVICE_API_KEY = "test-service-key"


@pytest.fixture
def authenticated_client(session_service, artifact_store, monkeypatch):
    """
    A test client for an app wired to the fixtures, with an API key configured.

    Args:
        session_service: Service the app's providers read from.
        artifact_store: Store the app serves artifacts from.
        monkeypatch: Used to point the module's artifact store at the fixture's
            directory rather than the container path.

    Returns:
        A TestClient over the assembled application.
    """
    os.environ["SERVICE_API_KEY"] = SERVICE_API_KEY
    set_session_service(session_service)
    monkeypatch.setattr(control_interface, "build_artifact_store", lambda: artifact_store)

    application = control_interface.create_app(
        {"StartHumanAISession": lambda request: None},
        collect_session_trace=lambda body: ({"status": "ok"}, 200),
        collect_survey_outcome=lambda body: ({"status": "ok"}, 200),
    )

    yield TestClient(application)

    os.environ.pop("SERVICE_API_KEY", None)
    set_session_service(None)


def test_control_execute_without_a_token_is_rejected(authenticated_client):
    """FR-03: /control/* requires the service bearer token.

    "Given SERVICE_API_KEY is set, when /control/execute is called without a
    bearer token, then it returns 401."
    """
    response = authenticated_client.post(
        "/control/execute",
        json={"method": "StartHumanAISession", "workflow_id": "w", "task_id": "t"},
    )

    assert response.status_code == 401


def test_control_execute_with_the_wrong_token_is_rejected(authenticated_client):
    """FR-03: a wrong bearer token is refused, not merely a missing one."""
    response = authenticated_client.post(
        "/control/execute",
        json={"method": "StartHumanAISession", "workflow_id": "w", "task_id": "t"},
        headers={"Authorization": "Bearer not-the-key"},
    )

    assert response.status_code == 401


def test_health_stays_unauthenticated(authenticated_client):
    """NFR: monitoring must not need a credential."""
    assert authenticated_client.get("/health").status_code == 200


def test_status_reports_the_session_phase(authenticated_client, session_service):
    """FR-29: status is read from the session store, not an in-memory task table."""
    session_service.start_session("session-one", HumanAISessionSpec())

    response = authenticated_client.get(
        "/control/status/session-one",
        headers={"Authorization": f"Bearer {SERVICE_API_KEY}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"


def test_status_of_an_unknown_session_is_not_found(authenticated_client):
    """FR-29: an unknown task id is a 404, not a fabricated pending status."""
    response = authenticated_client.get(
        "/control/status/no-such-session",
        headers={"Authorization": f"Bearer {SERVICE_API_KEY}"},
    )

    assert response.status_code == 404


def test_output_of_an_incomplete_session_is_refused(authenticated_client, session_service):
    """FR-05: a result reference is only offered once there is a result."""
    session_service.start_session("session-one", HumanAISessionSpec())

    response = authenticated_client.get(
        "/control/output/session-one",
        headers={"Authorization": f"Bearer {SERVICE_API_KEY}"},
    )

    assert response.status_code == 400


def test_completed_session_output_is_an_http_reference(authenticated_client, session_service):
    """FR-05: the output reference points at a URL the caller can fetch.

    "Given a completed session, when /control/output is called, then the
    response carries protocol http whose uri returns the artifact on a GET."
    """
    session_service.start_session("session-one", HumanAISessionSpec())
    session_service.record_session_trace("session-one", {"kpis": {"score": 1}})
    session_service.record_survey_outcome("session-one", {"answers": ["a"]})

    authorization = {"Authorization": f"Bearer {SERVICE_API_KEY}"}
    output_response = authenticated_client.get("/control/output/session-one", headers=authorization)

    assert output_response.status_code == 200
    reference = output_response.json()["output"]
    assert reference["protocol"] == "http"
    assert reference["uri"].endswith("/control/data/session-one")

    data_response = authenticated_client.get("/control/data/session-one", headers=authorization)
    assert data_response.status_code == 200
    stored_artifact = data_response.json()
    assert stored_artifact["kpis"] == {"kpis": {"score": 1}}
    assert stored_artifact["survey_outcomes"] == {"answers": ["a"]}


def test_artifact_endpoint_requires_authentication(authenticated_client, session_service):
    """FR-03: results carry participant data and are not world-readable."""
    session_service.start_session("session-one", HumanAISessionSpec())
    session_service.record_session_trace("session-one", {"kpis": {}})
    session_service.record_survey_outcome("session-one", {"answers": []})

    assert authenticated_client.get("/control/data/session-one").status_code == 401


def test_proxy_authorization_names_the_serving_slot(authenticated_client, session_service):
    """FR-14: the proxy learns the upstream from the control service.

    This is what lets session routing be dynamic while the slots stay
    statically declared, so the proxy needs no access to the Docker daemon.
    """
    outcome = session_service.start_session("session-one", HumanAISessionSpec())
    session_token = outcome.gui_url.split("?t=", 1)[1]

    response = authenticated_client.get(
        "/internal/authorize-session",
        params={"session_id": "session-one", "tool": "gui", "token": session_token},
    )

    assert response.status_code == 200
    assert response.headers["X-Slot-Upstream"] == "hai-slot-1-simulator:5000"


def test_proxy_authorization_refuses_a_tampered_token(authenticated_client, session_service):
    """FR-15: a forged link is refused before anything reaches a session container.

    "Given a valid session URL, when the signature is altered by one character,
    then the proxy returns 403 and no request reaches any session container."
    """
    outcome = session_service.start_session("session-one", HumanAISessionSpec())
    valid_token = outcome.gui_url.split("?t=", 1)[1]
    tampered_token = valid_token[:-1] + ("A" if valid_token[-1] != "A" else "B")

    response = authenticated_client.get(
        "/internal/authorize-session",
        params={"session_id": "session-one", "tool": "gui", "token": tampered_token},
    )

    assert response.status_code == 403
    assert "X-Slot-Upstream" not in response.headers


def test_proxy_authorization_refuses_a_finished_session(authenticated_client, session_service):
    """FR-15: a link stops working once its session is over.

    Without this a participant's link would keep resolving to whichever slot
    had been reassigned to the next participant.
    """
    outcome = session_service.start_session("session-one", HumanAISessionSpec())
    session_token = outcome.gui_url.split("?t=", 1)[1]
    session_service.record_session_trace("session-one", {"kpis": {}})
    session_service.record_survey_outcome("session-one", {"answers": []})

    response = authenticated_client.get(
        "/internal/authorize-session",
        params={"session_id": "session-one", "tool": "gui", "token": session_token},
    )

    assert response.status_code == 403


def test_proxy_authorization_refuses_an_unknown_tool(authenticated_client, session_service):
    """FR-14: only the two known tools are routable."""
    outcome = session_service.start_session("session-one", HumanAISessionSpec())
    session_token = outcome.gui_url.split("?t=", 1)[1]

    response = authenticated_client.get(
        "/internal/authorize-session",
        params={"session_id": "session-one", "tool": "admin", "token": session_token},
    )

    assert response.status_code == 403
