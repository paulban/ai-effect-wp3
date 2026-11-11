"""Session lifecycle through the real control plane (spec FR-01 to FR-16a, FR-20a, 5.4, 5.7).

The app is the one ``service.py`` runs — ``common.concurrent.create_app`` with
the agent and intermediate routers — with the InteractiveAI client, the agent
activation and the clock replaced by fakes.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from datetime import timedelta
from typing import Any

import pytest
import toml
from conftest import FAKE_CLOCK_START, FakeClock, valid_payload
from fastapi.testclient import TestClient

import service
from agent_wrapper import ActiveAgent, AgentRegistry, create_agent_router
from common.artifacts import FileArtifactStore
from common.batch_jobs import BatchJobRunner
from common.concurrent import create_app
from interactiveai_client import PreflightError, TraceCollectionError
from session import SessionCoordinator, compute_progress, create_intermediate_router
from session_warnings import SessionWarnings

FAKE_TRACES = [{"use_case": "PowerGrid", "step": "AWARD", "date": "2026-11-11T10:05:00", "data": {}}]
JOB_COMPLETION_TIMEOUT_SECONDS = 10.0


class FakeInteractiveAIClient:
    """Stands in for InteractiveAIClient; behaviour set per test."""

    def __init__(self) -> None:
        self.preflight_error: Exception | None = None
        self.preflight_gate: threading.Event | None = None
        self.trace_error: Exception | None = None
        self.trace_windows: list[tuple] = []

    def __call__(self, settings) -> "FakeInteractiveAIClient":
        return self  # used as the client factory

    def run_preflight(self) -> None:
        if self.preflight_gate is not None:
            self.preflight_gate.wait(JOB_COMPLETION_TIMEOUT_SECONDS)
        if self.preflight_error is not None:
            raise self.preflight_error

    def fetch_traces(self, window_start, window_end):
        self.trace_windows.append((window_start, window_end))
        if self.trace_error is not None:
            raise self.trace_error
        return list(FAKE_TRACES), {"url": "http://gateway.test/cabhistoric/api/v1/traces?use_case=PowerGrid"}


class StubAgent:
    def act(self, observation):
        raise AssertionError("not called in lifecycle tests")


class FakeActivator:
    """Stands in for activate_algorithm; records calls, optionally raises."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.activated_job_ids: list[str] = []

    def __call__(self, config, job_id: str, session_warnings: SessionWarnings) -> ActiveAgent:
        if self.error is not None:
            raise self.error
        self.activated_job_ids.append(job_id)
        return ActiveAgent(job_id=job_id, environment=None, agent=StubAgent(), session_warnings=session_warnings)


class ServiceHarness:
    """The service app with fakes installed, plus helpers to drive it."""

    def __init__(self, settings, artifact_directory, clock: FakeClock) -> None:
        self.settings = settings
        self.clock = clock
        self.fake_client = FakeInteractiveAIClient()
        self.fake_activator = FakeActivator()
        self.agent_registry = AgentRegistry()
        self.coordinator = SessionCoordinator(
            settings,
            self.agent_registry,
            client_factory=self.fake_client,
            agent_activator=self.fake_activator,
            clock=clock,
        )
        runner = BatchJobRunner(FileArtifactStore(artifact_directory), self_url="http://hai.test", max_concurrent_jobs=1)
        service.set_settings(settings)
        service.set_job_runner(runner)
        service.set_session_coordinator(self.coordinator)
        application = create_app(
            service,
            extra_routers=[
                create_agent_router(self.agent_registry, lambda: settings),
                create_intermediate_router(self.coordinator),
            ],
            artifact_store=runner.artifact_store,
        )
        self.http = TestClient(application)

    def submit(self, payload: dict[str, Any]) -> dict:
        """POST /control/execute with a fresh workflow id."""
        encoded_payload = base64.b64encode(json.dumps(payload).encode()).decode()
        response = self.http.post(
            "/control/execute",
            json={
                "method": "RunHAISession",
                "workflow_id": f"wf-{uuid.uuid4().hex[:8]}",
                "task_id": "hai-task",
                "inputs": [{"protocol": "inline", "uri": encoded_payload, "format": "json"}],
            },
        )
        assert response.status_code == 200
        return response.json()

    def wait_for_completion(self, job_id: str) -> dict:
        """Poll /control/status until the job leaves ``running``."""
        deadline = time.monotonic() + JOB_COMPLETION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            status = self.http.get(f"/control/status/{job_id}").json()
            if status["status"] != "running":
                return status
            time.sleep(0.02)
        raise AssertionError(f"job {job_id} still running after {JOB_COMPLETION_TIMEOUT_SECONDS}s")

    def fetch_result(self, job_id: str) -> dict:
        """Follow /control/output to the stored artifact."""
        output = self.http.get(f"/control/output/{job_id}").json()["output"]
        assert output["uri"].endswith(f"/control/data/{job_id}")
        return self.http.get(f"/control/data/{job_id}").json()


