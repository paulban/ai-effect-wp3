"""Grid2Op benchmark operations for AI-Effect orchestration.

MVP operation:
- RunBenchmark: execute a user-supplied Python algorithm template against
  a fixed Grid2Op environment and compute KPI outputs with grid2evaluate hooks.
"""

from __future__ import annotations

import base64
import importlib.util
import inspect
import json
import logging
import tempfile
import time
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
class BenchmarkConfig:
    env_name: str
    max_steps: int
    episodes: int


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
    env_name = benchmark_cfg.get("env_name", DEFAULT_ENV_NAME)
    max_steps = int(benchmark_cfg.get("max_steps", DEFAULT_MAX_STEPS))
    episodes = int(benchmark_cfg.get("episodes", DEFAULT_EPISODES))

    if max_steps <= 0:
        raise ValueError("benchmark.max_steps must be > 0")
    if episodes <= 0:
        raise ValueError("benchmark.episodes must be > 0")

    return BenchmarkConfig(env_name=env_name, max_steps=max_steps, episodes=episodes)


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


def _evaluate_with_grid2evaluate(
    record_directory: Path,
    episode_results: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Compute KPIs via grid2evaluate from recorded EnvRecorder outputs.

    grid2evaluate expects a directory containing Grid2Op recorder parquet files.
    We preserve existing manual KPI outputs and enrich them with grid2evaluate
    metrics when possible.
    """
    kpis = _compute_manual_kpis(episode_results)

    try:
        from grid2evaluate.carbon_intensity_kpi import CarbonIntensityKpi  # type: ignore
        from grid2evaluate.operation_score_kpi import OperationScoreKpi  # type: ignore
        from grid2evaluate.topological_action_complexity_kpi import (  # type: ignore
            TopologicalActionComplexityKpi,
        )

        errors: dict[str, str] = {}
        g2e_metrics: dict[str, Any] = {}

        try:
            g2e_metrics["carbon_intensity"] = CarbonIntensityKpi().evaluate(
                record_directory
            )
        except Exception as exc:
            errors["carbon_intensity"] = str(exc)

        try:
            g2e_metrics["operation_score"] = OperationScoreKpi().evaluate(
                record_directory
            )
        except Exception as exc:
            errors["operation_score"] = str(exc)

        try:
            g2e_metrics["topological_action_complexity"] = (
                TopologicalActionComplexityKpi().evaluate(record_directory)
            )
        except Exception as exc:
            errors["topological_action_complexity"] = str(exc)

        if g2e_metrics:
            kpis["grid2evaluate"] = g2e_metrics
            kpis["evaluation_backend"] = (
                "grid2evaluate" if not errors else "grid2evaluate_partial"
            )
            if errors:
                kpis["grid2evaluate_errors"] = errors
            return kpis

        logger.warning(
            "grid2evaluate KPIs unavailable for env=%s record_dir=%s context=%s",
            context.get("env_name"),
            record_directory,
            context,
        )
    except Exception as exc:
        logger.warning("grid2evaluate evaluation path failed: %s", exc)

    kpis["evaluation_backend"] = "fallback_manual"
    return kpis


def _run_grid2op_episodes(
    config: BenchmarkConfig,
    algorithm_module: ModuleType,
    payload: dict[str, Any],
) -> dict[str, Any]:
    import grid2op  # type: ignore
    from grid2op.Environment.EnvRecorder import EnvRecorder  # type: ignore

    env = grid2op.make(config.env_name, test=True)
    build_agent = getattr(algorithm_module, REQUIRED_ALGORITHM_FUNCTION)

    agent_context = {
        "grid_topology": payload.get("grid_topology", {}),
        "time_series": payload.get("time_series", {}),
        "benchmark": {
            "env_name": config.env_name,
            "max_steps": config.max_steps,
            "episodes": config.episodes,
        },
    }

    with tempfile.TemporaryDirectory(prefix="benchmark_record_") as record_dir:
        record_path = Path(record_dir)

        with EnvRecorder(env, record_path) as env_rec:
            agent = build_agent(env_rec, agent_context)
            if not hasattr(agent, "act") or not callable(agent.act):
                raise ValueError(
                    "Algorithm agent must expose callable method act(observation)"
                )

            episode_results: list[dict[str, Any]] = []

            def _call_agent_act(observation: Any, reward: float, done: bool) -> Any:
                """Call agent.act across different agent signatures.

                Some agents use act(obs), others use act(obs, reward) or
                act(obs, reward, done).
                """
                act_fn = agent.act
                try:
                    param_count = len(inspect.signature(act_fn).parameters)
                except (TypeError, ValueError):
                    param_count = 1

                if param_count <= 1:
                    return act_fn(observation)
                if param_count == 2:
                    return act_fn(observation, reward)
                return act_fn(observation, reward, done)

            for episode_idx in range(config.episodes):
                reset_result = env_rec.reset()
                if isinstance(reset_result, tuple):
                    obs = reset_result[0]
                else:
                    obs = reset_result

                done = False
                reward = 0.0
                steps = 0
                overload_violations = 0
                started = time.perf_counter()

                while not done and steps < config.max_steps:
                    action = _call_agent_act(obs, reward, done)

                    step_result = env_rec.step(action)
                    if isinstance(step_result, tuple) and len(step_result) == 5:
                        obs, reward, terminated, truncated, info = step_result
                        done = bool(terminated or truncated)
                    else:
                        obs, reward, done, info = step_result

                    steps += 1

                    # Grid2Op info contains backend-specific overload indicators.
                    if isinstance(info, dict):
                        if info.get("is_illegal", False):
                            overload_violations += 1
                        elif info.get("is_ambiguous", False):
                            overload_violations += 1

                runtime_seconds = time.perf_counter() - started
                episode_results.append(
                    {
                        "episode_index": episode_idx,
                        "steps": steps,
                        "overload_violations": overload_violations,
                        "runtime_seconds": runtime_seconds,
                        "terminated": done,
                    }
                )

        context = {
            "env_name": config.env_name,
            "episodes": config.episodes,
            "max_steps": config.max_steps,
        }
        kpis = _evaluate_with_grid2evaluate(record_path, episode_results, context)

    env.close()

    return {
        "environment": {
            "env_name": config.env_name,
            "fixed_environment": True,
        },
        "input_summary": {
            "topology_keys": sorted(payload.get("grid_topology", {}).keys()),
            "time_series_keys": sorted(payload.get("time_series", {}).keys()),
        },
        "episodes": episode_results,
        "kpis": kpis,
    }


def execute_RunBenchmark(request: ExecuteRequest) -> ExecuteResponse:
    """Run the benchmark with user template algorithm against Grid2Op."""
    if not request.inputs:
        return ExecuteResponse(status="failed", error="No benchmark input provided")

    try:
        payload = _decode_inline_json(request.inputs[0])
        config = _parse_benchmark_config(payload)
        source_code = _get_algorithm_source(payload)

        module = _load_algorithm_module(source_code)
        _validate_algorithm_module(module)

        benchmark_result = _run_grid2op_episodes(config, module, payload)

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
