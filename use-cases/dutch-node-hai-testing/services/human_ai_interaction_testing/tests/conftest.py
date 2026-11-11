"""
Shared fixtures for the HAI session service tests.

The service imports its modules and the shared control plane (``common``) side
by side, as they sit under /app in the image. Outside the image they live in
the service directory and three directories up, so both go on the path before
any test module is collected.

Nothing here needs a running InteractiveAI stack: the gateway, the simulator
and the clock are faked. Tests that need grid2op use the simulator's own test
environment from the InteractiveAI submodule and are skipped when grid2op is
not installed.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

SERVICE_DIRECTORY = Path(__file__).resolve().parents[1]
USE_CASES_DIRECTORY = SERVICE_DIRECTORY.parents[2]

for import_path in (SERVICE_DIRECTORY, USE_CASES_DIRECTORY):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from settings import REQUIRED_CHRONIC_SERIES, HAISettings  # noqa: E402

# The simulator's own test environment, used by the grid2op round-trip tests.
SIMULATOR_TEST_ENVIRONMENT = (
    SERVICE_DIRECTORY / "InteractiveAI" / "usecases_examples" / "PowerGrid" / "Ressources" / "env_ICAPS_input_data_test"
)
SIMULATOR_TEST_CHRONIC = "2035-01-01_0"

FAKE_ENVIRONMENT_NAME = "fake_env"
FAKE_CHRONIC_NAME = "2035-01-01_0"

# A fixed go-live time keeps formatted timestamps predictable.
FAKE_CLOCK_START = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)

RAISING_ALGORITHM_SOURCE = '''
def build_agent(env, context):
    raise RuntimeError("build_agent exploded")
'''


def make_fake_scenario(scenario_directory: Path, environment_name: str, chronic_name: str) -> Path:
    """Create an environment directory whose chronic holds every required series (empty files)."""
    chronic_directory = scenario_directory / environment_name / "chronics" / chronic_name
    chronic_directory.mkdir(parents=True)
    for series_name in REQUIRED_CHRONIC_SERIES:
        (chronic_directory / f"{series_name}.csv.bz2").write_bytes(b"")
    return scenario_directory / environment_name


@pytest.fixture
def node_directories(tmp_path: Path) -> dict[str, Path]:
    """Algorithms, scenarios and runtime folders, filled the way an operator would."""
    algorithm_directory = tmp_path / "algorithms"
    algorithm_directory.mkdir()
    (algorithm_directory / "algorithm_template.py").write_text(
        (SERVICE_DIRECTORY / "algorithms" / "algorithm_template.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (algorithm_directory / "raising_algorithm.py").write_text(RAISING_ALGORITHM_SOURCE, encoding="utf-8")

    scenario_directory = tmp_path / "scenarios"
    scenario_directory.mkdir()
    make_fake_scenario(scenario_directory, FAKE_ENVIRONMENT_NAME, FAKE_CHRONIC_NAME)

    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir()
    simulator_config_path = runtime_directory / "CONFIG.toml"
    simulator_config_path.write_text('titre = "original"\n', encoding="utf-8")

    return {
        "algorithms": algorithm_directory,
        "scenarios": scenario_directory,
        "simulator_config": simulator_config_path,
    }


@pytest.fixture
def hai_settings(node_directories: dict[str, Path]) -> HAISettings:
    """Settings pointing at the temporary node folders, otherwise section 5.8 defaults."""
    return HAISettings.from_environment(
        {
            "ALGORITHM_DIRECTORY": str(node_directories["algorithms"]),
            "SCENARIO_DIRECTORY": str(node_directories["scenarios"]),
            "HAI_SIMULATOR_CONFIG_PATH": str(node_directories["simulator_config"]),
            "HAI_CAB_INTERNAL_URL": "http://gateway.test",
            "HAI_SIMULATOR_INTERNAL_URL": "http://simulator.test",
        }
    )


def valid_payload(**overrides: Any) -> dict[str, Any]:
    """A request that passes validation against ``node_directories``."""
    payload: dict[str, Any] = {
        "algorithm": "algorithm_template.py",
        "scenario": {"env_name": FAKE_ENVIRONMENT_NAME, "scenario_name": FAKE_CHRONIC_NAME},
        "session_duration_seconds": 3600,
    }
    payload.update(overrides)
    return payload


class FakeClock:
    """
    Session clock whose time only moves when the session waits.

    With ``block_waits`` set, every wait blocks until ``release`` is called or
    the session is stopped, which keeps a session live for as long as a test
    needs it.
    """

    def __init__(self, start: datetime = FAKE_CLOCK_START, block_waits: bool = False) -> None:
        self.current_time = start
        self.block_waits = block_waits
        self.released = threading.Event()
        self.waiting = threading.Event()
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self.current_time

    def wait(self, stop_event: threading.Event, seconds: float) -> bool:
        self.waiting.set()
        if self.block_waits:
            while not (self.released.is_set() or stop_event.is_set()):
                stop_event.wait(0.01)
        with self._lock:
            self.current_time += timedelta(seconds=seconds)
        return stop_event.is_set()

    def release(self) -> None:
        self.released.set()


@pytest.fixture
def fake_clock() -> FakeClock:
    """A clock that advances instantly through every wait."""
    return FakeClock()
