"""Grid2Benchmark service for AI-Effect orchestration.

Exposes one operation, ``RunBenchmark``. Each function named
``execute_<MethodName>`` is exposed as an operation, so helpers here are kept
private with a leading underscore.

RunBenchmark evaluates an algorithm against one or more grid2op scenarios with
the grid2benchmark package and stores its result as JSON. The benchmark is a
standalone service: it does not consume data from the data synthesizer.

Both inputs are selected by name from directories the operator fills:

- the algorithm is a ``.py`` file in ``ALGORITHM_DIRECTORY`` implementing
  ``build_agent(env, context)``;
- a scenario is either a grid2op environment directory in
  ``SCENARIO_DIRECTORY`` or the name of a grid2op built-in environment such as
  ``l2rpn_case14_sandbox``.

The request carries names only, never paths or code, so a caller can choose
among what has been placed on the node but cannot point the service elsewhere.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import grid2benchmark

from common.batch_jobs import BatchJobRunner, build_runner
from handler import ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)

# Logical format recorded on the stored artifact and carried in the
# DataReference handed back to the caller.
BENCHMARK_RESULT_FORMAT = "BenchmarkResult"

SERVICE_DIRECTORY = Path(__file__).resolve().parent

# Where algorithm files and grid2op scenario directories are dumped. Both are
# bind-mounted in docker-compose.yml, so new ones need no image rebuild.
ALGORITHM_DIRECTORY = Path(
    os.environ.get("ALGORITHM_DIRECTORY", SERVICE_DIRECTORY / "algorithms")
)
SCENARIO_DIRECTORY = Path(
    os.environ.get("SCENARIO_DIRECTORY", SERVICE_DIRECTORY / "scenarios")
)

DEFAULT_ALGORITHM = "algorithm_template.py"

# KPIs grid2benchmark accepts but whose values are not yet trustworthy. A
# request selecting one still runs; the warning is logged, stored in the
# result's metadata, and appended to the error if the run fails.
KPI_WARNINGS = {
    "network_utilization": (
        "network_utilization (N-1) has a known bug in grid2evaluate and its "
        "values must not be relied on: it fails on grids whose element names "
        "differ between grid2op and the pypowsybl conversion (e.g. "
        "l2rpn_case14_sandbox), and with the pyarrow 19 this image installs it "
        "skips every time step, so its N-1 values come out as 0."
    ),
}

# Built on first use so importing this module opens no filesystem handles and
# reads no environment, which keeps it importable in tests.
_job_runner: BatchJobRunner | None = None


def set_job_runner(runner: BatchJobRunner | None) -> None:
    """
    Install a job runner for this process.

    Args:
        runner: Runner to use, or None to clear it so the next call builds a
            fresh one. Tests inject a runner writing to a temporary directory.
    """
    global _job_runner
    _job_runner = runner


def get_job_runner() -> BatchJobRunner:
    """
    Return the process-wide job runner, building it on first use.

    Returns:
        The shared BatchJobRunner.
    """
    global _job_runner
    if _job_runner is None:
        _job_runner = build_runner()
    return _job_runner


def _decode_inline_json(input_ref: dict[str, Any]) -> dict[str, Any]:
    """
    Decode an inline DataReference carrying base64-encoded JSON.

    Raises:
        ValueError: If the uri is empty or not base64-encoded JSON.
    """
    raw = input_ref.get("uri", "")
    if not raw:
        raise ValueError("Inline input uri is empty")

    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid inline base64 JSON payload: {exc}") from exc


def _read_payload(inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Read the benchmark request from the first input. No input means defaults.

    Raises:
        ValueError: If the input is not an inline reference.
    """
    if not inputs:
        return {}

    input_ref = inputs[0]
    protocol = str(input_ref.get("protocol", "")).lower()
    if protocol != "inline":
        raise ValueError(f"Unsupported input protocol: {protocol}. Expected 'inline'.")
    return _decode_inline_json(input_ref)


