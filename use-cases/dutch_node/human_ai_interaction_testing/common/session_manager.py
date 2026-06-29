"""Thread-safe session state manager for the Human-AI Interaction Testing service.

Tracks the lifecycle of each human-AI testing session from PENDING through
COMPLETED or FAILED. Each session records its current phase, both browser URLs
(gui_url for InteractiveAI, survey_url for hmisurveys), Docker container IDs
for both sub-containers, and the final parsed results once available.

Spec coverage: FR-02, FR-03, FR-07, FR-11, FR-14
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class SessionPhase(IntEnum):
    """Ordered session lifecycle phases — must only ever increase."""

    PENDING     = 1  # container not yet ready
    GUI_READY   = 2  # InteractiveAI container is up and browser-accessible
    IN_PROGRESS = 3  # operator is interacting with the GUI
    SURVEY      = 4  # grid episode ended; hmisurveys survey is displayed
    COMPLETED   = 5  # operator submitted survey; results written to volume
    FAILED      = 6  # timeout elapsed or container error


TERMINAL_PHASES = frozenset({SessionPhase.COMPLETED, SessionPhase.FAILED})


@dataclass
class SessionState:
    """All mutable state associated with a single testing session.

    Fields are mutated under the SessionManager lock; never access them
    outside a lock-protected context unless the session is terminal.
    """

    session_id: str
    phase: SessionPhase = SessionPhase.PENDING
    gui_url: str = ""           # Browser URL for the InteractiveAI grid GUI
    survey_url: str = ""        # Browser URL for the hmisurveys questionnaire
    container_id: str = ""      # Docker container ID for InteractiveAI
    survey_container_id: str = ""  # Docker container ID for hmisurveys
    volume_name: str = ""       # Host-path suffix for the shared results directory
    error_message: str = ""     # Non-empty only when phase == FAILED
    kpis: dict[str, Any] = field(default_factory=dict)
    survey_outcomes: dict[str, Any] = field(default_factory=dict)
    session_metadata: dict[str, str] = field(default_factory=dict)

    def is_terminal(self) -> bool:
        """Return True if this session has reached a final phase."""
        return self.phase in TERMINAL_PHASES


class SessionManager:
    """Thread-safe registry of all active and completed testing sessions.

    Only one session is active at a time in v1 (NFR-03). The registry
    retains completed and failed sessions so callers can still query results
    after the session ends.

    All public methods are safe to call from concurrent threads (background
    polling thread + gRPC handler threads).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def create(self, session_id: str) -> SessionState:
        """Register a new session in PENDING phase and return its state object.

        Args:
            session_id: Globally unique identifier for this session.

        Returns:
            The freshly created SessionState for the given session_id.

        Raises:
            ValueError: If a session with this session_id already exists.
        """
        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session already exists: {session_id}")
            state = SessionState(session_id=session_id)
            self._sessions[session_id] = state
            return state

    def advance_phase(
        self,
        session_id: str,
        new_phase: SessionPhase,
        *,
        gui_url: str = "",
        survey_url: str = "",
        container_id: str = "",
        survey_container_id: str = "",
        volume_name: str = "",
        error_message: str = "",
        kpis: dict[str, Any] | None = None,
        survey_outcomes: dict[str, Any] | None = None,
        session_metadata: dict[str, str] | None = None,
    ) -> None:
        """Transition a session to a new phase, updating optional fields atomically.

        Enforces monotonicity: phase may only increase (FAILED is always allowed
        as a terminal failure regardless of current numeric value).

        Args:
            session_id: Session to update.
            new_phase: Target phase.
            gui_url: InteractiveAI browser URL (recorded at GUI_READY).
            survey_url: hmisurveys browser URL (recorded at GUI_READY).
            container_id: Docker container ID for InteractiveAI (for cleanup).
            survey_container_id: Docker container ID for hmisurveys (for cleanup).
            volume_name: Host-path suffix for the shared results directory.
            error_message: Human-readable failure reason (FAILED phase only).
            kpis: Parsed grid KPI dict from kpis.json (COMPLETED only).
            survey_outcomes: Parsed survey outcome dict from survey_outcomes.json (COMPLETED only).
            session_metadata: Arbitrary string key-value metadata to attach.

        Raises:
            KeyError: If session_id is not registered.
            ValueError: If the phase transition would go backwards.
        """
        with self._lock:
            state = self._sessions[session_id]

            if new_phase != SessionPhase.FAILED and new_phase <= state.phase:
                raise ValueError(
                    f"Session {session_id}: cannot transition from "
                    f"{state.phase.name} to {new_phase.name} (phase must increase)"
                )

            state.phase = new_phase
            if gui_url:
                state.gui_url = gui_url
            if survey_url:
                state.survey_url = survey_url
            if container_id:
                state.container_id = container_id
            if survey_container_id:
                state.survey_container_id = survey_container_id
            if volume_name:
                state.volume_name = volume_name
            if error_message:
                state.error_message = error_message
            if kpis is not None:
                state.kpis = kpis
            if survey_outcomes is not None:
                state.survey_outcomes = survey_outcomes
            if session_metadata is not None:
                state.session_metadata.update(session_metadata)

    def get(self, session_id: str) -> SessionState | None:
        """Return a snapshot copy of the session state, or None if not found.

        The returned object is a shallow copy; callers must not mutate it or
        assume it stays current after the lock is released.

        Args:
            session_id: Session to look up.

        Returns:
            A copy of the SessionState, or None if session_id is unknown.
        """
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return None
            # Return a shallow copy so the caller sees a consistent snapshot
            # without holding the lock.
            import copy
            return copy.copy(state)

    def has_active_session(self) -> bool:
        """Return True if any session is currently in a non-terminal phase.

        Used to enforce the v1 constraint of one active session at a time.

        Returns:
            True if at least one non-terminal session exists.
        """
        with self._lock:
            return any(
                not state.is_terminal() for state in self._sessions.values()
            )


# ---------------------------------------------------------------------------
# Module-level singleton — one SessionManager per process.
# ---------------------------------------------------------------------------
_session_manager = SessionManager()


def get_session_manager() -> SessionManager:
    """Return the process-wide SessionManager singleton.

    Returns:
        The global SessionManager instance.
    """
    return _session_manager
