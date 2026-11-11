"""
Session config — decoding and validating a ``RunHAISession`` request.

A request is one inline base64 JSON input (the benchmarking convention). It
names an algorithm file and a grid2op scenario that the node operator has placed
in the service's directories, and sets the session's timing. It never carries
code or paths: names are checked to be bare, so a caller can choose among what
is on the node but cannot point the service anywhere else.

Validation is complete and does no network I/O (NFR-01), so a bad request is
rejected by ``/control/execute`` itself rather than becoming a job that fails
later.

Spec coverage: FR-02, FR-03, section 5.3, NFR-07
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from settings import (
    CHRONIC_FILE_EXTENSIONS,
    DEFAULT_ALGORITHM,
    DEFAULT_ENV_SEED,
    DEFAULT_SCENARIO_FIRST_STEP,
    DEFAULT_STEP_DURATION_SECONDS,
    MAXIMUM_SESSION_DURATION_SECONDS,
    MAXIMUM_STEP_DURATION_SECONDS,
    MINIMUM_SESSION_DURATION_SECONDS,
    REQUIRED_CHRONIC_SERIES,
)

# Keys a request may carry; anything else is rejected so a typo such as
# "max_step" fails loudly instead of being ignored (section 5.3).
ALLOWED_TOP_LEVEL_KEYS = frozenset({"algorithm", "scenario", "session_duration_seconds", "kpis"})
ALLOWED_SCENARIO_KEYS = frozenset(
    {"env_name", "scenario_name", "scenario_first_step", "env_seed", "step_duration_s"}
)


@dataclass(frozen=True)
class ScenarioSelection:
    """
    The grid2op scenario a session plays, as the request selected it.

    Attributes:
        env_name: Directory name of the grid2op environment in the scenarios folder.
        scenario_name: Chronic folder name inside ``<env_name>/chronics``.
        scenario_first_step: Step the operator takes over from.
        env_seed: Seed of the grid2op environment.
        step_duration_s: Seconds between simulator steps outside events.
    """

    env_name: str
    scenario_name: str
    scenario_first_step: int
    env_seed: int
    step_duration_s: float


@dataclass(frozen=True)
class SessionConfig:
    """
    A validated ``RunHAISession`` request, with defaults filled in.

    Attributes:
        algorithm: File name of the algorithm in the algorithms folder.
        scenario: The selected scenario.
        session_duration_seconds: Time from go-live to result collection.
        kpis: Requested KPI names; accepted and reserved, not computed.
        algorithm_path: Resolved path of the algorithm file.
        scenario_directory: Resolved path of the grid2op environment directory.
    """

    algorithm: str
    scenario: ScenarioSelection
    session_duration_seconds: int
    kpis: tuple[str, ...]
    algorithm_path: Path = field(compare=False)
    scenario_directory: Path = field(compare=False)

    def to_result_dict(self) -> dict[str, Any]:
        """
        Return the request as it is recorded in the result's ``config`` block.

        Returns:
            The validated request with defaults filled in, without local paths.
        """
        return {
            "algorithm": self.algorithm,
            "scenario": asdict(self.scenario),
            "session_duration_seconds": self.session_duration_seconds,
            "kpis": list(self.kpis),
        }

    def kpi_warnings(self) -> list[str]:
        """
        Return the warning recorded when KPIs were requested (section 5.3).

        Returns:
            One warning naming the requested KPIs, or an empty list.
        """
        if not self.kpis:
            return []
        return [f"kpis requested but not computed in this version: {list(self.kpis)}"]


def decode_inline_json(input_reference: dict[str, Any]) -> dict[str, Any]:
    """
    Decode an inline DataReference carrying base64-encoded JSON.

    Args:
        input_reference: One entry of ``ExecuteRequest.inputs``.

    Returns:
        The decoded JSON object.

    Raises:
        ValueError: If the uri is empty or not base64-encoded JSON.
    """
    encoded_payload = input_reference.get("uri", "")
    if not encoded_payload:
        raise ValueError("Inline input uri is empty")
    try:
        return json.loads(base64.b64decode(encoded_payload).decode("utf-8"))
    except Exception as decode_error:
        raise ValueError(f"Invalid inline base64 JSON payload: {decode_error}") from decode_error


def read_request_payload(inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Read the session request from the first input.

    Args:
        inputs: ``ExecuteRequest.inputs``.

    Returns:
        The decoded payload.

    Raises:
        ValueError: If there is no input, it is not inline, or it is not a JSON object.
    """
    if not inputs:
        raise ValueError("RunHAISession needs one inline JSON input; none was given")
    input_reference = inputs[0]
    protocol = str(input_reference.get("protocol", "")).lower()
    if protocol != "inline":
        raise ValueError(f"Unsupported input protocol: {protocol!r}. Expected 'inline'.")
    payload = decode_inline_json(input_reference)
    if not isinstance(payload, dict):
        raise ValueError("The RunHAISession input must be a JSON object")
    return payload