def _require_plain_name(name: Any, field_name: str) -> str:
    """
    Accept a bare file or directory name, rejecting anything path-like.

    Names come from the request, so a separator or parent reference would let a
    caller reach outside the directories the operator controls.

    Raises:
        ValueError: If the name is empty or contains a path component.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{field_name} must be a non-empty name")
    name = name.strip()
    if name in (".", "..") or Path(name).name != name or "\\" in name:
        raise ValueError(f"{field_name} must be a bare name, not a path: {name!r}")
    return name


def _resolve_algorithm(name: Any) -> Path:
    """
    Resolve an algorithm name to its file in ALGORITHM_DIRECTORY.

    Raises:
        ValueError: If the name is not a ``.py`` file present there.
    """
    file_name = _require_plain_name(name, "algorithm")
    if not file_name.endswith(".py"):
        raise ValueError(f"algorithm must name a .py file: {file_name!r}")

    algorithm_path = ALGORITHM_DIRECTORY / file_name
    if not algorithm_path.is_file():
        available = sorted(path.name for path in ALGORITHM_DIRECTORY.glob("*.py"))
        raise ValueError(f"Unknown algorithm {file_name!r}. Available: {available}")
    return algorithm_path


def _resolve_scenario_env(name: Any) -> str:
    """
    Resolve a scenario name to what ``grid2op.make()`` accepts.

    A directory of that name in SCENARIO_DIRECTORY wins; otherwise the name is
    passed through as a grid2op built-in environment.

    Raises:
        ValueError: If the name is empty or path-like.
    """
    scenario_name = _require_plain_name(name, "scenario env_name")
    scenario_path = SCENARIO_DIRECTORY / scenario_name
    if scenario_path.is_dir():
        return str(scenario_path)
    return scenario_name


def _build_scenario(raw_scenario: dict[str, Any]) -> grid2benchmark.ScenarioConfig:
    """
    Build one grid2benchmark scenario from its request entry.

    grid2benchmark validates the values (backend, time series ids) itself.

    Raises:
        ValueError: If the entry is malformed or rejected by grid2benchmark.
    """
    if not isinstance(raw_scenario, dict):
        raise ValueError("each scenario must be an object")

    scenario_kwargs: dict[str, Any] = {}
    if raw_scenario.get("env_name") is not None:
        scenario_kwargs["env_name"] = _resolve_scenario_env(raw_scenario["env_name"])
    if raw_scenario.get("time_series_ids") is not None:
        scenario_kwargs["time_series_ids"] = tuple(
            int(time_series_id) for time_series_id in raw_scenario["time_series_ids"]
        )
    if raw_scenario.get("backend") is not None:
        scenario_kwargs["backend"] = str(raw_scenario["backend"])

    return grid2benchmark.ScenarioConfig(**scenario_kwargs)


def _build_benchmark_config(payload: dict[str, Any]) -> grid2benchmark.BenchmarkConfig:
    """
    Build the grid2benchmark configuration from the request's ``benchmark`` block.

    Accepts either a ``scenarios`` list or the fields of a single scenario
    directly in the block. Omitted values take grid2benchmark's defaults.

    Raises:
        ValueError: If the block is malformed or rejected by grid2benchmark.
    """
    benchmark_block = payload.get("benchmark", {})
    if not isinstance(benchmark_block, dict):
        raise ValueError("benchmark must be an object")

    raw_scenarios = benchmark_block.get("scenarios", [benchmark_block])
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("benchmark.scenarios must be a non-empty list")

    config_kwargs: dict[str, Any] = {
        "scenarios": tuple(_build_scenario(raw) for raw in raw_scenarios),
    }
    if benchmark_block.get("max_steps") is not None:
        config_kwargs["max_steps"] = int(benchmark_block["max_steps"])
    if benchmark_block.get("kpis") is not None:
        config_kwargs["kpis"] = tuple(benchmark_block["kpis"])

    return grid2benchmark.BenchmarkConfig(**config_kwargs)


def _kpi_warnings(config: grid2benchmark.BenchmarkConfig) -> list[str]:
    """Return the known-issue warnings for the KPIs a configuration selects."""
    return [KPI_WARNINGS[kpi] for kpi in config.kpis if kpi in KPI_WARNINGS]


def _package_version(package_name: str) -> str:
    """Return an installed package's version, or 'unknown'."""
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unknown"


def execute_RunBenchmark(request: ExecuteRequest) -> ExecuteResponse:
    """Start a benchmark run and return immediately with its job id.

    Input (inline base64 JSON, every field optional):
        algorithm: File name in ALGORITHM_DIRECTORY (default
            ``algorithm_template.py``).
        benchmark:
            max_steps: Steps per episode (grid2benchmark default 200).
            kpis: KPI names (grid2benchmark default: carbon_intensity,
                operation_score, topological_action_complexity). The opt-in
                ``network_utilization`` (N-1) and ``assistant_alert_accuracy``
                are accepted too; see KPI_WARNINGS for the former.
            scenarios: List of scenarios, each with
                env_name: Directory in SCENARIO_DIRECTORY or a grid2op
                    built-in environment name.
                time_series_ids: Time series to run (default: all).
                backend: ``pandapower``, ``lightsim2grid`` or ``pypowsybl``.
            The fields of a single scenario may also sit directly in the block.

    The request is validated and the algorithm loaded on the calling thread, so
    a bad request is rejected now rather than becoming a background job that
    fails later. The run itself takes minutes and goes to the job runner;
    callers poll ``/control/status/{job_id}`` and fetch the result from the URL
    ``/control/output/{job_id}`` returns.

    Returns:
        An ExecuteResponse with status ``running``, or ``failed`` when the
        request is unusable or the service is already at capacity.
    """
    try:
        payload = _read_payload(request.inputs)
        algorithm_path = _resolve_algorithm(payload.get("algorithm", DEFAULT_ALGORITHM))
        grid2benchmark.validate_algorithm(
            grid2benchmark.load_algorithm_from_file(algorithm_path)
        )
        config = _build_benchmark_config(payload)
    except Exception as validation_error:  # noqa: BLE001 - reported to the caller
        logger.exception("RunBenchmark rejected: invalid request")
        return ExecuteResponse(
            status="failed", task_id=request.task_id, error=str(validation_error)
        )

    warnings = _kpi_warnings(config)
    for warning in warnings:
        logger.warning("RunBenchmark task %s: %s", request.task_id, warning)

    def run_benchmark(report_progress) -> dict:
        """Run grid2benchmark and attach what was run with which versions."""
        report_progress(10)
        try:
            result = grid2benchmark.run_benchmark(algorithm_path, config)
        except SystemExit as exit_request:
            # Library code that calls sys.exit() would otherwise end the job
            # thread silently and leave the job "running" forever, because the
            # job runner only catches Exception.
            raise RuntimeError(
                f"Benchmark code called sys.exit({exit_request.code})"
            ) from exit_request
        except Exception as benchmark_error:
            if not warnings:
                raise
            raise RuntimeError(
                f"{benchmark_error}. Known issue: {' '.join(warnings)}"
            ) from benchmark_error
        report_progress(90)
        result["metadata"] = {
            "algorithm": algorithm_path.name,
            "grid2benchmark_version": _package_version("grid2benchmark"),
            "grid2op_version": _package_version("grid2op"),
            "warnings": warnings,
        }
        return result

    return get_job_runner().submit(request, run_benchmark, data_format=BENCHMARK_RESULT_FORMAT)


if __name__ == "__main__":
    import sys
    run(sys.modules[__name__], artifact_store=get_job_runner().artifact_store)
