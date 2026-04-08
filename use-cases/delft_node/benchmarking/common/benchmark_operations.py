"""Grid2Benchmark operations for AI-Effect orchestration.

RunBenchmark keeps the existing request and response contract while delegating
episode execution and KPI computation to the external grid2benchmark package.
"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .control_interface import (
    DataReference,
    ExecuteRequest,
    ExecuteResponse,
    get_data_url,
)
from .task_manager import get_task_manager

logger = logging.getLogger(__name__)

DEFAULT_ENV_NAME = "l2rpn_case14_sandbox"
DEFAULT_MAX_STEPS = 200
DEFAULT_EPISODES = 1

REQUIRED_ALGORITHM_FUNCTION = "build_agent"


@dataclass(frozen=True)
class ScenarioConfig:
    env_name: str
    time_series_ids: tuple[int, ...] | None = None
    env_path: str | None = None


@dataclass(frozen=True)
class BenchmarkConfig:
    max_steps: int
    scenarios: tuple[ScenarioConfig, ...]
    kpis: tuple[str, ...] | None = None

    @property
    def primary_env_name(self) -> str:
        return self.scenarios[0].env_name if self.scenarios else DEFAULT_ENV_NAME


def _decode_inline_json(input_ref: dict[str, Any]) -> dict[str, Any]:
    if input_ref.get("protocol") != "inline":
        raise ValueError("Input protocol must be 'inline' for RunBenchmark")

    raw = input_ref.get("uri", "")
    if not raw:
        raise ValueError("Inline input uri is empty")

    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid inline base64 JSON payload: {exc}") from exc


def _parse_benchmark_config(payload: dict[str, Any]) -> BenchmarkConfig:
    benchmark_cfg = payload.get("benchmark", {})
    max_steps = int(benchmark_cfg.get("max_steps", DEFAULT_MAX_STEPS))

    if max_steps <= 0:
        raise ValueError("benchmark.max_steps must be > 0")

    raw_kpis = benchmark_cfg.get("kpis")
    kpis = _parse_kpis(raw_kpis)

    raw_scenarios = benchmark_cfg.get("scenarios")
    if raw_scenarios is not None:
        scenarios = _parse_scenarios(raw_scenarios)
    else:
        env_name = str(benchmark_cfg.get("env_name", DEFAULT_ENV_NAME))
        env_path = benchmark_cfg.get("env_path")
        raw_time_series_ids = benchmark_cfg.get("time_series_ids")

        if raw_time_series_ids is not None:
            time_series_ids = _parse_time_series_ids(raw_time_series_ids)
        else:
            episodes = int(benchmark_cfg.get("episodes", DEFAULT_EPISODES))
            if episodes <= 0:
                raise ValueError("benchmark.episodes must be > 0")
            time_series_ids = tuple(range(episodes))

        scenarios = (
            ScenarioConfig(
                env_name=env_name,
                time_series_ids=time_series_ids,
                env_path=str(env_path) if env_path else None,
            ),
        )

    return BenchmarkConfig(max_steps=max_steps, scenarios=scenarios, kpis=kpis)


def _parse_kpis(raw_kpis: Any) -> tuple[str, ...] | None:
    if raw_kpis is None:
        return None

    if isinstance(raw_kpis, str):
        parsed = tuple(part.strip() for part in raw_kpis.split(",") if part.strip())
    elif isinstance(raw_kpis, (list, tuple)):
        parsed = tuple(str(item).strip() for item in raw_kpis if str(item).strip())
    else:
        raise ValueError("benchmark.kpis must be a string or list of KPI names")

    if not parsed:
        raise ValueError("benchmark.kpis must contain at least one KPI name")

    return parsed


def _parse_time_series_ids(raw_ids: Any) -> tuple[int, ...] | None:
    if raw_ids is None:
        return None

    if isinstance(raw_ids, int):
        parsed = (int(raw_ids),)
    elif isinstance(raw_ids, str):
        parsed = tuple(int(part.strip()) for part in raw_ids.split(",") if part.strip())
    elif isinstance(raw_ids, (list, tuple)):
        parsed = tuple(int(item) for item in raw_ids)
    else:
        raise ValueError(
            "time_series_ids must be an int, comma-separated string, or list of ints"
        )

    if len(parsed) == 0:
        raise ValueError("time_series_ids must not be empty when provided")
    if any(ts_id < 0 for ts_id in parsed):
        raise ValueError("time_series_ids must contain non-negative integers")

    return parsed


def _parse_scenarios(raw_scenarios: Any) -> tuple[ScenarioConfig, ...]:
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("benchmark.scenarios must be a non-empty list")

    scenarios: list[ScenarioConfig] = []
    for index, raw_scenario in enumerate(raw_scenarios):
        if not isinstance(raw_scenario, dict):
            raise ValueError(f"benchmark.scenarios[{index}] must be an object")

        scenarios.append(
            ScenarioConfig(
                env_name=str(raw_scenario.get("env_name", DEFAULT_ENV_NAME)),
                time_series_ids=_parse_time_series_ids(
                    raw_scenario.get("time_series_ids")
                ),
                env_path=(
                    str(raw_scenario["env_path"])
                    if raw_scenario.get("env_path")
                    else None
                ),
            )
        )

    return tuple(scenarios)


def _get_algorithm_source(payload: dict[str, Any]) -> str:
    algorithm = payload.get("algorithm", {})
    source_b64 = algorithm.get("source_b64")
    source_plain = algorithm.get("source")

    if source_b64:
        return base64.b64decode(source_b64).decode("utf-8")
    if source_plain:
        return source_plain

    raise ValueError(
        "algorithm source missing. Provide algorithm.source or algorithm.source_b64"
    )


def _load_algorithm_module(source_code: str) -> ModuleType:
    with tempfile.TemporaryDirectory(prefix="benchmark_algo_") as td:
        module_path = Path(td) / "submitted_algorithm.py"
        module_path.write_text(source_code, encoding="utf-8")

        spec = importlib.util.spec_from_file_location(
            "submitted_algorithm", module_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Failed to create module spec for algorithm source")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _validate_algorithm_module(module: ModuleType) -> None:
    build_agent = getattr(module, REQUIRED_ALGORITHM_FUNCTION, None)
    if build_agent is None or not callable(build_agent):
        raise ValueError(
            "Algorithm template invalid: required callable build_agent(env, context) not found"
        )


def _compute_manual_kpis(episode_results: list[dict[str, Any]]) -> dict[str, Any]:
    if not episode_results:
        return {
            "survival": {
                "episode_lengths": [],
                "average_episode_length": 0.0,
            },
            "violations": {
                "overload_violations_per_episode": [],
                "total_overload_violations": 0,
            },
            "latency": {
                "runtime_seconds_per_episode": [],
                "total_runtime_seconds": 0.0,
                "average_runtime_seconds": 0.0,
            },
        }

    total_steps = sum(e["steps"] for e in episode_results)
    total_violations = sum(e["overload_violations"] for e in episode_results)
    total_runtime = sum(e["runtime_seconds"] for e in episode_results)

    return {
        "survival": {
            "episode_lengths": [e["steps"] for e in episode_results],
            "average_episode_length": total_steps / len(episode_results),
        },
        "violations": {
            "overload_violations_per_episode": [
                e["overload_violations"] for e in episode_results
            ],
            "total_overload_violations": total_violations,
        },
        "latency": {
            "runtime_seconds_per_episode": [
                e["runtime_seconds"] for e in episode_results
            ],
            "total_runtime_seconds": total_runtime,
            "average_runtime_seconds": total_runtime / len(episode_results),
        },
    }


def _object_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, dict):
            return dumped

    if hasattr(value, "__dict__"):
        return dict(value.__dict__)

    raise TypeError(f"Unsupported grid2benchmark result type: {type(value).__name__}")


def _normalize_episode_results(raw_episodes: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_episodes, list):
        return []

    normalized: list[dict[str, Any]] = []
    for index, episode in enumerate(raw_episodes):
        episode_dict = (
            _object_to_dict(episode) if not isinstance(episode, dict) else episode
        )
        normalized.append(
            {
                "episode_index": int(episode_dict.get("episode_index", index)),
                "steps": int(episode_dict.get("steps", 0)),
                "overload_violations": int(
                    episode_dict.get(
                        "overload_violations", episode_dict.get("violations", 0)
                    )
                ),
                "runtime_seconds": float(
                    episode_dict.get(
                        "runtime_seconds", episode_dict.get("runtime", 0.0)
                    )
                ),
                "terminated": bool(episode_dict.get("terminated", True)),
            }
        )

    return normalized


def _normalize_benchmark_result(
    raw_result: Any,
    config: BenchmarkConfig,
    payload: dict[str, Any],
) -> dict[str, Any]:
    result_dict = _object_to_dict(raw_result)
    raw_scenarios = result_dict.get("scenarios", [])
    scenarios = raw_scenarios if isinstance(raw_scenarios, list) else []

    episode_results: list[dict[str, Any]] = []
    scenario_metadata: list[dict[str, Any]] = []
    primary_environment: dict[str, Any] | None = None
    primary_kpis: dict[str, Any] | None = None

    for scenario_index, scenario in enumerate(scenarios):
        scenario_dict = (
            _object_to_dict(scenario) if not isinstance(scenario, dict) else scenario
        )
        scenario_episodes = _normalize_episode_results(
            scenario_dict.get("episodes", [])
        )

        for episode in scenario_episodes:
            episode["scenario_index"] = scenario_index

        scenario_environment = scenario_dict.get("environment")
        if not isinstance(scenario_environment, dict):
            scenario_environment = {
                "env_name": config.primary_env_name,
                "fixed_environment": True,
            }

        scenario_kpis = scenario_dict.get("kpis")
        if not isinstance(scenario_kpis, dict):
            scenario_kpis = _compute_manual_kpis(scenario_episodes)
            scenario_kpis["evaluation_backend"] = "grid2benchmark_manual"

        scenario_metadata.append(
            {
                "scenario_index": int(
                    scenario_dict.get("scenario_index", scenario_index)
                ),
                "environment": scenario_environment,
                "executed_time_series_ids": scenario_dict.get(
                    "executed_time_series_ids", []
                ),
                "episode_count": len(scenario_episodes),
                "kpis": scenario_kpis,
            }
        )
        episode_results.extend(scenario_episodes)

        if primary_environment is None:
            primary_environment = scenario_environment
        if primary_kpis is None:
            primary_kpis = scenario_kpis

    environment = primary_environment or {
        "env_name": config.primary_env_name,
        "fixed_environment": True,
    }
    kpis = primary_kpis or _compute_manual_kpis(episode_results)
    if "evaluation_backend" not in kpis:
        kpis["evaluation_backend"] = "grid2benchmark_manual"

    input_summary = result_dict.get("input_summary")
    if not isinstance(input_summary, dict):
        input_summary = {
            "topology_keys": sorted(payload.get("grid_topology", {}).keys()),
            "time_series_keys": sorted(payload.get("time_series", {}).keys()),
        }

    normalized = {
        "environment": environment,
        "input_summary": input_summary,
        "episodes": episode_results,
        "kpis": kpis,
    }

    metadata: dict[str, Any] = {
        "scenario_count": len(scenarios),
        "scenarios": scenario_metadata,
    }

    summary = result_dict.get("summary")
    if isinstance(summary, dict):
        metadata["summary"] = summary

    if isinstance(result_dict.get("metadata"), dict):
        metadata.update(result_dict["metadata"])

    normalized["metadata"] = metadata

    return normalized


def _invoke_grid2benchmark(
    config: BenchmarkConfig,
    algorithm_module: ModuleType,
    source_code: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    _ = (algorithm_module, payload)
    try:
        grid2benchmark = importlib.import_module("grid2benchmark")
    except Exception as exc:
        raise RuntimeError(
            "grid2benchmark package is not installed or not importable"
        ) from exc

    run_benchmark = getattr(grid2benchmark, "run_benchmark", None)
    package_benchmark_config = getattr(grid2benchmark, "BenchmarkConfig", None)
    package_scenario_config = getattr(grid2benchmark, "ScenarioConfig", None)

    if not callable(run_benchmark):
        raise RuntimeError("grid2benchmark.run_benchmark is not available")
    if not callable(package_benchmark_config) or not callable(package_scenario_config):
        raise RuntimeError(
            "grid2benchmark.BenchmarkConfig and ScenarioConfig must be available"
        )

    scenarios = tuple(
        package_scenario_config(
            env_name=scenario.env_name,
            time_series_ids=scenario.time_series_ids,
            env_path=scenario.env_path,
        )
        for scenario in config.scenarios
    )

    benchmark_config_kwargs: dict[str, Any] = {
        "scenarios": scenarios,
        "max_steps": config.max_steps,
    }
    if config.kpis is not None:
        benchmark_config_kwargs["kpis"] = config.kpis

    benchmark_config = package_benchmark_config(**benchmark_config_kwargs)
    result = run_benchmark(source_code, benchmark_config)
    return _normalize_benchmark_result(result, config, payload)


def execute_RunBenchmark(request: ExecuteRequest) -> ExecuteResponse:
    """Run the benchmark with user template algorithm against grid2benchmark."""
    if not request.inputs:
        return ExecuteResponse(status="failed", error="No benchmark input provided")

    try:
        payload = _decode_inline_json(request.inputs[0])
        config = _parse_benchmark_config(payload)
        source_code = _get_algorithm_source(payload)

        module = _load_algorithm_module(source_code)
        _validate_algorithm_module(module)

        benchmark_result = _invoke_grid2benchmark(config, module, source_code, payload)

        result_json = json.dumps(benchmark_result, indent=2)
        get_task_manager().store_data(request.task_id, result_json, "json")

        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="http",
                uri=get_data_url(request.task_id),
                format="json",
            ),
        )
    except Exception as exc:
        logger.exception("RunBenchmark failed")
        return ExecuteResponse(status="failed", error=str(exc))


benchmark_handlers = {
    "RunBenchmark": execute_RunBenchmark,
}
