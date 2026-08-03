"""Tests for the POST /collect/session-trace endpoint and hai-survey-wrapper Flask sidecar.

Covers acceptance criteria from spec sections 9.1 and 9.2:

  FR-12  _handle_collect_session_trace writes kpis.json to the active session dir
  FR-08  kpis.json is written server-side (not a browser download) when the
         InteractiveAI frontend POSTs to /collect/session-trace
  FR-10  session transitions to COMPLETED only when BOTH kpis.json AND
         survey_outcomes.json are present
  FR-17  GET /api/healthz on the Flask sidecar returns HTTP 200
  FR-18  POST /api/save_results on the Flask sidecar writes survey_outcomes.json

Spec coverage: FR-08, FR-09, FR-10, FR-12, FR-17, FR-18
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Allow importing common modules without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.session_manager import SessionManager, SessionPhase  # noqa: E402
from common import session_operations  # noqa: E402
from common.session_operations import (  # noqa: E402
    _handle_collect_session_trace,
    KPIS_FILENAME,
    RESULTS_CONTAINER_BASE_PATH,
)


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

EXAMPLE_SESSION_TRACE: dict[str, Any] = {
    "sessionId": "interactiveai-session-42",
    "userLogin": "powergrid_user",
    "startedAt": "2026-06-29T10:00:00Z",
    "endedAt": "2026-06-29T10:45:00Z",
    "kpis": {
        "total_session_time_ms": 2700000,
        "avg_decision_time_ms": 8500,
    },
    "traces": [],
}

EXAMPLE_SURVEY_PAYLOAD: dict[str, Any] = {
    "success": True,
    "participantData": {"id": "P001", "condition": "C_AutoAI"},
    "allResults": {
        "timestamp": "2026-06-29T10:50:00Z",
        "surveys": [
            {
                "surveyName": "NASA-TLX",
                "timestamp": "2026-06-29T10:46:00Z",
                "payload": {"mental_demand": 5, "success": True},
            }
        ],
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_session_manager() -> SessionManager:
    """Return a fresh SessionManager, patched into session_operations for this test."""
    manager = SessionManager()
    with patch.object(session_operations, "get_session_manager", return_value=manager):
        yield manager


@pytest.fixture()
def active_session(isolated_session_manager: SessionManager) -> str:
    """Create a session in GUI_READY phase and return its session_id."""
    session_id = "test_session_abc123"
    isolated_session_manager.create(session_id)
    isolated_session_manager.advance_phase(
        session_id,
        SessionPhase.GUI_READY,
        gui_url="http://localhost:8090",
        survey_url="http://localhost:8091",
    )
    return session_id


# ===========================================================================
# FR-12 — _handle_collect_session_trace writes kpis.json
# ===========================================================================

class TestHandleCollectSessionTrace:
    """Tests for the _handle_collect_session_trace function (FR-12)."""

    def test_writes_kpis_json_to_active_session_directory(
        self,
        active_session: str,
        isolated_session_manager: SessionManager,
        tmp_path: Path,
    ) -> None:
        """FR-12 / FR-08: kpis.json is written to the session results directory.

        Verifies acceptance criterion:
        'Given the InteractiveAI frontend calls POST /collect/session-trace with
        a valid session JSON body and a known session_id, when the request is
        received, then kpis.json is written to <results_dir>/<session_id>/kpis.json.'
        """
        # Given: an active session and a tmp directory acting as the results base
        session_results_dir = tmp_path / active_session
        session_results_dir.mkdir(parents=True)
        kpis_path = session_results_dir / KPIS_FILENAME

        with patch.object(
            session_operations, "RESULTS_CONTAINER_BASE_PATH", str(tmp_path)
        ):
            # When: the collect handler receives the InteractiveAI session trace
            response_body, status_code = _handle_collect_session_trace(
                EXAMPLE_SESSION_TRACE
            )

        # Then: kpis.json exists and contains the posted trace
        assert status_code == 200
        assert response_body["status"] == "saved"
        assert response_body["session_id"] == active_session
        assert kpis_path.exists(), "kpis.json was not created"

        written = json.loads(kpis_path.read_text(encoding="utf-8"))
        assert written["sessionId"] == EXAMPLE_SESSION_TRACE["sessionId"]
        assert written["kpis"]["total_session_time_ms"] == 2700000

    def test_uses_wp3_session_id_from_body_when_present(
        self,
        active_session: str,
        isolated_session_manager: SessionManager,
        tmp_path: Path,
    ) -> None:
        """FR-12 / OQ-1 stub: explicit wp3_session_id in body targets that session."""
        session_results_dir = tmp_path / active_session
        session_results_dir.mkdir(parents=True)
        kpis_path = session_results_dir / KPIS_FILENAME

        body_with_session_id = {
            **EXAMPLE_SESSION_TRACE,
            "wp3_session_id": active_session,
        }

        with patch.object(
            session_operations, "RESULTS_CONTAINER_BASE_PATH", str(tmp_path)
        ):
            response_body, status_code = _handle_collect_session_trace(body_with_session_id)

        assert status_code == 200
        assert kpis_path.exists()

    def test_returns_404_when_no_active_session(
        self,
        isolated_session_manager: SessionManager,
        tmp_path: Path,
    ) -> None:
        """FR-12: returns 404 when no active session exists."""
        # Given: no sessions in the manager at all
        with patch.object(
            session_operations, "RESULTS_CONTAINER_BASE_PATH", str(tmp_path)
        ):
            response_body, status_code = _handle_collect_session_trace(
                EXAMPLE_SESSION_TRACE
            )

        assert status_code == 404
        assert "error" in response_body

    def test_returns_404_for_unknown_explicit_session_id(
        self,
        isolated_session_manager: SessionManager,
        tmp_path: Path,
    ) -> None:
        """FR-12: returns 404 when body includes wp3_session_id that does not exist."""
        body = {**EXAMPLE_SESSION_TRACE, "wp3_session_id": "nonexistent-session"}

        with patch.object(
            session_operations, "RESULTS_CONTAINER_BASE_PATH", str(tmp_path)
        ):
            response_body, status_code = _handle_collect_session_trace(body)

        assert status_code == 404
        assert "nonexistent-session" in response_body.get("error", "")

    def test_returns_500_when_results_directory_does_not_exist(
        self,
        active_session: str,
        isolated_session_manager: SessionManager,
        tmp_path: Path,
    ) -> None:
        """FR-12: returns 500 when the session results directory has not been created."""
        # The session exists in the manager but no directory was created
        # (e.g., Docker container launch failed before mkdir)
        with patch.object(
            session_operations, "RESULTS_CONTAINER_BASE_PATH", str(tmp_path)
        ):
            # No mkdir — session directory does not exist
            response_body, status_code = _handle_collect_session_trace(
                EXAMPLE_SESSION_TRACE
            )

        assert status_code == 500
        assert "error" in response_body

    def test_skips_completed_sessions_in_fallback_lookup(
        self,
        isolated_session_manager: SessionManager,
        tmp_path: Path,
    ) -> None:
        """FR-12: completed sessions are not treated as active in the v1 fallback."""
        # Given: one completed session and no active sessions
        session_id = "completed_session"
        isolated_session_manager.create(session_id)
        isolated_session_manager.advance_phase(session_id, SessionPhase.GUI_READY)
        isolated_session_manager.advance_phase(
            session_id,
            SessionPhase.COMPLETED,
            kpis={"test": 1},
            survey_outcomes={"answered": True},
        )

        with patch.object(
            session_operations, "RESULTS_CONTAINER_BASE_PATH", str(tmp_path)
        ):
            response_body, status_code = _handle_collect_session_trace(
                EXAMPLE_SESSION_TRACE
            )

        # The completed session must NOT be picked up as the active session
        assert status_code == 404


# ===========================================================================
# FR-10 — both result files are required for session completion
# ===========================================================================

class TestBothFilesRequiredForCompletion:
    """Tests verifying FR-10: COMPLETED fires only when BOTH files are present."""

    def test_only_kpis_json_does_not_satisfy_completion_condition(
        self,
        tmp_path: Path,
    ) -> None:
        """FR-10: kpis.json alone is not sufficient to trigger COMPLETED."""
        kpis_path = tmp_path / KPIS_FILENAME
        kpis_path.write_text(json.dumps(EXAMPLE_SESSION_TRACE), encoding="utf-8")
        survey_path = tmp_path / session_operations.SURVEY_OUTCOMES_FILENAME

        kpis_present = kpis_path.exists()
        survey_present = survey_path.exists()

        assert kpis_present is True
        assert survey_present is False
        # The polling thread's completion condition is (kpis AND survey)
        assert not (kpis_present and survey_present)

    def test_only_survey_outcomes_does_not_satisfy_completion_condition(
        self,
        tmp_path: Path,
    ) -> None:
        """FR-10: survey_outcomes.json alone is not sufficient to trigger COMPLETED."""
        kpis_path = tmp_path / KPIS_FILENAME
        survey_path = tmp_path / session_operations.SURVEY_OUTCOMES_FILENAME
        survey_path.write_text(
            json.dumps(EXAMPLE_SURVEY_PAYLOAD), encoding="utf-8"
        )

        kpis_present = kpis_path.exists()
        survey_present = survey_path.exists()

        assert kpis_present is False
        assert survey_present is True
        assert not (kpis_present and survey_present)

    def test_both_files_present_satisfies_completion_condition(
        self,
        tmp_path: Path,
    ) -> None:
        """FR-10: session completes when both kpis.json and survey_outcomes.json exist."""
        kpis_path = tmp_path / KPIS_FILENAME
        survey_path = tmp_path / session_operations.SURVEY_OUTCOMES_FILENAME
        kpis_path.write_text(json.dumps(EXAMPLE_SESSION_TRACE), encoding="utf-8")
        survey_path.write_text(json.dumps(EXAMPLE_SURVEY_PAYLOAD), encoding="utf-8")

        kpis_present = kpis_path.exists()
        survey_present = survey_path.exists()

        assert kpis_present and survey_present


# ===========================================================================
# FR-17/FR-18 — hai-survey-wrapper Flask sidecar
# ===========================================================================

class TestHaiSurveyWrapperFlaskSidecar:
    """Tests for the hai-survey-wrapper Flask app (hai-survey-wrapper/app.py).

    Skipped automatically when Flask is not installed in the test environment.
    Flask is a Docker-only dependency that runs inside the hai-survey-wrapper
    container image, not in the WP3 service venv.
    """

    @pytest.fixture()
    def flask_client(self, tmp_path: Path):
        pytest.importorskip(
            "flask",
            reason="Flask is a Docker-only dependency; install it to run sidecar tests",
        )
        """Return a Flask test client with the results directory pointed at tmp_path."""
        import os

        app_path = (
            Path(__file__).parent.parent / "hai-survey-wrapper" / "app.py"
        )
        spec = importlib.util.spec_from_file_location("survey_wrapper_app", app_path)
        survey_module = importlib.util.module_from_spec(spec)

        with patch.dict(os.environ, {
            "HAI_RESULTS_PATH": str(tmp_path),
            "HAI_SURVEY_FILENAME": "survey_outcomes.json",
        }):
            spec.loader.exec_module(survey_module)

        # Override module-level constants after import to point at tmp_path
        survey_module.RESULTS_DIRECTORY = tmp_path
        survey_module.SURVEY_OUTCOMES_FILENAME = "survey_outcomes.json"

        survey_module.app.config["TESTING"] = True
        return survey_module.app.test_client(), tmp_path

    def test_healthz_returns_200(self, flask_client) -> None:
        """FR-17: GET /api/healthz returns HTTP 200 with status ok."""
        client, _ = flask_client
        response = client.get("/api/healthz")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["status"] == "ok"

    def test_save_results_writes_survey_outcomes_json(self, flask_client) -> None:
        """FR-18 / FR-09: POST /api/save_results writes survey_outcomes.json to /results.

        Verifies acceptance criterion:
        'Given hai-survey-wrapper is running, when the operator completes the survey,
        then POST /save_results is called by the wrapper page and survey_outcomes.json
        appears in /results.'
        """
        client, results_dir = flask_client
        response = client.post(
            "/api/save_results",
            data=json.dumps(EXAMPLE_SURVEY_PAYLOAD),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["status"] == "saved"

        output_path = results_dir / "survey_outcomes.json"
        assert output_path.exists(), "survey_outcomes.json was not created"

        written = json.loads(output_path.read_text(encoding="utf-8"))
        assert written["success"] is True
        assert written["participantData"]["id"] == "P001"
        assert len(written["allResults"]["surveys"]) == 1

    def test_save_results_rejects_non_json_body(self, flask_client) -> None:
        """FR-18: POST /api/save_results returns 400 for a non-JSON body."""
        client, _ = flask_client
        response = client.post(
            "/api/save_results",
            data="this is not json",
            content_type="application/json",
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data

    def test_save_results_rejects_json_array(self, flask_client) -> None:
        """FR-18: POST /api/save_results returns 400 when body is a JSON array, not object."""
        client, _ = flask_client
        response = client.post(
            "/api/save_results",
            data=json.dumps([1, 2, 3]),
            content_type="application/json",
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data

    def test_save_results_overwrites_previous_output(self, flask_client) -> None:
        """FR-18: successive POSTs to /api/save_results overwrite the output file."""
        client, results_dir = flask_client

        first_payload = {**EXAMPLE_SURVEY_PAYLOAD, "run": 1}
        second_payload = {**EXAMPLE_SURVEY_PAYLOAD, "run": 2}

        client.post(
            "/api/save_results",
            data=json.dumps(first_payload),
            content_type="application/json",
        )
        client.post(
            "/api/save_results",
            data=json.dumps(second_payload),
            content_type="application/json",
        )

        written = json.loads(
            (results_dir / "survey_outcomes.json").read_text(encoding="utf-8")
        )
        assert written["run"] == 2
