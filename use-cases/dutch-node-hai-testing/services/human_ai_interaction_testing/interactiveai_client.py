"""
InteractiveAI client — the few HTTP calls the session makes to the CAB stack.

The service talks to InteractiveAI only through its nginx gateway (the
``frontend`` container), as the test account:

- ``POST /auth/token`` — a Keycloak password grant for ``opfab-client``, as in
  InteractiveAI's ``resources/getToken.sh`` (FR-21);
- ``GET /cabrecommendation/api/v1/health`` — preflight (FR-11);
- ``GET /cabhistoric/api/v1/traces`` — the session's traces (FR-20).

It reaches the simulator once, with ``GET /``, to check that it is up before a
session goes live. It never drives the simulator: the participant does.

The HTTP client and the sleep function are injectable, so tests run against an
``httpx.MockTransport`` without waiting.

Spec coverage: FR-11, FR-20, FR-20a, FR-21
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from settings import (
    HTTP_TIMEOUT_SECONDS,
    INTERACTIVEAI_USE_CASE,
    PREFLIGHT_ATTEMPTS,
    PREFLIGHT_RETRY_DELAY_SECONDS,
    TRACE_FETCH_RETRY_DELAYS_SECONDS,
    TRACE_FETCH_TIMEOUT_SECONDS,
    HAISettings,
)

logger = logging.getLogger(__name__)

# Keycloak client InteractiveAI's frontend and scripts authenticate with.
KEYCLOAK_CLIENT_ID = "opfab-client"

TOKEN_PATH = "/auth/token"
RECOMMENDATION_HEALTH_PATH = "/cabrecommendation/api/v1/health"
TRACES_PATH = "/cabhistoric/api/v1/traces"

# Trace dates are compared by cabhistoric's SQL filter; naive ISO seconds are
# what its DateTime column compares cleanly against (see open question 2).
TRACE_QUERY_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


class PreflightError(RuntimeError):
    """Raised when a dependency of the session is not reachable before go-live."""


class TraceCollectionError(RuntimeError):
    """Raised when the session's traces cannot be fetched after every retry."""


def format_trace_query_time(moment: datetime) -> str:
    """
    Format a UTC moment for cabhistoric's ``date_start`` / ``date_end`` filter.

    Args:
        moment: A timezone-aware UTC datetime.

    Returns:
        Naive ISO-8601 seconds in UTC, e.g. ``2026-11-11T10:00:00``.
    """
    return moment.strftime(TRACE_QUERY_TIME_FORMAT)


