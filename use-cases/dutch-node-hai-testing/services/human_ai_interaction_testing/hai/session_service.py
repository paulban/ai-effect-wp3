"""Session lifecycle operations, independent of how they are invoked.

Starting a session used to mean creating two containers through the Docker
socket, binding them to fixed host ports, and starting a thread that watched a
shared host directory for result files to appear. This module replaces all of
that:

* a slot is *reserved* from a pool of containers that are already running,
* it is configured over HTTP,
* the participant receives signed, proxied links,
* results arrive by being posted back, and the last one to arrive completes the
  session.

Nothing here touches Docker, the filesystem layout of the host, or a polling
loop. The transports that call into it — the orchestrator's HTTP control plane
and the gRPC data plane — live in ``session_operations`` and share this one
implementation, so the two cannot drift apart.

Spec coverage: FR-01, FR-05, FR-06, FR-07, FR-08, FR-10, FR-20, FR-21, FR-22
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from common.artifacts import FileArtifactStore

from .session_manager import (
    SessionManager,
    SessionPhase,
    SessionState,
    get_session_manager,
)
from .session_tokens import build_session_url, issue_token, verify_token
from .slot_pool import SessionSlot, SlotPool, build_slot_pool

logger = logging.getLogger(__name__)

# Public base URL of this node, as a participant's browser sees it. Every
# session link is built from it, so it must be the proxy's address and not an
# internal Docker name — getting this wrong is what made the previous design's
# host.docker.internal URLs unreachable.
PUBLIC_BASE_URL: str = os.environ.get("HAI_PUBLIC_BASE_URL", "http://localhost:8443")

# Default wall-clock budget for a session when the caller does not set one.
DEFAULT_SESSION_TIMEOUT_SECONDS = 90 * 60

# How long to wait on a slot's configuration endpoints. Configuring a simulator
# includes building a grid2op environment, which is slow; the survey wrapper is
# trivial by comparison but shares the timeout for simplicity.
SLOT_CONFIGURE_TIMEOUT_SECONDS = float(os.environ.get("HAI_SLOT_CONFIGURE_TIMEOUT", "120"))

# Logical format recorded on the stored result artifact.
SESSION_RESULT_FORMAT = "HumanAISessionResult"


@dataclass
class HumanAISessionSpec:
    """What the caller asked for when starting a session.

    Mirrors the fields of the proto message of the same name, but as a plain
    dataclass so this module does not depend on generated protobuf types and
    can be exercised without compiling them.

    Attributes:
        scenario_name: Grid scenario the simulator should load.
        agent_name: Assistant/agent the simulator should run alongside.
        survey_id: Questionnaire the survey wrapper should present.
        kpis: KPI names the caller wants extracted from the trace.
        session_timeout_seconds: Wall-clock budget before the session is
            failed and its slot reclaimed.
    """

    scenario_name: str = ""
    agent_name: str = ""
    survey_id: str = ""
    kpis: list[str] = field(default_factory=list)
    session_timeout_seconds: int = DEFAULT_SESSION_TIMEOUT_SECONDS

    @classmethod
    def from_mapping(cls, source: dict[str, Any]) -> HumanAISessionSpec:
        """
        Build a spec from a decoded JSON payload.

        Accepts both the nested shape the proto uses (``{"scenario": {"name":
        ...}}``) and a flat shape, because the HTTP control plane carries
        whatever the workflow author put in the inline input.

        Args:
            source: Decoded request payload.

        Returns:
            The parsed spec, with defaults for anything absent.
        """
        scenario = source.get("scenario") or {}
        agent = source.get("agent") or {}
        survey = source.get("survey") or {}

        timeout_seconds = int(
            source.get("session_timeout_seconds") or DEFAULT_SESSION_TIMEOUT_SECONDS
        )

        return cls(
            scenario_name=scenario.get("name") or source.get("scenario_name", ""),
            agent_name=agent.get("name") or source.get("agent_name", ""),
            survey_id=survey.get("survey_id") or source.get("survey_id", ""),
            kpis=list(source.get("kpis") or []),
            session_timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class SessionStartOutcome:
    """Result of attempting to start a session.

    Attributes:
        succeeded: Whether a session is now running.
        session_id: Identifier of the session, present on success.
        gui_url: Signed, proxied link to the simulator.
        survey_url: Signed, proxied link to the questionnaire.
        message: Human-readable explanation, populated on failure.
        slots_busy: How many slots were occupied, when the failure was
            exhaustion. Lets the caller report RESOURCE_EXHAUSTED with a number
            rather than a bare refusal (FR-10).
    """

    succeeded: bool
    session_id: str = ""
    gui_url: str = ""
    survey_url: str = ""
    message: str = ""
    slots_busy: int = 0


class SessionService:
    """Starts, observes and finishes human-AI testing sessions."""

    def __init__(
        self,
        session_manager: SessionManager,
        slot_pool: SlotPool,
        artifact_store: FileArtifactStore,
        public_base_url: str = PUBLIC_BASE_URL,
    ) -> None:
        """
        Wire the service to its collaborators.

        Args:
            session_manager: Where session state is kept.
            slot_pool: Pool of running session containers.
            artifact_store: Where completed results are written for retrieval.
            public_base_url: Base URL participants reach this node at.
        """
        self._sessions = session_manager
        self._slots = slot_pool
        self._artifacts = artifact_store
        self._public_base_url = public_base_url

    # -- starting ----------------------------------------------------------

    def start_session(self, session_id: str, spec: HumanAISessionSpec) -> SessionStartOutcome:
        """
        Reserve a slot, configure it, and hand back participant links.

        On any failure after the slot is reserved, the slot is released and the
        session marked FAILED, so a configuration error costs one session
        rather than permanently shrinking the pool.

        Args:
            session_id: Identifier for the new session; also the orchestrator
                task id, so status and output can be looked up by it.
            spec: What to configure the session with.

        Returns:
            The outcome, carrying either the participant links or the reason no
            session could be started.
        """
        slot = self._slots.reserve(session_id)
        if slot is None:
            busy_slots = self._slots.busy_count()
            return SessionStartOutcome(
                succeeded=False,
                slots_busy=busy_slots,
                message=(
                    f"All {busy_slots} session slots are in use. "
                    "Wait for a session to finish, or increase HAI_SESSION_SLOT_COUNT."
                ),
            )

        try:
            self._sessions.create(session_id, created_at=time.time())
        except ValueError:
            self._slots.release(session_id)
            return SessionStartOutcome(
                succeeded=False,
                message=f"Session already exists: {session_id}",
            )

        # Issued once and reused for the participant's links and for the survey
        # wrapper's callback, so every artefact of this session carries the same
        # credential and expires together with it.
        session_token = issue_token(session_id, lifetime_seconds=spec.session_timeout_seconds)

        try:
            self._configure_slot(slot, session_id, spec, session_token)
        except Exception as configuration_error:  # noqa: BLE001 - reported, not raised
            logger.exception("Failed to configure slot %d for session %s", slot.index, session_id)
            self._abandon_session(session_id, slot, str(configuration_error))
            return SessionStartOutcome(
                succeeded=False,
                session_id=session_id,
                message=f"Could not prepare a session slot: {configuration_error}",
            )

        gui_url = build_session_url(self._public_base_url, session_id, "gui", session_token)
        survey_url = build_session_url(self._public_base_url, session_id, "survey", session_token)

        self._sessions.advance_phase(
            session_id,
            SessionPhase.GUI_READY,
            gui_url=gui_url,
            survey_url=survey_url,
            slot_index=slot.index,
            session_metadata={
                "scenario_name": spec.scenario_name,
                "agent_name": spec.agent_name,
                "survey_id": spec.survey_id,
                # Recorded so the timeout sweep can enforce the budget this
                # caller asked for rather than a service-wide default.
                "timeout_seconds": str(spec.session_timeout_seconds),
            },
        )

        logger.info(
            "Session %s started on slot %d (scenario=%s)",
            session_id,
            slot.index,
            spec.scenario_name or "default",
        )
        return SessionStartOutcome(
            succeeded=True,
            session_id=session_id,
            gui_url=gui_url,
            survey_url=survey_url,
            message="Session ready. Send the participant to gui_url, then survey_url.",
        )

    def _configure_slot(
        self,
        slot: SessionSlot,
        session_id: str,
        spec: HumanAISessionSpec,
        session_token: str,
    ) -> None:
        """
        Bind a reserved slot's containers to this session.

        Args:
            slot: The reserved slot.
            session_id: Session being started.
            spec: Configuration to apply.
            session_token: Token the survey wrapper presents when posting its
                outcome back, so the collect endpoint can authorise it.

        Raises:
            httpx.HTTPError: If either container is unreachable or rejects the
                configuration. The caller turns this into a failed session.
        """
        simulator_request: dict[str, Any] = {"session_id": session_id}
        if spec.scenario_name:
            simulator_request["scenario_name"] = spec.scenario_name
        if spec.agent_name:
            simulator_request["assistant_path"] = spec.agent_name

        with httpx.Client(timeout=SLOT_CONFIGURE_TIMEOUT_SECONDS) as http_client:
            simulator_response = http_client.post(
                f"{slot.simulator_url}/hai/session", json=simulator_request
            )
            simulator_response.raise_for_status()

            survey_response = http_client.post(
                f"{slot.survey_url}/api/session",
                json={
                    "session_id": session_id,
                    "survey_id": spec.survey_id,
                    "collect_url": self._survey_collect_url(),
                    "session_token": session_token,
                },
            )
            survey_response.raise_for_status()

    def _survey_collect_url(self) -> str:
        """Return the URL the survey wrapper posts its outcome to."""
        internal_base = os.environ.get("HAI_INTERNAL_BASE_URL", "http://hai-testing-service:8080")
        return f"{internal_base.rstrip('/')}/collect/survey-outcome"

    def _abandon_session(self, session_id: str, slot: SessionSlot, reason: str) -> None:
        """
        Mark a session failed and return its slot to the pool.

        Args:
            session_id: Session to abandon.
            slot: Slot it had reserved.
            reason: Failure reason recorded on the session.
        """
        try:
            self._sessions.advance_phase(
                session_id, SessionPhase.FAILED, error_message=reason, slot_index=slot.index
            )
        except KeyError:
            logger.warning("Abandoning unknown session %s", session_id)

        self._reset_slot(slot)
        self._slots.release(session_id)

    def _reset_slot(self, slot: SessionSlot) -> None:
        """
        Ask a slot's containers to return to their idle state.

        Failures are logged rather than raised: the slot is being released
        either way, and a container that cannot be reset is a problem for the
        next reservation to detect, not a reason to fail the current caller.

        Args:
            slot: Slot to reset.
        """
        with httpx.Client(timeout=SLOT_CONFIGURE_TIMEOUT_SECONDS) as http_client:
            for reset_url in (f"{slot.simulator_url}/hai/reset", f"{slot.survey_url}/api/reset"):
                try:
                    http_client.post(reset_url).raise_for_status()
                except httpx.HTTPError as reset_error:
                    logger.warning("Could not reset %s: %s", reset_url, reset_error)

    # -- results -----------------------------------------------------------

    def record_session_trace(self, session_id: str, trace: dict[str, Any]) -> SessionState:
        """
        Store the simulator trace posted by the participant's browser.

        Args:
            session_id: Session the trace belongs to.
            trace: Full trace document.

        Returns:
            The updated session state.

        Raises:
            KeyError: If the session does not exist.
        """
        state = self._sessions.record_result(session_id, kpis=trace)
        self._finalise_if_complete(state)
        return state

    def record_survey_outcome(self, session_id: str, outcome: dict[str, Any]) -> SessionState:
        """
        Store the questionnaire outcome posted by the survey wrapper.

        Args:
            session_id: Session the outcome belongs to.
            outcome: Questionnaire answers.

        Returns:
            The updated session state.

        Raises:
            KeyError: If the session does not exist.
        """
        state = self._sessions.record_result(session_id, survey_outcomes=outcome)
        self._finalise_if_complete(state)
        return state

    def _finalise_if_complete(self, state: SessionState) -> None:
        """
        Write the combined artifact and free the slot once both results arrived.

        Args:
            state: Session state as of the most recent result.
        """
        if state.phase != SessionPhase.COMPLETED:
            return

        self._artifacts.store_json(
            state.session_id,
            {
                "session_id": state.session_id,
                "kpis": state.kpis,
                "survey_outcomes": state.survey_outcomes,
                "metadata": state.session_metadata,
            },
            data_format=SESSION_RESULT_FORMAT,
        )

        slot = self._slots.slot_for_session(state.session_id)
        if slot is not None:
            self._reset_slot(slot)
        self._slots.release(state.session_id)

        logger.info("Session %s completed; slot released", state.session_id)

    # -- timeouts ----------------------------------------------------------

    def expire_timed_out_sessions(self, now: float | None = None) -> list[str]:
        """
        Fail sessions that have outlived their budget and reclaim their slots.

        A participant who abandons a session would otherwise hold a slot until
        the reservation's backstop expiry, which is measured in hours. This is
        what keeps the pool usable (FR-06, NFR-07).

        Args:
            now: Current Unix timestamp; injectable so tests need not sleep.

        Returns:
            Ids of the sessions that were failed.
        """
        current_time = time.time() if now is None else now
        expired_session_ids = []

        for session_id in self._sessions.active_session_ids():
            state = self._sessions.get(session_id)
            if state is None:
                continue

            budget_seconds = int(
                state.session_metadata.get("timeout_seconds", DEFAULT_SESSION_TIMEOUT_SECONDS)
            )
            if state.created_at + budget_seconds > current_time:
                continue

            self._sessions.advance_phase(
                session_id,
                SessionPhase.FAILED,
                error_message=f"Session exceeded its {budget_seconds}s budget",
            )
            slot = self._slots.slot_for_session(session_id)
            if slot is not None:
                self._reset_slot(slot)
            self._slots.release(session_id)
            expired_session_ids.append(session_id)
            logger.info("Session %s timed out; slot reclaimed", session_id)

        return expired_session_ids

    def reconcile_slots_at_startup(self) -> list[int]:
        """
        Reclaim slots left reserved by a previous process.

        Returns:
            Indices of the reclaimed slots.
        """
        return self._slots.reconcile(self._sessions.is_session_active)

    # -- authorisation -----------------------------------------------------

    def get_session(self, session_id: str) -> SessionState | None:
        """
        Look up a session's state.

        The control plane reads through this rather than reaching for the
        session store directly, so there is one path to session state and one
        object that owns it.

        Args:
            session_id: Session to look up.

        Returns:
            The session state, or None when unknown.
        """
        return self._sessions.get(session_id)

    def build_result_reference(self, session_id: str, self_url: str) -> dict[str, Any] | None:
        """
        Build the fetchable reference to a completed session's results.

        Args:
            session_id: Session to reference.
            self_url: Base URL the fetching party reaches this service at.

        Returns:
            A DataReference mapping, or None when the session has not completed
            or stored no artifact.
        """
        state = self._sessions.get(session_id)
        if state is None or state.phase != SessionPhase.COMPLETED:
            return None

        return self._artifacts.build_reference(session_id, self_url)

    def authorize_session_access(
        self,
        session_id: str,
        tool: str,
        presented_token: str | None,
    ) -> str | None:
        """
        Decide whether a request may reach a session's tool, and which slot serves it.

        Called by the proxy for every participant request, before anything
        reaches a session container. Returning the upstream address here is
        what lets the proxy route by *session* while the slots themselves stay
        statically declared — so the proxy needs no access to the Docker
        daemon to discover them (FR-14, FR-15).

        Args:
            session_id: Session named in the request path.
            tool: Which tool is being addressed — "gui" or "survey".
            presented_token: Token from the query string or session cookie.

        Returns:
            The upstream ``host:port`` to proxy to, or None when the request is
            not authorised — an unknown or finished session, an unknown tool,
            or a missing, forged or expired token.
        """
        if tool not in ("gui", "survey"):
            return None

        state = self._sessions.get(session_id)
        if state is None or state.is_terminal():
            return None

        verification = verify_token(session_id, presented_token or "")
        if not verification.is_valid:
            logger.warning(
                "Denied %s access to session %s: token %s",
                tool,
                session_id,
                verification.reason,
            )
            return None

        slot = self._slots.slot_for_session(session_id)
        if slot is None:
            logger.warning("Session %s holds no slot; denying access", session_id)
            return None

        upstream_url = slot.simulator_url if tool == "gui" else slot.survey_url
        return upstream_url.removeprefix("http://").removeprefix("https://")

    def resolve_result_session(
        self,
        declared_session_id: str | None,
        presented_token: str | None,
    ) -> tuple[str | None, str]:
        """
        Decide which session an incoming result belongs to, and whether to accept it.

        Results are posted by tools running in the participant's browser, which
        cannot hold the service API key, so the session token is what
        authorises them (FR-04).

        Args:
            declared_session_id: Session id claimed in the request body.
            presented_token: Session token supplied with the request.

        Returns:
            A tuple of (session_id, rejection_reason). When session_id is None,
            rejection_reason explains why; when it is set, the caller may
            proceed and rejection_reason is empty.
        """
        session_id = declared_session_id or self._sessions.get_active_session_id()

        if session_id is None:
            return None, (
                "No session_id supplied and the active session is ambiguous. "
                "Include wp3_session_id in the request body."
            )

        if self._sessions.get(session_id) is None:
            return None, f"Session not found: {session_id}"

        verification = verify_token(session_id, presented_token or "")
        if not verification.is_valid:
            logger.warning(
                "Rejected result for session %s: token %s", session_id, verification.reason
            )
            return None, "Invalid or expired session token"

        return session_id, ""


# ---------------------------------------------------------------------------
# Process-wide accessor, built lazily so importing this module opens no sockets.
# ---------------------------------------------------------------------------
_session_service: SessionService | None = None


def set_session_service(service: SessionService | None) -> None:
    """
    Install a service instance for this process.

    Args:
        service: Service to use, or None to clear it so the next call builds a
            fresh one. Tests inject a service backed by fakes.
    """
    global _session_service
    _session_service = service


def get_session_service() -> SessionService:
    """
    Return the process-wide session service, building it on first use.

    Returns:
        The shared SessionService.
    """
    global _session_service
    if _session_service is None:
        session_manager = get_session_manager()
        _session_service = SessionService(
            session_manager,
            build_slot_pool(session_manager.redis_client),
            FileArtifactStore(os.environ.get("HAI_ARTIFACT_DIR", "/artifacts")),
        )
    return _session_service
