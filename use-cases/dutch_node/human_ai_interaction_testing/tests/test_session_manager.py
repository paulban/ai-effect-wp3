"""Unit tests for SessionManager — phase transitions and state tracking.

Tests map directly to acceptance criteria AC-FR-02 (monotonic phase progression)
and the session state invariants required by FR-03 and FR-10.

Spec coverage: FR-02, FR-03, FR-10
"""

from __future__ import annotations

import pytest

import sys
from pathlib import Path

# Allow importing common modules without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.session_manager import (
    SessionManager,
    SessionPhase,
    TERMINAL_PHASES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def manager() -> SessionManager:
    """Return a fresh SessionManager for each test (not the global singleton)."""
    return SessionManager()


# ---------------------------------------------------------------------------
# Creation tests
# ---------------------------------------------------------------------------

def test_create_registers_session_in_pending_phase(manager: SessionManager) -> None:
    """Tests that a new session starts in PENDING phase (pre-condition for FR-02)."""
    state = manager.create("session-001")
    assert state.phase == SessionPhase.PENDING
    assert state.session_id == "session-001"
    assert state.gui_url == ""
    assert state.error_message == ""


def test_create_raises_on_duplicate_session_id(manager: SessionManager) -> None:
    """Tests that registering the same session_id twice raises ValueError."""
    manager.create("session-dup")
    with pytest.raises(ValueError, match="already exists"):
        manager.create("session-dup")


# ---------------------------------------------------------------------------
# Phase progression tests — AC-FR-02
# ---------------------------------------------------------------------------

def test_phase_advances_monotonically_through_full_lifecycle(manager: SessionManager) -> None:
    """Tests that phase progresses PENDING → GUI_READY → IN_PROGRESS → SURVEY → COMPLETED.

    Verifies AC-FR-02: 'phase progresses monotonically and never goes backwards'.
    """
    session_id = "session-lifecycle"
    manager.create(session_id)

    ordered_phases = [
        (SessionPhase.GUI_READY,   {"gui_url": "http://host:8090"}),
        (SessionPhase.IN_PROGRESS, {}),
        (SessionPhase.SURVEY,      {}),
        (SessionPhase.COMPLETED,   {"kpis": {"steps": 42}, "survey_outcomes": {"trust": 4}}),
    ]

    for target_phase, kwargs in ordered_phases:
        manager.advance_phase(session_id, target_phase, **kwargs)
        state = manager.get(session_id)
        assert state.phase == target_phase, (
            f"Expected {target_phase.name}, got {state.phase.name}"
        )


def test_advance_phase_raises_on_backwards_transition(manager: SessionManager) -> None:
    """Tests that attempting to go backwards raises ValueError (AC-FR-02 invariant)."""
    session_id = "session-backward"
    manager.create(session_id)
    manager.advance_phase(session_id, SessionPhase.IN_PROGRESS)

    with pytest.raises(ValueError, match="phase must increase"):
        manager.advance_phase(session_id, SessionPhase.GUI_READY)


def test_advance_phase_raises_on_same_phase(manager: SessionManager) -> None:
    """Tests that transitioning to the same phase raises ValueError."""
    session_id = "session-same"
    manager.create(session_id)
    manager.advance_phase(session_id, SessionPhase.GUI_READY)

    with pytest.raises(ValueError, match="phase must increase"):
        manager.advance_phase(session_id, SessionPhase.GUI_READY)


def test_failed_phase_is_always_allowed_regardless_of_current_phase(
    manager: SessionManager,
) -> None:
    """Tests that FAILED can be reached from any non-terminal phase (FR-10 requirement)."""
    for starting_phase in [
        SessionPhase.PENDING,
        SessionPhase.GUI_READY,
        SessionPhase.IN_PROGRESS,
        SessionPhase.SURVEY,
    ]:
        fresh_manager = SessionManager()
        session_id = f"session-fail-from-{starting_phase.name}"
        fresh_manager.create(session_id)

        if starting_phase != SessionPhase.PENDING:
            fresh_manager.advance_phase(session_id, starting_phase)

        fresh_manager.advance_phase(
            session_id, SessionPhase.FAILED, error_message="timed out"
        )
        state = fresh_manager.get(session_id)
        assert state.phase == SessionPhase.FAILED
        assert state.error_message == "timed out"


# ---------------------------------------------------------------------------
# Field update tests
# ---------------------------------------------------------------------------

def test_gui_url_is_stored_at_gui_ready_transition(manager: SessionManager) -> None:
    """Tests that gui_url is correctly recorded when transitioning to GUI_READY."""
    session_id = "session-url"
    manager.create(session_id)
    manager.advance_phase(
        session_id, SessionPhase.GUI_READY, gui_url="http://host.docker.internal:8090"
    )
    state = manager.get(session_id)
    assert state.gui_url == "http://host.docker.internal:8090"


def test_results_stored_at_completed_transition(manager: SessionManager) -> None:
    """Tests that kpis and survey_outcomes are stored when COMPLETED is reached."""
    session_id = "session-results"
    manager.create(session_id)
    manager.advance_phase(session_id, SessionPhase.COMPLETED, kpis={"steps": 100}, survey_outcomes={"trust": 5})
    state = manager.get(session_id)
    assert state.kpis == {"steps": 100}
    assert state.survey_outcomes == {"trust": 5}


def test_error_message_stored_at_failed_transition(manager: SessionManager) -> None:
    """Tests that error_message is stored when FAILED is reached (FR-14 requirement)."""
    session_id = "session-err"
    manager.create(session_id)
    manager.advance_phase(
        session_id, SessionPhase.FAILED, error_message="Session timed out after 300s"
    )
    state = manager.get(session_id)
    assert state.error_message == "Session timed out after 300s"


# ---------------------------------------------------------------------------
# Active session detection tests
# ---------------------------------------------------------------------------

def test_has_active_session_false_when_no_sessions(manager: SessionManager) -> None:
    """Tests that has_active_session returns False when no sessions exist."""
    assert manager.has_active_session() is False


def test_has_active_session_true_for_pending_session(manager: SessionManager) -> None:
    """Tests that has_active_session returns True when a PENDING session exists."""
    manager.create("session-active")
    assert manager.has_active_session() is True


def test_has_active_session_false_after_completed(manager: SessionManager) -> None:
    """Tests that has_active_session returns False once the only session is COMPLETED."""
    session_id = "session-done"
    manager.create(session_id)
    manager.advance_phase(session_id, SessionPhase.COMPLETED)
    assert manager.has_active_session() is False


def test_has_active_session_false_after_failed(manager: SessionManager) -> None:
    """Tests that has_active_session returns False once the only session is FAILED."""
    session_id = "session-failed"
    manager.create(session_id)
    manager.advance_phase(session_id, SessionPhase.FAILED, error_message="error")
    assert manager.has_active_session() is False


# ---------------------------------------------------------------------------
# Get tests
# ---------------------------------------------------------------------------

def test_get_returns_none_for_unknown_session_id(manager: SessionManager) -> None:
    """Tests that get() returns None for unregistered session IDs (FR-03 guard)."""
    assert manager.get("nonexistent-session") is None


def test_get_returns_snapshot_not_live_reference(manager: SessionManager) -> None:
    """Tests that the returned state is a copy; mutating it does not affect the registry."""
    session_id = "session-copy"
    manager.create(session_id)
    state_copy = manager.get(session_id)
    state_copy.gui_url = "mutated"

    state_fresh = manager.get(session_id)
    assert state_fresh.gui_url == "", "Mutating the copy should not affect stored state"
