"""
Session — one human-AI interaction session, from go-live to collected result.

A session runs on a ``BatchJobRunner`` worker thread and goes through fixed
steps:

1. **preflight** — the simulator, the recommendation gateway and the test
   account must answer (FR-11);
2. **activate** — build the wrapper environment and the algorithm's agent, and
   install it as the agent InteractiveAI calls (FR-12);
3. **configure** — write the simulator's CONFIG.toml for the scenario (FR-13);
4. **go live** — fix the deadline and publish the participant's links and
   instructions as the intermediate result (FR-14);
5. **wait** — until the deadline, reporting progress (FR-15). Nothing else ends
   a session: InteractiveAI sends no "participant finished" signal;
6. **collect** — remove the agent, fetch the traces of the session window and
   assemble the result (FR-16a, FR-20).

Only one session runs at a time, because the simulator, the agent slot in
cabrecommendation and the test account are shared, and traces can only be told
apart by time. ``SessionCoordinator.reserve`` enforces that before a job is
submitted, so a second request is refused with the running session's deadline
(FR-05).

Spec coverage: FR-05, FR-06, FR-08, FR-09, FR-10, FR-14, FR-15, FR-16a,
sections 5.4 and 5.7
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from agent_wrapper import ActiveAgent, AgentRegistry, activate_algorithm
from common.concurrent import _check_api_key
from interactiveai_client import InteractiveAIClient
from session_config import SessionConfig
from session_warnings import SessionWarnings
from settings import (
    MAXIMUM_PROGRESS_WHILE_RUNNING,
    PROGRESS_REPORT_INTERVAL_SECONDS,
    SERVICE_NAME,
    HAISettings,
)
from simulator_config import render_simulator_config, write_simulator_config_in_place

logger = logging.getLogger(__name__)

# Recorded in every result: the traces API cannot filter by user or session (IAI-4).
TRACE_FILTER_WARNING = (
    "traces are filtered by time window and use case only; any other use of the "
    "test account in that window is included"
)

# Why survey, session export and KPIs are absent (section 5.7).
NOT_COLLECTED_EXPLANATIONS = {
    "survey": "InteractiveAI only downloads survey answers in the participant's browser (see spec 6.3, IAI-1).",
    "session_export": "InteractiveAI only downloads the session export in the participant's browser (IAI-1).",
    "kpis": "Not computed in this version.",
}

# Participant instructions (section 5.4), formatted with the session's values.
INSTRUCTION_TEMPLATES = (
    "Open the simulator at {simulator_url}. Log in as {username} / {password} and choose the server {simulator_server}.",
    "The simulation loads the prepared scenario. Do not change the scenario, seed or settings in the "
    "simulator dashboard. Click Start.",
    "Open InteractiveAI at {frontend_url} in a second tab and log in with the same account. "
    "Choose the recommendation agent when asked.",
    "When the simulator pauses on an event, decide in InteractiveAI (apply a recommendation or act "
    "yourself), then click Resume in the simulator.",
    "Play until the scenario ends, then log out of InteractiveAI and complete the questionnaire that opens.",
    "Finish before {deadline}. Results are collected automatically at that time.",
)


def format_utc(moment: datetime) -> str:
    """
    Format a moment as ISO-8601 UTC with a ``Z`` suffix.

    Args:
        moment: A timezone-aware datetime.

    Returns:
        E.g. ``2026-11-11T10:00:00Z``.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _installed_version(package_name: str) -> str:
    """Return an installed package's version, or 'unknown'."""
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unknown"


class SessionClock:
    """
    Wall clock and interruptible wait; replaced by a fake clock in tests.
    """

    def now(self) -> datetime:
        """Return the current time in UTC."""
        return datetime.now(timezone.utc)

    def wait(self, stop_event: threading.Event, seconds: float) -> bool:
        """
        Wait up to ``seconds``, returning early if the event is set.

        Returns:
            True if the event was set, False if the time elapsed.
        """
        return stop_event.wait(seconds)