def require_plain_name(name: Any, field_name: str) -> str:
    """
    Accept a bare file or directory name, rejecting anything path-like (NFR-07).

    Args:
        name: The value from the request.
        field_name: Dotted field name, used in the error message.

    Returns:
        The name, stripped of surrounding whitespace.

    Raises:
        ValueError: If the name is empty or contains a path component.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{field_name} must be a non-empty name")
    name = name.strip()
    if name in (".", "..") or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError(f"{field_name} must be a bare name, not a path: {name!r}")
    return name


def _require_integer(value: Any, field_name: str, minimum: int, maximum: int | None = None) -> int:
    """
    Accept an integer within bounds; booleans and floats are rejected.

    Raises:
        ValueError: If the value is not an int or out of bounds.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer, got {value!r}")
    if value < minimum or (maximum is not None and value > maximum):
        upper_bound = "" if maximum is None else f" and at most {maximum}"
        raise ValueError(f"{field_name} must be at least {minimum}{upper_bound}, got {value}")
    return value


def _require_step_duration(value: Any) -> float:
    """
    Accept a step duration in (0, MAXIMUM_STEP_DURATION_SECONDS] seconds.

    Raises:
        ValueError: If the value is not a number or out of bounds.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"scenario.step_duration_s must be a number, got {value!r}")
    if not 0 < value <= MAXIMUM_STEP_DURATION_SECONDS:
        raise ValueError(
            f"scenario.step_duration_s must be > 0 and <= {MAXIMUM_STEP_DURATION_SECONDS}, got {value}"
        )
    return float(value)


def _reject_unknown_keys(block: dict[str, Any], allowed_keys: frozenset[str], block_name: str) -> None:
    """
    Raise if a request block carries keys the schema does not define.

    Raises:
        ValueError: Listing the unknown keys.
    """
    unknown_keys = sorted(set(block) - allowed_keys)
    if unknown_keys:
        raise ValueError(f"Unknown {block_name} keys: {unknown_keys}. Allowed: {sorted(allowed_keys)}")


def resolve_algorithm(name: Any, algorithm_directory: Path) -> Path:
    """
    Resolve an algorithm name to its ``.py`` file in the algorithms folder.

    Args:
        name: The ``algorithm`` value from the request.
        algorithm_directory: The folder algorithms are selected from.

    Returns:
        The algorithm file's path.

    Raises:
        ValueError: If the name is not a ``.py`` file present there.
    """
    file_name = require_plain_name(name, "algorithm")
    if not file_name.endswith(".py"):
        raise ValueError(f"algorithm must name a .py file: {file_name!r}")
    algorithm_path = algorithm_directory / file_name
    if not algorithm_path.is_file():
        available = sorted(path.name for path in algorithm_directory.glob("*.py"))
        raise ValueError(f"Unknown algorithm {file_name!r}. Available: {available}")
    return algorithm_path


def _find_missing_chronic_series(chronic_directory: Path) -> list[str]:
    """Return the required time series that have no file in a chronic folder."""
    return [
        series_name
        for series_name in REQUIRED_CHRONIC_SERIES
        if not any(
            (chronic_directory / f"{series_name}{extension}").is_file()
            for extension in CHRONIC_FILE_EXTENSIONS
        )
    ]


def resolve_scenario_directory(env_name: str, scenario_name: str, scenario_directory: Path) -> Path:
    """
    Check that a scenario exists in the simulator's format and return its env directory.

    The simulator builds its environment from per-chronic CSV handlers, so the
    chronic folder must hold every series in ``REQUIRED_CHRONIC_SERIES``.
    Built-in grid2op environment names are not supported: the simulator needs
    the environment on disk.

    Args:
        env_name: Validated bare name of the environment directory.
        scenario_name: Validated bare name of the chronic folder.
        scenario_directory: The folder scenarios are selected from.

    Returns:
        The environment directory's path.

    Raises:
        ValueError: Naming the field and what is missing.
    """
    environment_directory = scenario_directory / env_name
    if not (environment_directory / "chronics").is_dir():
        available = sorted(
            path.name for path in scenario_directory.iterdir() if (path / "chronics").is_dir()
        ) if scenario_directory.is_dir() else []
        raise ValueError(
            f"scenario.env_name {env_name!r} is not an environment directory with chronics/ "
            f"in the scenarios folder. Available: {available}"
        )

    chronic_directory = environment_directory / "chronics" / scenario_name
    if not chronic_directory.is_dir():
        raise ValueError(
            f"scenario.scenario_name {scenario_name!r} is not a chronic folder of {env_name!r}"
        )

    missing_series = _find_missing_chronic_series(chronic_directory)
    if missing_series:
        raise ValueError(
            f"scenario.scenario_name {scenario_name!r} lacks required time series "
            f"{missing_series} (as .csv or .csv.bz2)"
        )
    return environment_directory


def _parse_scenario(raw_scenario: Any, scenario_directory: Path) -> tuple[ScenarioSelection, Path]:
    """
    Validate the ``scenario`` block.

    Returns:
        The selection and the resolved environment directory.

    Raises:
        ValueError: Naming the offending field.
    """
    if not isinstance(raw_scenario, dict):
        raise ValueError("scenario is required and must be an object")
    _reject_unknown_keys(raw_scenario, ALLOWED_SCENARIO_KEYS, "scenario")

    env_name = require_plain_name(raw_scenario.get("env_name"), "scenario.env_name")
    scenario_name = require_plain_name(raw_scenario.get("scenario_name"), "scenario.scenario_name")
    selection = ScenarioSelection(
        env_name=env_name,
        scenario_name=scenario_name,
        scenario_first_step=_require_integer(
            raw_scenario.get("scenario_first_step", DEFAULT_SCENARIO_FIRST_STEP),
            "scenario.scenario_first_step",
            minimum=0,
        ),
        env_seed=_require_integer(
            raw_scenario.get("env_seed", DEFAULT_ENV_SEED), "scenario.env_seed", minimum=0
        ),
        step_duration_s=_require_step_duration(
            raw_scenario.get("step_duration_s", DEFAULT_STEP_DURATION_SECONDS)
        ),
    )
    environment_directory = resolve_scenario_directory(env_name, scenario_name, scenario_directory)
    return selection, environment_directory


def _parse_kpis(raw_kpis: Any) -> tuple[str, ...]:
    """
    Validate the reserved ``kpis`` list.

    Raises:
        ValueError: If it is not a list of strings.
    """
    if not isinstance(raw_kpis, list) or not all(isinstance(kpi, str) for kpi in raw_kpis):
        raise ValueError(f"kpis must be a list of strings, got {raw_kpis!r}")
    return tuple(raw_kpis)


def parse_session_config(
    payload: dict[str, Any], algorithm_directory: Path, scenario_directory: Path
) -> SessionConfig:
    """
    Validate a decoded request against the section 5.3 schema.

    Args:
        payload: The decoded request.
        algorithm_directory: The folder algorithms are selected from.
        scenario_directory: The folder scenarios are selected from.

    Returns:
        The validated configuration with defaults filled in.

    Raises:
        ValueError: On the first invalid field, naming it.
    """
    _reject_unknown_keys(payload, ALLOWED_TOP_LEVEL_KEYS, "top-level")

    if "session_duration_seconds" not in payload:
        raise ValueError("session_duration_seconds is required")
    session_duration_seconds = _require_integer(
        payload["session_duration_seconds"],
        "session_duration_seconds",
        minimum=MINIMUM_SESSION_DURATION_SECONDS,
        maximum=MAXIMUM_SESSION_DURATION_SECONDS,
    )

    algorithm_path = resolve_algorithm(payload.get("algorithm", DEFAULT_ALGORITHM), algorithm_directory)
    scenario, environment_directory = _parse_scenario(payload.get("scenario"), scenario_directory)

    return SessionConfig(
        algorithm=algorithm_path.name,
        scenario=scenario,
        session_duration_seconds=session_duration_seconds,
        kpis=_parse_kpis(payload.get("kpis", [])),
        algorithm_path=algorithm_path,
        scenario_directory=environment_directory,
    )
