"""Session lifecycle state, held in Redis rather than in process memory.

Tracks each human-AI testing session from PENDING through COMPLETED or FAILED,
recording the phase, the participant-facing URLs, the pool slot serving it, and
the results collected from the session tools.

State moved out of the process because the control service is no longer the
thing running the session. It hands a participant a proxied link and then waits
for results to be posted back, which can be many minutes later — so a restart,
a deployment, or a crash in between must not orphan a live session or lose the
slot it holds (NFR-05).

Sessions are stored as one JSON document per session id. Read-modify-write
cycles take a short-lived Redis lock, because phase transitions are guarded by
a monotonicity rule that cannot be enforced by a blind write.

Spec coverage: FR-02, FR-21, NFR-05
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any

logger = logging.getLogger(__name__)

# Redis key holding one session's JSON document.
SESSION_KEY_PATTERN = "hai:session:{session_id}"

# Redis key of the lock guarding one session's read-modify-write cycles.
SESSION_LOCK_KEY_PATTERN = "hai:session-lock:{session_id}"

# Set membership of every session id this service has created, so active
# sessions can be enumerated without scanning the keyspace.
SESSION_INDEX_KEY = "hai:sessions"

# How long a session document is retained. Sessions carry participant data, so
# this is a data-protection setting rather than a cache tuning knob; the
# default matches the retention period recorded in the spec, pending the data
# protection officer's decision (NFR-06).
DEFAULT_SESSION_RECORD_TTL_SECONDS = 90 * 24 * 60 * 60

# Longest a phase transition may hold a session's lock. Transitions are pure
# in-memory work between two Redis round trips, so this is generous; it exists
# only so a killed process cannot wedge a session permanently.
SESSION_LOCK_TIMEOUT_SECONDS = 10


class SessionPhase(IntEnum):
    """Ordered session lifecycle phases — must only ever increase."""

    PENDING = 1      # slot reserved, tools not yet configured
    GUI_READY = 2    # slot configured; participant can open the links
    IN_PROGRESS = 3  # participant is interacting with the simulator
    SURVEY = 4       # episode ended; questionnaire outstanding
    COMPLETED = 5    # both trace and survey outcome received
    FAILED = 6       # timeout elapsed, or configuring the slot failed


TERMINAL_PHASES = frozenset({SessionPhase.COMPLETED, SessionPhase.FAILED})


@dataclass
class SessionState:
    """Everything recorded about a single testing session.

    Attributes:
        session_id: Unique identifier, also used as the orchestrator task id.
        phase: Current lifecycle phase.
        gui_url: Proxied, signed URL the participant opens for the simulator.
        survey_url: Proxied, signed URL for the questionnaire.
        slot_index: Pool slot serving this session. Replaces the Docker
            container ids the previous design recorded, since nothing creates
            containers any more.
        error_message: Populated only when the phase is FAILED.
        kpis: Grid KPIs extracted from the session trace.
        survey_outcomes: Questionnaire answers.
        session_metadata: Free-form strings attached by the caller.
        created_at: Unix timestamp of creation, used for timeout enforcement.
    """

    session_id: str
    phase: SessionPhase = SessionPhase.PENDING
    gui_url: str = ""
    survey_url: str = ""
    slot_index: int = 0
    error_message: str = ""
    kpis: dict[str, Any] = field(default_factory=dict)
    survey_outcomes: dict[str, Any] = field(default_factory=dict)
    session_metadata: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0

    def is_terminal(self) -> bool:
        """Return True if this session has reached a final phase."""
        return self.phase in TERMINAL_PHASES

    def has_all_results(self) -> bool:
        """
        Report whether both result producers have reported in.

        Completion is decided by this rather than by a thread watching a
        directory: the session trace arrives from the simulator's frontend and
        the questionnaire outcome from the survey wrapper, and the session is
        done when both have been received (FR-21).

        Returns:
            True when both the trace-derived KPIs and the survey outcomes are
            present.
        """
        return bool(self.kpis) and bool(self.survey_outcomes)

    def to_json(self) -> str:
        """Serialise this state for storage."""
        document = asdict(self)
        document["phase"] = int(self.phase)
        return json.dumps(document)

    @classmethod
    def from_json(cls, raw_document: str) -> SessionState:
        """
        Rebuild a state object from its stored form.

        Args:
            raw_document: JSON produced by ``to_json``.

        Returns:
            The reconstructed session state.

        Raises:
            ValueError: If the document is not valid JSON or is missing the
                session id.
        """
        try:
            document = json.loads(raw_document)
        except json.JSONDecodeError as decode_error:
            raise ValueError(f"Corrupt session document: {decode_error}") from decode_error

        if "session_id" not in document:
            raise ValueError("Session document has no session_id")

        document["phase"] = SessionPhase(document.get("phase", int(SessionPhase.PENDING)))
        return cls(**document)


class SessionManager:
    """Redis-backed registry of testing sessions.

    Every method is safe to call concurrently from multiple processes, not just
    multiple threads — which is the point of moving the state out of memory.
    """

    def __init__(self, redis_client, record_ttl_seconds: int = DEFAULT_SESSION_RECORD_TTL_SECONDS):
        """
        Build a manager over an existing Redis connection.

        Args:
            redis_client: Redis client, expected to decode responses to str.
            record_ttl_seconds: Retention period for session documents.
        """
        self._redis = redis_client
        self._record_ttl_seconds = record_ttl_seconds

    @property
    def redis_client(self):
        """The underlying Redis client.

        Exposed so the slot pool can share this connection rather than opening
        a second one to the same server.
        """
        return self._redis

    def create(self, session_id: str, created_at: float) -> SessionState:
        """
        Register a new session in the PENDING phase.

        Args:
            session_id: Globally unique identifier for this session.
            created_at: Unix timestamp, passed in rather than read from the
                clock so callers can make timeout behaviour deterministic in
                tests.

        Returns:
            The newly created session state.

        Raises:
            ValueError: If a session with this id already exists.
        """
        session_key = SESSION_KEY_PATTERN.format(session_id=session_id)
        state = SessionState(session_id=session_id, created_at=created_at)

        # NX makes creation atomic: two callers racing on the same id cannot
        # both believe they created it.
        was_created = self._redis.set(
            session_key, state.to_json(), nx=True, ex=self._record_ttl_seconds
        )
        if not was_created:
            raise ValueError(f"Session already exists: {session_id}")

        self._redis.sadd(SESSION_INDEX_KEY, session_id)
        logger.info("Created session %s", session_id)
        return state

    def advance_phase(
        self,
        session_id: str,
        new_phase: SessionPhase,
        *,
        gui_url: str = "",
        survey_url: str = "",
        slot_index: int = 0,
        error_message: str = "",
        kpis: dict[str, Any] | None = None,
        survey_outcomes: dict[str, Any] | None = None,
        session_metadata: dict[str, str] | None = None,
    ) -> SessionState:
        """
        Transition a session to a new phase, updating fields atomically.

        Phases may only increase, except FAILED which is reachable from
        anywhere. The check and the write happen under a per-session lock, so
        two results arriving at the same moment cannot interleave into a lost
        update.

        Args:
            session_id: Session to update.
            new_phase: Target phase.
            gui_url: Participant URL for the simulator.
            survey_url: Participant URL for the questionnaire.
            slot_index: Pool slot serving this session.
            error_message: Failure reason; only meaningful with FAILED.
            kpis: KPIs extracted from the session trace.
            survey_outcomes: Questionnaire answers.
            session_metadata: Extra strings to merge into the record.

        Returns:
            The updated session state.

        Raises:
            KeyError: If the session does not exist.
            ValueError: If the transition would move the phase backwards.
        """
        lock_key = SESSION_LOCK_KEY_PATTERN.format(session_id=session_id)

        with self._redis.lock(lock_key, timeout=SESSION_LOCK_TIMEOUT_SECONDS):
            state = self._read(session_id)
            if state is None:
                raise KeyError(f"Session not found: {session_id}")

            if new_phase != SessionPhase.FAILED and new_phase < state.phase:
                raise ValueError(
                    f"Session {session_id}: cannot transition from "
                    f"{state.phase.name} to {new_phase.name} (phase must not decrease)"
                )

            state.phase = new_phase
            if gui_url:
                state.gui_url = gui_url
            if survey_url:
                state.survey_url = survey_url
            if slot_index:
                state.slot_index = slot_index
            if error_message:
                state.error_message = error_message
            if kpis is not None:
                state.kpis = kpis
            if survey_outcomes is not None:
                state.survey_outcomes = survey_outcomes
            if session_metadata is not None:
                state.session_metadata.update(session_metadata)

            self._write(state)
            return state

    def record_result(
        self,
        session_id: str,
        *,
        kpis: dict[str, Any] | None = None,
        survey_outcomes: dict[str, Any] | None = None,
    ) -> SessionState:
        """
        Attach a result from one producer and complete the session if both arrived.

        This is the event that replaces the polling thread: whichever of the
        two producers reports last moves the session to COMPLETED, and the
        caller releases the slot (FR-21).

        Args:
            session_id: Session the result belongs to.
            kpis: KPIs from the session trace, when that is the producer.
            survey_outcomes: Questionnaire answers, when that is the producer.

        Returns:
            The updated session state, whose phase is COMPLETED when both
            results are now present.

        Raises:
            KeyError: If the session does not exist.
        """
        lock_key = SESSION_LOCK_KEY_PATTERN.format(session_id=session_id)

        with self._redis.lock(lock_key, timeout=SESSION_LOCK_TIMEOUT_SECONDS):
            state = self._read(session_id)
            if state is None:
                raise KeyError(f"Session not found: {session_id}")

            if kpis is not None:
                state.kpis = kpis
            if survey_outcomes is not None:
                state.survey_outcomes = survey_outcomes

            if state.has_all_results():
                state.phase = SessionPhase.COMPLETED
            elif state.phase < SessionPhase.SURVEY:
                # One producer has reported; the participant is at least past
                # the simulation stage.
                state.phase = SessionPhase.SURVEY

            self._write(state)
            logger.info(
                "Recorded result for session %s; phase is now %s",
                session_id,
                state.phase.name,
            )
            return state

    def get(self, session_id: str) -> SessionState | None:
        """
        Look up a session.

        Args:
            session_id: Session to look up.

        Returns:
            The session state, or None when unknown. The returned object is a
            fresh deserialisation, so mutating it affects nothing until it is
            written back through another method.
        """
        return self._read(session_id)

    def active_session_ids(self) -> list[str]:
        """
        List sessions that have not reached a terminal phase.

        Returns:
            Session ids in no particular order. Ids whose documents have
            expired out of Redis are pruned from the index as a side effect.
        """
        active_session_ids = []
        expired_session_ids = []

        for session_id in self._redis.smembers(SESSION_INDEX_KEY) or []:
            state = self._read(session_id)
            if state is None:
                expired_session_ids.append(session_id)
                continue
            if not state.is_terminal():
                active_session_ids.append(session_id)

        if expired_session_ids:
            self._redis.srem(SESSION_INDEX_KEY, *expired_session_ids)

        return active_session_ids

    def is_session_active(self, session_id: str) -> bool:
        """
        Report whether a session exists and is not terminal.

        Used as the predicate for slot reconciliation at startup, so a slot
        held by a session that finished or vanished is reclaimed.

        Args:
            session_id: Session to check.

        Returns:
            True only if the session exists and is still running.
        """
        state = self._read(session_id)
        return state is not None and not state.is_terminal()

    def get_active_session_id(self) -> str | None:
        """
        Return the single active session's id, when exactly one is active.

        The collect endpoints accept a result without a session id only when
        there is no ambiguity about which session it belongs to. With a pool of
        slots more than one session can be live at once, so unlike the previous
        single-session design this deliberately returns None when several are
        active rather than guessing.

        Returns:
            The active session id, or None when none or several are active.
        """
        active_session_ids = self.active_session_ids()
        if len(active_session_ids) == 1:
            return active_session_ids[0]
        return None

    def _read(self, session_id: str) -> SessionState | None:
        """Load and deserialise one session document, or None if absent."""
        raw_document = self._redis.get(SESSION_KEY_PATTERN.format(session_id=session_id))
        if raw_document is None:
            return None

        try:
            return SessionState.from_json(raw_document)
        except ValueError:
            logger.exception("Discarding unreadable document for session %s", session_id)
            return None

    def _write(self, state: SessionState) -> None:
        """Persist a session document, refreshing its retention window."""
        self._redis.set(
            SESSION_KEY_PATTERN.format(session_id=state.session_id),
            state.to_json(),
            ex=self._record_ttl_seconds,
        )


# ---------------------------------------------------------------------------
# Process-wide accessor. Built lazily so importing this module never opens a
# connection — which keeps it importable in tests that supply their own client.
# ---------------------------------------------------------------------------
_session_manager: SessionManager | None = None


def build_redis_client():
    """
    Connect to the session store described by the environment.

    Returns:
        A Redis client decoding responses to str, which the rest of this module
        assumes.

    Raises:
        ImportError: If the redis package is unavailable.
    """
    import redis  # imported here so the module stays importable without it

    redis_url = os.environ.get("HAI_REDIS_URL", "redis://hai-redis:6379/0")
    return redis.Redis.from_url(redis_url, decode_responses=True)


def set_session_manager(manager: SessionManager | None) -> None:
    """
    Install a manager instance for this process.

    Args:
        manager: Manager to use, or None to clear it so the next call to
            ``get_session_manager`` builds a fresh one. Tests use this to inject
            a manager backed by a fake client.
    """
    global _session_manager
    _session_manager = manager


def get_session_manager() -> SessionManager:
    """
    Return the process-wide session manager, building it on first use.

    Returns:
        The shared SessionManager.
    """
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager(build_redis_client())
    return _session_manager
