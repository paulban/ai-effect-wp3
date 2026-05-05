"""Grid2Benchmark operations for AI-Effect orchestration.

RunBenchmark supports gRPC data-plane inputs from the Delft data synthesizer while
retaining inline/http control payload compatibility for benchmark config and
algorithm source.
"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import json
import logging
import os
import tempfile
import threading
from concurrent import futures
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

import grpc

from .control_interface import (
    DataReference,
    ExecuteRequest,
    ExecuteResponse,
    get_data_url,
)
from .proto_runtime import ensure_generated
from .task_manager import get_task_manager

logger = logging.getLogger(__name__)

ensure_generated("common.proto", "data_synthesizer.proto", "benchmarking.proto")
import benchmarking_pb2  # type: ignore  # noqa: E402
import benchmarking_pb2_grpc  # type: ignore  # noqa: E402
import data_synthesizer_pb2  # type: ignore  # noqa: E402
import data_synthesizer_pb2_grpc  # type: ignore  # noqa: E402

DEFAULT_ENV_NAME = "l2rpn_case14_sandbox"
DEFAULT_MAX_STEPS = 200

REQUIRED_ALGORITHM_FUNCTION = "build_agent"

_cache_lock = threading.Lock()
_cached_result_response: benchmarking_pb2.GetBenchmarkResultResponse | None = None


class BenchmarkingServicer(benchmarking_pb2_grpc.BenchmarkingServiceServicer):
    """gRPC servicer exposing cached benchmark results."""

    def GetBenchmarkResult(self, request, context):
        with _cache_lock:
            if _cached_result_response is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details("No benchmark result available")
                return benchmarking_pb2.GetBenchmarkResultResponse(
                    success=False,
                    message="No benchmark result available",
                )
            return _cached_result_response


def start_grpc_server():
    """Start the benchmark gRPC data plane server in background."""
    grpc_port = os.environ.get("GRPC_PORT", "50051")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    benchmarking_pb2_grpc.add_BenchmarkingServiceServicer_to_server(
        BenchmarkingServicer(), server
    )
    server.add_insecure_port(f"[::]:{grpc_port}")
    server.start()
    logger.info(f"Benchmark gRPC server started on port {grpc_port}")
    return server


@dataclass(frozen=True)
class TopologySourceConfig:
    format: str
    path: str


@dataclass(frozen=True)
class TimeSeriesSourceConfig:
    format: str
    path: str


@dataclass(frozen=True)
class ScenarioConfig:
    env_name: str
    time_series_ids: tuple[int, ...] | None = None
    topology: TopologySourceConfig | None = None
    time_series: TimeSeriesSourceConfig | None = None
    backend: str | None = None


@dataclass(frozen=True)
class BenchmarkConfig:
    max_steps: int
    scenarios: tuple[ScenarioConfig, ...]
    kpis: tuple[str, ...] | None = None

    @property
    def primary_env_name(self) -> str:
        return self.scenarios[0].env_name if self.scenarios else DEFAULT_ENV_NAME


def _fetch_http_data(uri: str, timeout: float = 60.0) -> str:
    import httpx

    response = httpx.get(uri, timeout=timeout)
    response.raise_for_status()
    return response.text


def _decode_inline_json(input_ref: dict[str, Any]) -> dict[str, Any]:
    if input_ref.get("protocol") != "inline":
        raise ValueError("Input protocol must be 'inline'")

    raw = input_ref.get("uri", "")
    if not raw:
        raise ValueError("Inline input uri is empty")

    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid inline base64 JSON payload: {exc}") from exc


def _fetch_grid_data_from_upstream(
    grpc_uri: str,
) -> data_synthesizer_pb2.GetGridDataResponse:
    channel = grpc.insecure_channel(grpc_uri)
    stub = data_synthesizer_pb2_grpc.DataSynthesizerServiceStub(channel)
    try:
        return stub.GetGridData(data_synthesizer_pb2.GetGridDataRequest())
    finally:
        channel.close()


def _split_inputs(
    inputs: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, data_synthesizer_pb2.GetGridDataResponse | None]:
    payload: dict[str, Any] | None = None
    grid_data_response: data_synthesizer_pb2.GetGridDataResponse | None = None

    for input_ref in inputs:
        protocol = str(input_ref.get("protocol", "")).lower()
        if protocol == "inline":
            payload = _decode_inline_json(input_ref)
            continue

        if protocol in ("http", "https"):
            payload = json.loads(_fetch_http_data(str(input_ref.get("uri", ""))))
            continue

        if protocol == "grpc":
            uri = str(input_ref.get("uri", ""))
            if not uri:
                raise ValueError("gRPC input is missing uri")
            grid_data_response = _fetch_grid_data_from_upstream(uri)
            continue

        raise ValueError(f"Unsupported input protocol: {protocol}")

    return payload, grid_data_response


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
        raw_time_series_ids = benchmark_cfg.get("time_series_ids")
        time_series_ids = _parse_time_series_ids(raw_time_series_ids) or (0,)

        scenarios = (
            ScenarioConfig(
                env_name=env_name,
                time_series_ids=time_series_ids,
                topology=_parse_topology_source(benchmark_cfg.get("topology")),
                time_series=_parse_time_series_source(benchmark_cfg.get("time_series")),
                backend=_parse_backend(benchmark_cfg.get("backend")),
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
                topology=_parse_topology_source(raw_scenario.get("topology")),
                time_series=_parse_time_series_source(raw_scenario.get("time_series")),
                backend=_parse_backend(raw_scenario.get("backend")),
            )
        )

    return tuple(scenarios)


def _parse_topology_source(raw_topology: Any) -> TopologySourceConfig | None:
    source = _parse_source_config(raw_topology, name="topology")
    if source is None:
        return None
    return TopologySourceConfig(format=source["format"], path=source["path"])


def _parse_time_series_source(raw_time_series: Any) -> TimeSeriesSourceConfig | None:
    source = _parse_source_config(raw_time_series, name="time_series")
    if source is None:
        return None
    return TimeSeriesSourceConfig(format=source["format"], path=source["path"])


def _parse_source_config(raw_source: Any, name: str) -> dict[str, str] | None:
    if raw_source is None:
        return None
    if not isinstance(raw_source, dict):
        raise ValueError(f"{name} must be an object when provided")

    fmt = raw_source.get("format")
    path = raw_source.get("path")
    if fmt is None and path is None:
        return None
    if not isinstance(fmt, str) or not fmt.strip():
        raise ValueError(f"{name}.format must be a non-empty string")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{name}.path must be a non-empty string")

    return {"format": fmt.strip(), "path": path.strip()}


def _parse_backend(raw_backend: Any) -> str | None:
    if raw_backend is None:
        return None
    backend = str(raw_backend).strip()
    if not backend:
        raise ValueError("backend must be a non-empty string when provided")
    return backend


def _default_algorithm_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent / "algorithms" / "algorithm_template.py"
    )


def _get_algorithm_from_uri(source_uri: str) -> str:
    parsed = urlparse(source_uri)
    if parsed.scheme in ("http", "https"):
        return _fetch_http_data(source_uri)

    local_path = source_uri
    if parsed.scheme == "file":
        local_path = parsed.path

    path = Path(local_path)
    if not path.exists() or not path.is_file():
        raise ValueError(f"algorithm.source_uri is not a valid file path: {source_uri}")
    return path.read_text(encoding="utf-8")


def _get_algorithm_source(payload: dict[str, Any]) -> str:
    algorithm = payload.get("algorithm", {}) if isinstance(payload, dict) else {}
    source_b64 = algorithm.get("source_b64") if isinstance(algorithm, dict) else None
    source_plain = algorithm.get("source") if isinstance(algorithm, dict) else None
    source_uri = algorithm.get("source_uri") if isinstance(algorithm, dict) else None

    if source_b64:
        return base64.b64decode(source_b64).decode("utf-8")
    if source_plain:
        return source_plain
    if source_uri:
        return _get_algorithm_from_uri(str(source_uri))

    default_path = _default_algorithm_path()
    if default_path.exists():
        logger.info(
            "No algorithm payload provided, using default algorithm_template.py"
        )
        return default_path.read_text(encoding="utf-8")

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
        episode_dict = _object_to_dict(episode)
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
    raw_result: Any, config: BenchmarkConfig
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
        first_scenario = config.scenarios[0] if config.scenarios else None
        topology_payload = (
            {
                "format": first_scenario.topology.format,
                "path": first_scenario.topology.path,
            }
            if first_scenario and first_scenario.topology
            else {}
        )
        time_series_payload = (
            {
                "format": first_scenario.time_series.format,
                "path": first_scenario.time_series.path,
            }
            if first_scenario and first_scenario.time_series
            else {}
        )
        input_summary = {
            "topology_keys": sorted(topology_payload.keys()),
            "time_series_keys": sorted(time_series_payload.keys()),
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


def _invoke_grid2benchmark(config: BenchmarkConfig, source_code: str) -> dict[str, Any]:
    try:
        grid2benchmark = importlib.import_module("grid2benchmark")
    except Exception as exc:
        raise RuntimeError(
            "grid2benchmark package is not installed or not importable"
        ) from exc

    run_benchmark = getattr(grid2benchmark, "run_benchmark", None)
    package_benchmark_config = getattr(grid2benchmark, "BenchmarkConfig", None)
    package_scenario_config = getattr(grid2benchmark, "ScenarioConfig", None)
    package_topology_source = getattr(grid2benchmark, "TopologySource", None)
    package_time_series_source = getattr(grid2benchmark, "TimeSeriesSource", None)

    if not callable(run_benchmark):
        raise RuntimeError("grid2benchmark.run_benchmark is not available")
    if not callable(package_benchmark_config) or not callable(package_scenario_config):
        raise RuntimeError(
            "grid2benchmark.BenchmarkConfig and ScenarioConfig must be available"
        )

    scenarios = []
    for scenario in config.scenarios:
        scenario_kwargs: dict[str, Any] = {
            "env_name": scenario.env_name,
            "time_series_ids": scenario.time_series_ids,
        }

        if scenario.topology is not None:
            if not callable(package_topology_source):
                raise RuntimeError(
                    "grid2benchmark.TopologySource must be available when topology is provided"
                )
            scenario_kwargs["topology"] = package_topology_source(
                format=scenario.topology.format,
                path=Path(scenario.topology.path),
            )

        if scenario.time_series is not None:
            if not callable(package_time_series_source):
                raise RuntimeError(
                    "grid2benchmark.TimeSeriesSource must be available when time_series is provided"
                )
            scenario_kwargs["time_series"] = package_time_series_source(
                format=scenario.time_series.format,
                path=Path(scenario.time_series.path),
            )

        if scenario.backend is not None:
            scenario_kwargs["backend"] = scenario.backend

        scenarios.append(package_scenario_config(**scenario_kwargs))

    benchmark_config_kwargs: dict[str, Any] = {
        "scenarios": tuple(scenarios),
        "max_steps": config.max_steps,
    }
    if config.kpis is not None:
        benchmark_config_kwargs["kpis"] = config.kpis

    benchmark_config = package_benchmark_config(**benchmark_config_kwargs)
    result = run_benchmark(source_code, benchmark_config)
    return _normalize_benchmark_result(result, config)


def _metric_value_from_any(value: Any) -> benchmarking_pb2.MetricValue:
    metric = benchmarking_pb2.MetricValue()

    if isinstance(value, bool):
        metric.text = str(value)
        return metric

    if isinstance(value, (int, float)):
        metric.scalar = float(value)
        return metric

    if isinstance(value, list) and all(
        isinstance(item, (int, float)) for item in value
    ):
        metric.series.values.extend(float(item) for item in value)
        return metric

    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(nested, (dict, list)):
                metric.attributes.values[str(key)] = json.dumps(nested)
            else:
                metric.attributes.values[str(key)] = str(nested)
        return metric

    metric.text = str(value)
    return metric


def _to_benchmark_result_proto(
    result: dict[str, Any],
) -> benchmarking_pb2.BenchmarkResult:
    structured = benchmarking_pb2.BenchmarkRunResult()
    episodes = result.get("episodes", [])
    metadata = result.get("metadata", {})
    scenario_metadata = (
        metadata.get("scenarios", []) if isinstance(metadata, dict) else []
    )

    episodes_by_scenario: dict[int, list[dict[str, Any]]] = {}
    for episode in episodes:
        scenario_index = int(episode.get("scenario_index", 0))
        episodes_by_scenario.setdefault(scenario_index, []).append(episode)

    for index, scenario in enumerate(scenario_metadata):
        if not isinstance(scenario, dict):
            continue
        scenario_result = structured.scenarios.add(
            scenario_index=int(scenario.get("scenario_index", index)),
            environment=str(
                scenario.get("environment", {}).get("env_name", DEFAULT_ENV_NAME)
                if isinstance(scenario.get("environment"), dict)
                else DEFAULT_ENV_NAME
            ),
        )

        executed_ids = scenario.get("executed_time_series_ids", [])
        if isinstance(executed_ids, list):
            scenario_result.executed_time_series_ids.extend(
                int(v) for v in executed_ids
            )

        for episode in episodes_by_scenario.get(scenario_result.scenario_index, []):
            scenario_result.episodes.add(
                episode_index=int(episode.get("episode_index", 0)),
                scenario_index=int(episode.get("scenario_index", 0)),
                steps=int(episode.get("steps", 0)),
                overload_violations=int(episode.get("overload_violations", 0)),
                runtime_seconds=float(episode.get("runtime_seconds", 0.0)),
                terminated=bool(episode.get("terminated", True)),
            )

        scenario_kpis = scenario.get("kpis", {})
        if isinstance(scenario_kpis, dict):
            for key, value in scenario_kpis.items():
                scenario_result.metrics[str(key)].CopyFrom(
                    _metric_value_from_any(value)
                )

        for key, value in scenario.items():
            if key in {
                "scenario_index",
                "environment",
                "executed_time_series_ids",
                "kpis",
            }:
                continue
            scenario_result.metadata[str(key)] = (
                json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            )

    total_runtime = sum(float(e.get("runtime_seconds", 0.0)) for e in episodes)
    total_violations = sum(int(e.get("overload_violations", 0)) for e in episodes)
    total_steps = sum(int(e.get("steps", 0)) for e in episodes)

    structured.summary.scenario_count = len(structured.scenarios)
    structured.summary.episode_count = len(episodes)
    structured.summary.total_overload_violations = total_violations
    structured.summary.total_runtime_seconds = total_runtime
    structured.summary.average_episode_length = (
        total_steps / len(episodes) if episodes else 0.0
    )

    aggregate_metrics = result.get("kpis", {})
    if isinstance(aggregate_metrics, dict):
        for key, value in aggregate_metrics.items():
            structured.summary.aggregates[str(key)].CopyFrom(
                _metric_value_from_any(value)
            )

    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if key == "scenarios":
                continue
            structured.metadata[str(key)] = (
                json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            )

    benchmark_result = benchmarking_pb2.BenchmarkResult(structured=structured)
    benchmark_result.metadata["format"] = "benchmark.run_result.v2"
    benchmark_result.metadata["metric_schema"] = "map<string,MetricValue>"
    return benchmark_result


def execute_RunBenchmark(request: ExecuteRequest) -> ExecuteResponse:
    """Run the benchmark with user template algorithm against grid2benchmark."""
    global _cached_result_response

    if not request.inputs:
        return ExecuteResponse(status="failed", error="No benchmark input provided")

    try:
        payload, grid_data_response = _split_inputs(request.inputs)
        payload = payload or {}

        if grid_data_response is not None and not grid_data_response.success:
            return ExecuteResponse(
                status="failed",
                error=f"Upstream synthesized grid unavailable: {grid_data_response.message}",
            )

        config = _parse_benchmark_config(payload)
        source_code = _get_algorithm_source(payload)

        module = _load_algorithm_module(source_code)
        _validate_algorithm_module(module)

        benchmark_result = _invoke_grid2benchmark(config, source_code)
        if grid_data_response is not None:
            benchmark_result.setdefault("metadata", {})
            benchmark_result["metadata"][
                "upstream_grid_id"
            ] = grid_data_response.grid_data.grid_id
            benchmark_result["metadata"]["upstream_grid_nodes"] = len(
                grid_data_response.grid_data.topology.nodes
            )
            benchmark_result["metadata"]["upstream_grid_edges"] = len(
                grid_data_response.grid_data.topology.edges
            )

        result_json = json.dumps(benchmark_result, indent=2)
        get_task_manager().store_data(request.task_id, result_json, "json")

        with _cache_lock:
            _cached_result_response = benchmarking_pb2.GetBenchmarkResultResponse(
                success=True,
                message="Benchmark result available",
                result=_to_benchmark_result_proto(benchmark_result),
            )

        grpc_host = os.environ.get("GRPC_HOST", "benchmark-runner")
        grpc_port = os.environ.get("GRPC_PORT", "50051")

        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="grpc",
                uri=f"{grpc_host}:{grpc_port}",
                format="GetBenchmarkResult",
            ),
        )
    except Exception as exc:
        logger.exception("RunBenchmark failed")
        return ExecuteResponse(status="failed", error=str(exc))


benchmark_handlers = {
    "RunBenchmark": execute_RunBenchmark,
}