@dataclass
class ActiveSession:
    """
    The session holding the service's single slot.

    Attributes:
        job_id: Job id returned to the orchestrator.
        deadline: When results are collected; None until the session is live.
        stop_event: Set to end the wait early (shutdown).
    """

    job_id: str
    deadline: datetime | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)


def build_intermediate_result(
    job_id: str, config: SessionConfig, settings: HAISettings, started_at: datetime, deadline: datetime
) -> dict[str, Any]:
    """
    Build what the participant needs, handed to the orchestrator mid-session (section 5.4).

    TODO(orchestrator): the orchestrator does not yet consume intermediate
    results. Until it does, this object is served at
    ``GET /control/intermediate/{task_id}``, logged at go-live and stored in the
    final result's ``session.intermediate``. When the orchestrator gains an
    intermediate-result channel, this is the payload to send through it (FR-10).

    Args:
        job_id: The session's job id.
        config: The validated session request.
        settings: Public URLs and test account.
        started_at: Go-live time, UTC.
        deadline: Collection time, UTC.

    Returns:
        The intermediate object.
    """
    template_values = {
        "simulator_url": settings.simulator_public_url,
        "frontend_url": settings.frontend_public_url,
        "username": settings.cab_username,
        "password": settings.cab_password,
        "simulator_server": settings.simulator_cab_server,
        "deadline": format_utc(deadline),
    }
    return {
        "task_id": job_id,
        "frontend_url": settings.frontend_public_url,
        "simulator_url": settings.simulator_public_url,
        "credentials": {"username": settings.cab_username, "password": settings.cab_password},
        "simulator_server": settings.simulator_cab_server,
        "started_at": format_utc(started_at),
        "deadline": format_utc(deadline),
        "scenario": {"env_name": config.scenario.env_name, "scenario_name": config.scenario.scenario_name},
        "instructions": [template.format(**template_values) for template in INSTRUCTION_TEMPLATES],
    }


def build_session_result(
    job_id: str,
    config: SessionConfig,
    settings: HAISettings,
    session_times: dict[str, datetime],
    intermediate: dict[str, Any],
    traces: list[Any],
    trace_query: dict[str, str],
    session_warnings: SessionWarnings,
) -> dict[str, Any]:
    """
    Assemble the ``HAISessionResult`` artifact (section 5.7).

    Args:
        job_id: The session's job id.
        config: The validated session request.
        settings: For the recorded InteractiveAI commit.
        session_times: ``started_at``, ``deadline`` and ``ended_at``, UTC.
        intermediate: The object published at go-live.
        traces: cabhistoric traces, unchanged.
        trace_query: Query description from ``InteractiveAIClient.build_trace_query``.
        session_warnings: Warnings recorded during the session.

    Returns:
        The JSON-serialisable result.
    """
    return {
        "metadata": {
            "service": SERVICE_NAME,
            "task_id": job_id,
            "algorithm": config.algorithm,
            "interactiveai_commit": settings.interactiveai_commit,
            "grid2op_version": _installed_version("grid2op"),
            "trace_query": trace_query,
            "warnings": [TRACE_FILTER_WARNING, *session_warnings.as_list()],
        },
        "config": config.to_result_dict(),
        "session": {
            "started_at": format_utc(session_times["started_at"]),
            "deadline": format_utc(session_times["deadline"]),
            "ended_at": format_utc(session_times["ended_at"]),
            "intermediate": intermediate,
        },
        "traces": traces,
        "kpis": None,
        "survey": None,
        "not_collected": dict(NOT_COLLECTED_EXPLANATIONS),
    }


def compute_progress(started_at: datetime, now: datetime, session_duration_seconds: int) -> int:
    """
    Return the session's progress for ``/control/status`` (FR-06).

    Args:
        started_at: Go-live time.
        now: Current time.
        session_duration_seconds: Configured duration.

    Returns:
        Elapsed share in percent, between 0 and MAXIMUM_PROGRESS_WHILE_RUNNING.
    """
    elapsed_seconds = (now - started_at).total_seconds()
    percent = int(elapsed_seconds / session_duration_seconds * 100)
    return max(0, min(percent, MAXIMUM_PROGRESS_WHILE_RUNNING))