@pytest.fixture
def harness(hai_settings, tmp_path, fake_clock):
    yield ServiceHarness(hai_settings, tmp_path / "artifacts", fake_clock)
    service.set_settings(None)
    service.set_job_runner(None)
    service.set_session_coordinator(None)


@pytest.fixture
def blocking_harness(hai_settings, tmp_path):
    """A harness whose sessions stay live until ``harness.clock.release()``."""
    harness = ServiceHarness(hai_settings, tmp_path / "artifacts", FakeClock(block_waits=True))
    yield harness
    harness.clock.release()
    service.set_settings(None)
    service.set_job_runner(None)
    service.set_session_coordinator(None)


def test_happy_path_session_produces_result(harness, node_directories):
    """Happy path (9.2) and FR-15/16a/20: a 60 s session completes with the 5.7 result."""
    # Given / When
    response = harness.submit(valid_payload(session_duration_seconds=60, kpis=["operation_score"]))

    # Then: accepted immediately
    assert response["status"] == "running"
    job_id = response["task_id"]
    assert harness.wait_for_completion(job_id)["status"] == "complete"

    result = harness.fetch_result(job_id)
    assert result["traces"] == FAKE_TRACES
    assert result["survey"] is None and result["kpis"] is None
    assert set(result["not_collected"]) == {"survey", "session_export", "kpis"}
    assert result["config"]["session_duration_seconds"] == 60
    assert result["metadata"]["service"] == "hai-session-runner"
    assert result["metadata"]["task_id"] == job_id
    assert result["metadata"]["algorithm"] == "algorithm_template.py"
    assert any("filtered by time window" in warning for warning in result["metadata"]["warnings"])
    assert any("operation_score" in warning for warning in result["metadata"]["warnings"])

    session_block = result["session"]
    assert session_block["started_at"] == "2026-11-11T10:00:00Z"
    assert session_block["deadline"] == "2026-11-11T10:01:00Z"
    assert session_block["ended_at"] >= session_block["deadline"]
    assert session_block["intermediate"]["task_id"] == job_id

    # The traces window is the session's go-live to deadline.
    assert harness.fake_client.trace_windows == [(FAKE_CLOCK_START, FAKE_CLOCK_START + timedelta(seconds=60))]
    # The agent was installed and removed again.
    assert harness.fake_activator.activated_job_ids == [job_id]
    assert harness.agent_registry.current() is None


def test_session_writes_simulator_config(harness, node_directories):
    """FR-13: going live writes the scenario into the shared CONFIG.toml."""
    job_id = harness.submit(valid_payload(session_duration_seconds=60))["task_id"]
    harness.wait_for_completion(job_id)

    written_values = toml.load(node_directories["simulator_config"])
    assert written_values["env_name"] == "Ressources/hai_scenarios/fake_env"
    assert written_values["scenario_name"] == "2035-01-01_0"


def test_invalid_payload_is_rejected_without_job(harness):
    """FR-03: an invalid request fails at once, naming the field, and registers no job."""
    response = harness.submit(valid_payload(session_duration_seconds=10))
    assert response["status"] == "failed"
    assert "session_duration_seconds" in response["error"]
    assert harness.http.get("/control/status/hai-task").status_code == 404


def test_live_session_serves_intermediate_and_refuses_second(blocking_harness):
    """FR-05 and FR-08/09: while live, the intermediate object is served and a second
    request is refused with the first job id and its deadline."""
    harness = blocking_harness

    # Given: a live session
    first_job_id = harness.submit(valid_payload(session_duration_seconds=3600))["task_id"]
    assert harness.clock.waiting.wait(JOB_COMPLETION_TIMEOUT_SECONDS)

    # Then: the intermediate result is available
    intermediate_response = harness.http.get(f"/control/intermediate/{first_job_id}")
    assert intermediate_response.status_code == 200
    intermediate = intermediate_response.json()
    assert intermediate["frontend_url"] == "http://localhost:3200"
    assert intermediate["simulator_url"] == "http://localhost:5122"
    assert intermediate["credentials"] == {"username": "powergrid_user", "password": "test"}
    assert intermediate["deadline"] == "2026-11-11T11:00:00Z"
    assert len(intermediate["instructions"]) == 6
    assert "http://localhost:5122" in intermediate["instructions"][0]
    assert "2026-11-11T11:00:00Z" in intermediate["instructions"][5]
    assert harness.agent_registry.current().job_id == first_job_id

    # When: a second session is requested
    second_response = harness.submit(valid_payload(session_duration_seconds=60))

    # Then: refused with the running session's id and deadline
    assert second_response["status"] == "failed"
    assert first_job_id in second_response["error"]
    assert "2026-11-11T11:00:00Z" in second_response["error"]

    # And: after the first completes, a new one is accepted
    harness.clock.release()
    assert harness.wait_for_completion(first_job_id)["status"] == "complete"
    third_job_id = harness.submit(valid_payload(session_duration_seconds=60))["task_id"]
    assert harness.wait_for_completion(third_job_id)["status"] == "complete"


