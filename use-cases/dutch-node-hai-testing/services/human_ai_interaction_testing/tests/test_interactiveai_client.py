"""InteractiveAI client: token, preflight, traces (spec FR-11, FR-20, FR-20a, FR-21)."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from interactiveai_client import InteractiveAIClient, PreflightError, TraceCollectionError

WINDOW_START = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone.utc)


class FakeInteractiveAI:
    """An httpx handler standing in for the gateway and the simulator."""

    def __init__(self) -> None:
        self.simulator_status = 200
        self.gateway_status = 200
        self.token_status = 200
        self.trace_statuses: list[int] = []  # consumed per trace request; 200 once empty
        self.traces = [{"step": "AWARD", "date": "2026-11-11T10:05:00"}]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "simulator.test":
            return httpx.Response(self.simulator_status, text="<html></html>")
        if path == "/auth/token":
            return httpx.Response(self.token_status, json={"access_token": "token-123"})
        if path == "/cabrecommendation/api/v1/health":
            return httpx.Response(self.gateway_status, json={"message": "Ok"})
        if path == "/cabhistoric/api/v1/traces":
            status = self.trace_statuses.pop(0) if self.trace_statuses else 200
            return httpx.Response(status, json=self.traces if status == 200 else {"error": "down"})
        return httpx.Response(404)


@pytest.fixture
def fake_interactiveai() -> FakeInteractiveAI:
    return FakeInteractiveAI()


@pytest.fixture
def client_and_sleeps(hai_settings, fake_interactiveai):
    """A client wired to the fake, recording sleeps instead of sleeping."""
    sleeps: list[float] = []
    client = InteractiveAIClient(
        hai_settings, http_client=httpx.Client(transport=httpx.MockTransport(fake_interactiveai)), sleep=sleeps.append
    )
    return client, sleeps


def test_token_uses_test_account_password_grant(client_and_sleeps, fake_interactiveai):
    """FR-21: the token request is the opfab-client password grant for the test account."""
    client, _ = client_and_sleeps
    assert client.fetch_access_token() == "token-123"
    form = parse_qs(fake_interactiveai.requests[-1].content.decode())
    assert form == {
        "username": ["powergrid_user"],
        "password": ["test"],
        "grant_type": ["password"],
        "client_id": ["opfab-client"],
    }


def test_preflight_passes_when_everything_answers(client_and_sleeps):
    """FR-11: no error when the simulator, gateway and auth answer."""
    client, sleeps = client_and_sleeps
    client.run_preflight()
    assert sleeps == []


def test_preflight_names_unreachable_simulator(client_and_sleeps, fake_interactiveai):
    """FR-11: a down simulator fails after 3 attempts, naming the simulator URL."""
    # Given
    client, sleeps = client_and_sleeps
    fake_interactiveai.simulator_status = 502

    # When / Then
    with pytest.raises(PreflightError, match=r"simulator at http://simulator\.test/"):
        client.run_preflight()
    assert sleeps == [2.0, 2.0]


def test_preflight_names_failing_authentication(client_and_sleeps, fake_interactiveai):
    """FR-11 (c): a rejected token request fails the preflight."""
    client, _ = client_and_sleeps
    fake_interactiveai.token_status = 401
    with pytest.raises(PreflightError, match="authentication"):
        client.run_preflight()


def test_trace_query_window_is_exact(client_and_sleeps):
    """Trace window (9.2): use case, and start/end widened by the 900 s margin."""
    client, _ = client_and_sleeps
    trace_query = client.build_trace_query(WINDOW_START, WINDOW_END)

    assert trace_query["use_case"] == "PowerGrid"
    assert trace_query["date_start"] == "2026-11-11T09:45:00"
    assert trace_query["date_end"] == "2026-11-12T10:15:00"
    query_parameters = parse_qs(urlparse(trace_query["url"]).query)
    assert query_parameters == {
        "use_case": ["PowerGrid"],
        "date_start": ["2026-11-11T09:45:00"],
        "date_end": ["2026-11-12T10:15:00"],
    }


def test_traces_are_fetched_with_bearer_token(client_and_sleeps, fake_interactiveai):
    """FR-20: the traces are returned unchanged and requested with the token."""
    client, _ = client_and_sleeps
    traces, _ = client.fetch_traces(WINDOW_START, WINDOW_END)

    assert traces == fake_interactiveai.traces
    trace_request = fake_interactiveai.requests[-1]
    assert trace_request.headers["Authorization"] == "Bearer token-123"


def test_trace_fetch_recovers_after_transient_failures(client_and_sleeps, fake_interactiveai):
    """FR-20a: failures within the retry budget are absorbed with 5 / 15 s back-off."""
    client, sleeps = client_and_sleeps
    fake_interactiveai.trace_statuses = [500, 503]
    traces, _ = client.fetch_traces(WINDOW_START, WINDOW_END)
    assert traces == fake_interactiveai.traces
    assert sleeps == [5.0, 15.0]


def test_trace_fetch_exhausted_reports_query_url(client_and_sleeps, fake_interactiveai):
    """Trace retry exhausted (9.2): four failures raise with the query URL."""
    client, sleeps = client_and_sleeps
    fake_interactiveai.trace_statuses = [500, 500, 500, 500]

    with pytest.raises(TraceCollectionError, match=r"http://gateway\.test/cabhistoric/api/v1/traces\?use_case=PowerGrid"):
        client.fetch_traces(WINDOW_START, WINDOW_END)
    assert sleeps == [5.0, 15.0, 45.0]