AgentActivator = Callable[[SessionConfig, str, SessionWarnings], ActiveAgent]
ClientFactory = Callable[[HAISettings], InteractiveAIClient]


class SessionCoordinator:
    """
    Owns the single session slot and runs sessions through their lifecycle.

    Example:
        >>> coordinator = SessionCoordinator(settings, AgentRegistry())
        >>> error = coordinator.reserve(job_id)        # None when the slot was free
        >>> result = coordinator.run_session(job_id, config, report_progress)
    """

    def __init__(
        self,
        settings: HAISettings,
        agent_registry: AgentRegistry,
        client_factory: ClientFactory = InteractiveAIClient,
        agent_activator: AgentActivator = activate_algorithm,
        clock: SessionClock | None = None,
    ) -> None:
        """
        Args:
            settings: Service configuration.
            agent_registry: Where the session's agent is installed.
            client_factory: Builds the InteractiveAI client; faked in tests.
            agent_activator: Builds the session's agent; faked in tests.
            clock: Time source and wait; faked in tests.
        """
        self._settings = settings
        self._agent_registry = agent_registry
        self._client_factory = client_factory
        self._agent_activator = agent_activator
        self._clock = clock or SessionClock()
        self._active_session: ActiveSession | None = None
        # job id -> intermediate object, or None while that session is not live yet.
        self._intermediate_results: dict[str, dict[str, Any] | None] = {}
        self._lock = threading.Lock()

    # --- Slot (FR-05) -------------------------------------------------------------

    def reserve(self, job_id: str) -> str | None:
        """
        Claim the session slot for a job.

        Args:
            job_id: The job about to be submitted.

        Returns:
            None if the slot was claimed, otherwise the error for the caller.
        """
        with self._lock:
            active_session = self._active_session
            if active_session is not None:
                if active_session.deadline is None:
                    state = "is starting"
                else:
                    state = f"is running until {format_utc(active_session.deadline)}"
                return f"HAI session {active_session.job_id} {state}; retry after it completes."
            self._active_session = ActiveSession(job_id=job_id)
            self._intermediate_results[job_id] = None
            return None

    def release(self, job_id: str) -> None:
        """
        Free the slot if this job holds it.

        Args:
            job_id: The job that ended or was never started.
        """
        with self._lock:
            if self._active_session is not None and self._active_session.job_id == job_id:
                self._active_session = None

    def forget(self, job_id: str) -> None:
        """
        Release the slot and drop the job's intermediate entry; for a job never submitted.

        Args:
            job_id: The job the runner refused.
        """
        self.release(job_id)
        with self._lock:
            self._intermediate_results.pop(job_id, None)

    def stop_active_session(self) -> None:
        """End the active session's wait early; its results are still collected."""
        with self._lock:
            if self._active_session is not None:
                self._active_session.stop_event.set()

    # --- Intermediate result (FR-08) ------------------------------------------------

    def get_intermediate_result(self, job_id: str) -> tuple[str, dict[str, Any] | None]:
        """
        Look up a job's intermediate object.

        Args:
            job_id: Job id from ``/control/execute``.

        Returns:
            ``("unknown", None)``, ``("pending", None)`` before go-live, or
            ``("ready", intermediate)``.
        """
        with self._lock:
            if job_id not in self._intermediate_results:
                return "unknown", None
            intermediate = self._intermediate_results[job_id]
        return ("pending", None) if intermediate is None else ("ready", intermediate)

    def _go_live(self, job_id: str, config: SessionConfig) -> tuple[datetime, datetime, dict[str, Any]]:
        """Fix the deadline and publish the intermediate object (FR-14, FR-09)."""
        started_at = self._clock.now()
        deadline = started_at + timedelta(seconds=config.session_duration_seconds)
        intermediate = build_intermediate_result(job_id, config, self._settings, started_at, deadline)
        with self._lock:
            if self._active_session is not None and self._active_session.job_id == job_id:
                self._active_session.deadline = deadline
            self._intermediate_results[job_id] = intermediate
        logger.info("HAI session %s is live: %s", job_id, json.dumps(intermediate))
        return started_at, deadline, intermediate

    # --- Lifecycle ------------------------------------------------------------------

    def _stop_event_for(self, job_id: str) -> threading.Event:
        """Return the job's stop event; a fresh one if the job no longer holds the slot."""
        with self._lock:
            if self._active_session is not None and self._active_session.job_id == job_id:
                return self._active_session.stop_event
        return threading.Event()

    def _wait_until_deadline(
        self,
        job_id: str,
        started_at: datetime,
        deadline: datetime,
        session_duration_seconds: int,
        report_progress: Callable[[int], None],
    ) -> bool:
        """
        Block until the deadline, reporting progress at least every 30 s (FR-15).

        Returns:
            True if the wait was interrupted before the deadline.
        """
        stop_event = self._stop_event_for(job_id)
        while True:
            now = self._clock.now()
            remaining_seconds = (deadline - now).total_seconds()
            if remaining_seconds <= 0:
                return False
            report_progress(compute_progress(started_at, now, session_duration_seconds))
            if self._clock.wait(stop_event, min(PROGRESS_REPORT_INTERVAL_SECONDS, remaining_seconds)):
                return True

    def run_session(
        self, job_id: str, config: SessionConfig, report_progress: Callable[[int], None]
    ) -> dict[str, Any]:
        """
        Run one session to its result; the job body handed to ``BatchJobRunner``.

        The agent is removed and the slot freed on every exit path, so a failed
        session never leaves the service busy (FR-16a).

        Args:
            job_id: The session's job id; must hold the slot (``reserve``).
            config: The validated session request.
            report_progress: Publishes progress to ``/control/status``.

        Returns:
            The ``HAISessionResult`` artifact.

        Raises:
            PreflightError: If a dependency is down before go-live.
            TraceCollectionError: If the traces cannot be fetched at the deadline.
            Exception: Whatever activating the algorithm or writing the config raises.
        """
        session_warnings = SessionWarnings()
        for kpi_warning in config.kpi_warnings():
            session_warnings.add(kpi_warning)

        try:
            client = self._client_factory(self._settings)
            client.run_preflight()

            self._agent_registry.activate(self._agent_activator(config, job_id, session_warnings))
            write_simulator_config_in_place(
                self._settings.simulator_config_path,
                render_simulator_config(config.scenario, self._settings.simulator_env_prefix),
            )

            started_at, deadline, intermediate = self._go_live(job_id, config)
            interrupted = self._wait_until_deadline(
                job_id, started_at, deadline, config.session_duration_seconds, report_progress
            )
            if interrupted:
                session_warnings.add("session was interrupted before its deadline (service shutdown)")

            self._agent_registry.deactivate(job_id)
            ended_at = self._clock.now()
            traces, trace_query = client.fetch_traces(started_at, deadline)
            logger.info("HAI session %s collected %d trace(s)", job_id, len(traces))

            return build_session_result(
                job_id,
                config,
                self._settings,
                {"started_at": started_at, "deadline": deadline, "ended_at": ended_at},
                intermediate,
                traces,
                trace_query,
                session_warnings,
            )
        finally:
            self._agent_registry.deactivate(job_id)
            self.release(job_id)


def create_intermediate_router(coordinator: SessionCoordinator) -> APIRouter:
    """
    Build ``GET /control/intermediate/{task_id}``, the orchestrator placeholder (FR-08).

    ``/control/status`` cannot carry the object: its ``response_model`` in the
    shared control plane drops unknown fields. The route uses the same bearer
    check as ``/control/*``.

    Args:
        coordinator: Supplies the intermediate objects.

    Returns:
        The router.
    """
    router = APIRouter(tags=["control"])

    @router.get("/control/intermediate/{task_id}", dependencies=[Depends(_check_api_key)])
    def get_intermediate(task_id: str) -> dict:
        """Return the participant links and instructions of a live session."""
        state, intermediate = coordinator.get_intermediate_result(task_id)
        if state == "unknown":
            raise HTTPException(status_code=404, detail="Task not found")
        if state == "pending":
            raise HTTPException(status_code=409, detail="not ready")
        return intermediate

    return router
