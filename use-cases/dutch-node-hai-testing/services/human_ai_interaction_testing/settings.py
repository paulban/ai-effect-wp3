"""
Settings — every tunable of the HAI session service in one place.

The service reads its deployment configuration from the environment once, into
a frozen ``HAISettings``. Fixed protocol values — retry schedules, validation
bounds, the simulator's default CONFIG.toml entries — are module constants
next to it, each with a note on where its value comes from.

Nothing here performs I/O beyond reading ``os.environ``, so importing the
module is safe in tests.

Spec coverage: section 5.8 (environment variables), NFR-04, FR-11, FR-19a,
FR-20, FR-20a (timing constants)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

SERVICE_DIRECTORY = Path(__file__).resolve().parent

# --- Identity ---------------------------------------------------------------

# Container name the orchestrator dials; recorded in the result metadata.
SERVICE_NAME = "hai-session-runner"

# Logical format recorded on the stored artifact and carried in the DataReference.
HAI_SESSION_RESULT_FORMAT = "HAISessionResult"

# InteractiveAI use case the simulator posts its context and events under, and
# the use case the session's traces are filtered by (FR-20).
INTERACTIVEAI_USE_CASE = "PowerGrid"

# --- Input validation bounds (section 5.3, NFR-04) --------------------------

DEFAULT_ALGORITHM = "algorithm_template.py"

# One minute is the shortest session worth running; thirty days the longest a
# study window is expected to stay open (NFR-04).
MINIMUM_SESSION_DURATION_SECONDS = 60
MAXIMUM_SESSION_DURATION_SECONDS = 30 * 24 * 60 * 60

DEFAULT_SCENARIO_FIRST_STEP = 0
DEFAULT_ENV_SEED = 0
# The simulator's own default pace (its shipped CONFIG.toml).
DEFAULT_STEP_DURATION_SECONDS = 0.5
# Anything slower than a minute per step makes an episode unplayable.
MAXIMUM_STEP_DURATION_SECONDS = 60.0

# Time series every chronic folder must contain, because the simulator builds
# its environment with exactly these handlers (Simulator.initialize_simulation).
REQUIRED_CHRONIC_SERIES = (
    "prod_p",
    "load_p",
    "prod_v",
    "load_q",
    "prod_p_forecasted",
    "load_p_forecasted",
    "load_q_forecasted",
)
# Extensions grid2op's CSVHandler reads.
CHRONIC_FILE_EXTENSIONS = (".csv", ".csv.bz2")

# --- Simulator environment construction (FR-17) ----------------------------

# Forecast horizons, in minutes, the simulator passes to grid2op. Copied from
# Simulator.initialize_simulation at InteractiveAI 600a1ce; the wrapper env must
# match or observations will not round-trip.
SIMULATOR_FORECAST_HORIZONS_MINUTES = (5, 10, 15, 20, 25, 30)

# --- Simulator CONFIG.toml defaults (section 5.6) ---------------------------

# Values of the shipped usecases_examples/PowerGrid/config/CONFIG.toml at
# InteractiveAI 600a1ce for the keys a request does not set. They are written
# back unchanged so the generated file is complete.
SIMULATOR_CONFIG_TITLE = "Simulator configuration"
SIMULATOR_TIME_STEP_FORECAST = 3
SIMULATOR_DURATION_STEP_FORECAST = 3
SIMULATOR_REFRESH_FREQUENCY_STEP = 3
SIMULATOR_STEP_START_SECURITY_ANALYSIS = 5
SIMULATOR_ASSISTANT_PATH = "Ressources/XD_silly_repo"
SIMULATOR_ASSISTANT_SEED = 1227139268

# --- Timing (FR-06, FR-11, FR-15, FR-20a) ------------------------------------

# Preflight: three tries two seconds apart, five seconds each (FR-11).
PREFLIGHT_ATTEMPTS = 3
PREFLIGHT_RETRY_DELAY_SECONDS = 2.0
HTTP_TIMEOUT_SECONDS = 5.0

# Trace collection back-off between attempts: 4 attempts in total (FR-20a).
TRACE_FETCH_RETRY_DELAYS_SECONDS = (5.0, 15.0, 45.0)
# Fetching a long session's traces can take longer than a health check.
TRACE_FETCH_TIMEOUT_SECONDS = 60.0

# How often the waiting job wakes to report progress (FR-15).
PROGRESS_REPORT_INTERVAL_SECONDS = 30.0
# Progress stays below 100 until the result is stored; the job runner sets 100
# on completion (FR-06).
MAXIMUM_PROGRESS_WHILE_RUNNING = 99

# --- Warnings (FR-19) --------------------------------------------------------

# Agent errors repeat every step of a broken session. The result keeps the
# first fifty and counts the rest.
MAXIMUM_RECORDED_WARNINGS = 50

# Truncation of str(action) in a recommendation's description (FR-18).
MAXIMUM_RECOMMENDATION_DESCRIPTION_LENGTH = 2000


def _read_text(environ: Mapping[str, str], name: str, default: str) -> str:
    """Return an environment value, or the default when unset or blank."""
    value = environ.get(name, "").strip()
    return value or default


def _read_optional_text(environ: Mapping[str, str], name: str) -> str | None:
    """Return an environment value, or None when unset or blank."""
    value = environ.get(name, "").strip()
    return value or None


def _read_positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    """
    Return a positive number from the environment.

    Raises:
        ValueError: If the value is set but not a positive number.
    """
    raw_value = environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as parse_error:
        raise ValueError(f"{name} must be a number, got {raw_value!r}") from parse_error
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class HAISettings:
    """
    Deployment configuration of the HAI session service (spec section 5.8).

    Attributes:
        algorithm_directory: Where algorithm ``.py`` files are selected from.
        scenario_directory: Where grid2op environment directories are selected from.
        simulator_config_path: The CONFIG.toml shared with the simulator by bind mount.
        simulator_env_prefix: The scenarios mount as the simulator sees it,
            relative to its working directory ``/code``.
        cab_internal_url: InteractiveAI gateway as this service reaches it.
        simulator_internal_url: Simulator as this service reaches it.
        frontend_public_url: InteractiveAI frontend link for the participant.
        simulator_public_url: Simulator link for the participant.
        simulator_cab_server: Server the participant selects in the simulator login.
        cab_username: InteractiveAI test account user.
        cab_password: InteractiveAI test account password.
        agent_token: Bearer token required on the agent endpoint, or None for open.
        agent_act_timeout_seconds: Longest an algorithm may take per recommendation.
        trace_window_margin_seconds: Widening of the trace query window on each side.
        interactiveai_commit: InteractiveAI commit recorded in the result, if known.
    """

    algorithm_directory: Path
    scenario_directory: Path
    simulator_config_path: Path
    simulator_env_prefix: str
    cab_internal_url: str
    simulator_internal_url: str
    frontend_public_url: str
    simulator_public_url: str
    simulator_cab_server: str
    cab_username: str
    cab_password: str
    agent_token: str | None
    agent_act_timeout_seconds: float
    trace_window_margin_seconds: float
    interactiveai_commit: str | None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "HAISettings":
        """
        Read the settings from environment variables, applying section 5.8 defaults.

        Args:
            environ: Mapping to read from; ``os.environ`` when omitted.

        Returns:
            The settings.

        Raises:
            ValueError: If a numeric variable is set to an invalid value.
        """
        environ = os.environ if environ is None else environ
        return cls(
            algorithm_directory=Path(
                _read_text(environ, "ALGORITHM_DIRECTORY", str(SERVICE_DIRECTORY / "algorithms"))
            ),
            scenario_directory=Path(
                _read_text(environ, "SCENARIO_DIRECTORY", str(SERVICE_DIRECTORY / "scenarios"))
            ),
            simulator_config_path=Path(
                _read_text(environ, "HAI_SIMULATOR_CONFIG_PATH", "/app/runtime/CONFIG.toml")
            ),
            simulator_env_prefix=_read_text(
                environ, "HAI_SIMULATOR_ENV_PREFIX", "Ressources/hai_scenarios"
            ).rstrip("/"),
            cab_internal_url=_read_text(
                environ, "HAI_CAB_INTERNAL_URL", "http://frontend:80"
            ).rstrip("/"),
            simulator_internal_url=_read_text(
                environ, "HAI_SIMULATOR_INTERNAL_URL", "http://PowerGrid-simulator-app:5000"
            ).rstrip("/"),
            frontend_public_url=_read_text(
                environ, "HAI_FRONTEND_PUBLIC_URL", "http://localhost:3200"
            ),
            simulator_public_url=_read_text(
                environ, "HAI_SIMULATOR_PUBLIC_URL", "http://localhost:5122"
            ),
            simulator_cab_server=_read_text(
                environ, "HAI_SIMULATOR_CAB_SERVER", "http://host.docker.internal:3200/"
            ),
            cab_username=_read_text(environ, "HAI_CAB_USERNAME", "powergrid_user"),
            cab_password=_read_text(environ, "HAI_CAB_PASSWORD", "test"),
            agent_token=_read_optional_text(environ, "HAI_AGENT_TOKEN"),
            agent_act_timeout_seconds=_read_positive_float(
                environ, "HAI_AGENT_ACT_TIMEOUT_SECONDS", 20.0
            ),
            trace_window_margin_seconds=_read_positive_float(
                environ, "HAI_TRACE_WINDOW_MARGIN_SECONDS", 900.0
            ),
            interactiveai_commit=_read_optional_text(environ, "HAI_INTERACTIVEAI_COMMIT"),
        )