def test_intermediate_before_live_and_unknown(harness):
    """FR-08: 409 before go-live, 404 for an unknown task."""
    harness.fake_client.preflight_gate = threading.Event()
    job_id = harness.submit(valid_payload(session_duration_seconds=60))["task_id"]

    response = harness.http.get(f"/control/intermediate/{job_id}")
    assert response.status_code == 409
    assert response.json() == {"detail": "not ready"}
    assert harness.http.get("/control/intermediate/unknown-task").status_code == 404

    harness.fake_client.preflight_gate.set()
    assert harness.wait_for_completion(job_id)["status"] == "complete"
    assert harness.http.get(f"/control/intermediate/{job_id}").status_code == 200


def test_intermediate_requires_service_api_key(harness, monkeypatch):
    """FR-08 / NFR-05: with SERVICE_API_KEY set, the intermediate route needs it."""
    monkeypatch.setenv("SERVICE_API_KEY", "control-key")
    assert harness.http.get("/control/intermediate/any").status_code == 401
    authorised = harness.http.get("/control/intermediate/any", headers={"Authorization": "Bearer control-key"})
    assert authorised.status_code == 404


def test_preflight_failure_fails_job_and_frees_slot(harness):
    """FR-11: an unreachable simulator fails the job with its URL; a new session is accepted after."""
    harness.fake_client.preflight_error = PreflightError("Preflight failed: simulator at http://simulator.test/")

    job_id = harness.submit(valid_payload(session_duration_seconds=60))["task_id"]
    status = harness.wait_for_completion(job_id)

    assert status["status"] == "failed"
    assert "http://simulator.test/" in status["error"]
    harness.fake_client.preflight_error = None
    next_job_id = harness.submit(valid_payload(session_duration_seconds=60))["task_id"]
    assert harness.wait_for_completion(next_job_id)["status"] == "complete"


def test_build_agent_failure_fails_job_and_frees_slot(harness):
    """Algorithm raises in build_agent (9.2): the job fails with the message; the slot is cleared."""
    harness.fake_activator.error = RuntimeError("build_agent exploded")

    job_id = harness.submit(valid_payload(session_duration_seconds=60))["task_id"]
    status = harness.wait_for_completion(job_id)

    assert status["status"] == "failed"
    assert "build_agent exploded" in status["error"]
    assert harness.coordinator.reserve("probe") is None


def test_trace_failure_fails_job_with_query_url(harness):
    """FR-20a: when collection fails the job fails with the query URL; the agent is removed."""
    harness.fake_client.trace_error = TraceCollectionError(
        "Could not fetch ... from: http://gateway.test/cabhistoric/api/v1/traces?use_case=PowerGrid"
    )

    job_id = harness.submit(valid_payload(session_duration_seconds=60))["task_id"]
    status = harness.wait_for_completion(job_id)

    assert status["status"] == "failed"
    assert "cabhistoric/api/v1/traces" in status["error"]
    assert harness.agent_registry.current() is None
    harness.fake_client.trace_error = None
    next_job_id = harness.submit(valid_payload(session_duration_seconds=60))["task_id"]
    assert harness.wait_for_completion(next_job_id)["status"] == "complete"


def test_stopped_session_still_collects(blocking_harness):
    """FR-15: a wait interrupted by shutdown still collects, with a warning."""
    harness = blocking_harness
    job_id = harness.submit(valid_payload(session_duration_seconds=3600))["task_id"]
    assert harness.clock.waiting.wait(JOB_COMPLETION_TIMEOUT_SECONDS)

    harness.coordinator.stop_active_session()

    assert harness.wait_for_completion(job_id)["status"] == "complete"
    warnings = harness.fetch_result(job_id)["metadata"]["warnings"]
    assert any("interrupted" in warning for warning in warnings)


def test_progress_is_elapsed_share_capped():
    """FR-06: progress is elapsed / duration, 45–55 at half time, at most 99."""
    assert compute_progress(FAKE_CLOCK_START, FAKE_CLOCK_START + timedelta(seconds=1800), 3600) == 50
    assert compute_progress(FAKE_CLOCK_START, FAKE_CLOCK_START + timedelta(seconds=7200), 3600) == 99
    assert compute_progress(FAKE_CLOCK_START, FAKE_CLOCK_START - timedelta(seconds=10), 3600) == 0


def test_status_reports_running_progress(blocking_harness):
    """FR-06: a live session is ``running`` with its progress."""
    harness = blocking_harness
    job_id = harness.submit(valid_payload(session_duration_seconds=3600))["task_id"]
    assert harness.clock.waiting.wait(JOB_COMPLETION_TIMEOUT_SECONDS)

    status = harness.http.get(f"/control/status/{job_id}").json()

    assert status["status"] == "running"
    assert 0 <= status["progress"] <= 99