class InteractiveAIClient:
    """
    Calls the InteractiveAI gateway and simulator on behalf of a session.

    Example:
        >>> client = InteractiveAIClient(HAISettings.from_environment())
        >>> client.run_preflight()            # raises PreflightError when down
        >>> traces, query = client.fetch_traces(start, end)
    """

    def __init__(
        self,
        settings: HAISettings,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        Args:
            settings: Gateway, simulator and account configuration.
            http_client: Client to send requests with; a fresh one when omitted.
            sleep: Waits between retries; replaced by a no-op in tests.
        """
        self._settings = settings
        self._http_client = http_client or httpx.Client()
        self._sleep = sleep

    # --- Authentication ---------------------------------------------------------

    def fetch_access_token(self) -> str:
        """
        Obtain an access token for the test account (FR-21).

        Returns:
            The bearer token.

        Raises:
            RuntimeError: If the gateway rejects the request or returns no token.
        """
        token_url = f"{self._settings.cab_internal_url}{TOKEN_PATH}"
        response = self._http_client.post(
            token_url,
            data={
                "username": self._settings.cab_username,
                "password": self._settings.cab_password,
                "grant_type": "password",
                "client_id": KEYCLOAK_CLIENT_ID,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Token request to {token_url} returned HTTP {response.status_code}")
        access_token = response.json().get("access_token")
        if not access_token:
            raise RuntimeError(f"Token response from {token_url} carries no access_token")
        return access_token

    # --- Preflight (FR-11) ----------------------------------------------------------

    def _check_returns_ok(self, url: str) -> None:
        """
        Raise unless ``GET url`` answers HTTP 200.

        Raises:
            RuntimeError: With the status or connection error.
        """
        try:
            response = self._http_client.get(url, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True)
        except httpx.HTTPError as connection_error:
            raise RuntimeError(f"{type(connection_error).__name__}: {connection_error}") from connection_error
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")

    def _retry_check(self, check_name: str, check: Callable[[], Any], target_url: str) -> None:
        """
        Run one preflight check up to PREFLIGHT_ATTEMPTS times.

        Raises:
            PreflightError: Naming the check and URL, once every attempt failed.
        """
        last_error: Exception | None = None
        for attempt_number in range(1, PREFLIGHT_ATTEMPTS + 1):
            try:
                check()
                return
            except Exception as check_error:  # noqa: BLE001 - retried, then reported
                last_error = check_error
                logger.warning(
                    "Preflight %s attempt %d/%d failed: %s", check_name, attempt_number, PREFLIGHT_ATTEMPTS, check_error
                )
                if attempt_number < PREFLIGHT_ATTEMPTS:
                    self._sleep(PREFLIGHT_RETRY_DELAY_SECONDS)
        raise PreflightError(
            f"Preflight failed: {check_name} at {target_url} is not available ({last_error}). "
            "Is the InteractiveAI stack up and connected to the ai-effect-services network? "
            "Re-run scripts/setup_interactiveai.sh."
        )

    def run_preflight(self) -> None:
        """
        Check the simulator, the recommendation gateway and authentication (FR-11).

        Raises:
            PreflightError: Naming the first check that failed and its URL.
        """
        simulator_url = f"{self._settings.simulator_internal_url}/"
        gateway_url = f"{self._settings.cab_internal_url}{RECOMMENDATION_HEALTH_PATH}"
        token_url = f"{self._settings.cab_internal_url}{TOKEN_PATH}"

        self._retry_check("simulator", lambda: self._check_returns_ok(simulator_url), simulator_url)
        self._retry_check("recommendation gateway", lambda: self._check_returns_ok(gateway_url), gateway_url)
        self._retry_check("test account authentication", self.fetch_access_token, token_url)

    # --- Traces (FR-20, FR-20a) ------------------------------------------------------

    def build_trace_query(self, window_start: datetime, window_end: datetime) -> dict[str, str]:
        """
        Describe the traces query for a session window, margin included (FR-20).

        Args:
            window_start: Session go-live, UTC.
            window_end: Session deadline, UTC.

        Returns:
            ``url`` (full query URL), ``date_start``, ``date_end`` and ``use_case``.
        """
        margin = timedelta(seconds=self._settings.trace_window_margin_seconds)
        query_parameters = {
            "use_case": INTERACTIVEAI_USE_CASE,
            "date_start": format_trace_query_time(window_start - margin),
            "date_end": format_trace_query_time(window_end + margin),
        }
        query_url = f"{self._settings.cab_internal_url}{TRACES_PATH}?{urlencode(query_parameters)}"
        return {"url": query_url, **query_parameters}

    def _fetch_traces_once(self, query_url: str) -> list[Any]:
        """
        Fetch the traces with a fresh token.

        Raises:
            RuntimeError: If the request fails or the answer is not a list.
        """
        access_token = self.fetch_access_token()
        response = self._http_client.get(
            query_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TRACE_FETCH_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        traces = response.json()
        if not isinstance(traces, list):
            raise RuntimeError(f"expected a list of traces, got {type(traces).__name__}")
        return traces

    def fetch_traces(self, window_start: datetime, window_end: datetime) -> tuple[list[Any], dict[str, str]]:
        """
        Fetch every PowerGrid trace of a session window, retrying with back-off (FR-20a).

        Args:
            window_start: Session go-live, UTC.
            window_end: Session deadline, UTC.

        Returns:
            The traces unchanged, and the query description for the result metadata.

        Raises:
            TraceCollectionError: Including the query URL, once every attempt failed,
                so the traces can be fetched by hand. They stay in cabhistoric.
        """
        trace_query = self.build_trace_query(window_start, window_end)
        retry_delays = list(TRACE_FETCH_RETRY_DELAYS_SECONDS)
        attempt_count = len(retry_delays) + 1
        last_error: Exception | None = None

        for attempt_number in range(1, attempt_count + 1):
            try:
                return self._fetch_traces_once(trace_query["url"]), trace_query
            except Exception as fetch_error:  # noqa: BLE001 - retried, then reported
                last_error = fetch_error
                logger.warning("Trace fetch attempt %d/%d failed: %s", attempt_number, attempt_count, fetch_error)
                if retry_delays:
                    self._sleep(retry_delays.pop(0))

        raise TraceCollectionError(
            f"Could not fetch the session traces after {attempt_count} attempts ({last_error}). "
            f"They remain in cabhistoric; fetch them with a test-account token from: {trace_query['url']}"
        )
