import base64
import json
from types import ModuleType, SimpleNamespace

import pytest

import common.benchmark_operations as benchmark_operations
from common.benchmark_operations import (
    BenchmarkConfig,
    ScenarioConfig,
    TimeSeriesSourceConfig,
    TopologySourceConfig,
    _compute_manual_kpis,
    _decode_inline_json,
    _invoke_grid2benchmark,
    _normalize_benchmark_result,
    _parse_benchmark_config,
    _validate_algorithm_module,
)


def _inline(payload: dict) -> dict:
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    return {"protocol": "inline", "uri": encoded, "format": "json"}


def test_decode_inline_json_roundtrip():
    payload = {"benchmark": {"episodes": 2}}
    decoded = _decode_inline_json(_inline(payload))
    assert decoded == payload


def test_parse_benchmark_config_defaults():
    cfg = _parse_benchmark_config({})
    assert cfg.max_steps > 0
    assert len(cfg.scenarios) == 1
    assert cfg.scenarios[0].env_name == "l2rpn_case14_sandbox"
    assert cfg.scenarios[0].time_series_ids == (0,)


def test_parse_benchmark_config_supports_explicit_scenarios_and_kpis():
    cfg = _parse_benchmark_config(
        {
            "benchmark": {
                "max_steps": 25,
                "kpis": ["survival", "latency"],
                "scenarios": [
                    {
                        "env_name": "env-a",
                        "time_series_ids": [2, 4],
                        "topology": {
                            "format": "pandapower",
                            "path": "/tmp/env-a.json",
                        },
                        "time_series": {
                            "format": "grid2op_chronics_dir",
                            "path": "/tmp/chronics-a",
                        },
                        "backend": "lightsim2grid",
                    },
                    {
                        "env_name": "env-b",
                    },
                ],
            }
        }
    )

    assert cfg.max_steps == 25
    assert cfg.kpis == ("survival", "latency")
    assert len(cfg.scenarios) == 2
    assert cfg.scenarios[0].time_series_ids == (2, 4)
    assert cfg.scenarios[0].topology == TopologySourceConfig(
        format="pandapower", path="/tmp/env-a.json"
    )
    assert cfg.scenarios[0].time_series == TimeSeriesSourceConfig(
        format="grid2op_chronics_dir", path="/tmp/chronics-a"
    )
    assert cfg.scenarios[0].backend == "lightsim2grid"
    assert cfg.scenarios[1].time_series_ids is None


def test_validate_algorithm_module_requires_build_agent():
    module = SimpleNamespace()
    try:
        _validate_algorithm_module(module)
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "build_agent" in str(exc)


def test_validate_algorithm_module_accepts_callable():
    module = SimpleNamespace(build_agent=lambda env, context: None)
    _validate_algorithm_module(module)


def test_compute_manual_kpis_handles_empty_results():
    kpis = _compute_manual_kpis([])

    assert kpis["survival"]["average_episode_length"] == 0.0
    assert kpis["violations"]["total_overload_violations"] == 0
    assert kpis["latency"]["total_runtime_seconds"] == 0.0


def test_normalize_benchmark_result_adds_fallback_fields():
    config = BenchmarkConfig(
        max_steps=5,
        scenarios=(ScenarioConfig(env_name="demo-env", time_series_ids=(0,)),),
    )
    config = BenchmarkConfig(
        max_steps=5,
        scenarios=(
            ScenarioConfig(
                env_name="demo-env",
                time_series_ids=(0,),
                topology=TopologySourceConfig(format="pandapower", path="./grid.json"),
                time_series=TimeSeriesSourceConfig(
                    format="grid2op_chronics_dir", path="./chronics"
                ),
            ),
        ),
    )

    result = _normalize_benchmark_result(
        {
            "scenarios": [
                {
                    "scenario_index": 0,
                    "environment": {"env_name": "demo-env", "fixed_environment": True},
                    "executed_time_series_ids": [0],
                    "episodes": [
                        {
                            "steps": 3,
                            "violations": 2,
                            "runtime": 1.25,
                        }
                    ],
                }
            ],
            "summary": {"scenario_count": 1, "episode_count": 1, "kpis": {}},
        },
        config,
    )

    assert result["environment"]["env_name"] == "demo-env"
    assert result["episodes"][0]["overload_violations"] == 2
    assert result["episodes"][0]["runtime_seconds"] == 1.25
    assert result["episodes"][0]["scenario_index"] == 0
    assert result["kpis"]["evaluation_backend"] == "grid2benchmark_manual"
    assert result["input_summary"]["topology_keys"] == ["format", "path"]
    assert result["metadata"]["summary"]["scenario_count"] == 1


def test_invoke_grid2benchmark_calls_supported_function(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_grid2benchmark = ModuleType("grid2benchmark")

    class FakeScenarioConfig:
        def __init__(
            self,
            env_name,
            time_series_ids=None,
            topology=None,
            time_series=None,
            backend=None,
        ):
            self.env_name = env_name
            self.time_series_ids = time_series_ids
            self.topology = topology
            self.time_series = time_series
            self.backend = backend

    class FakeTopologySource:
        def __init__(self, format, path):
            self.format = format
            self.path = path

    class FakeTimeSeriesSource:
        def __init__(self, format, path):
            self.format = format
            self.path = path

    class FakeBenchmarkConfig:
        def __init__(
            self, scenarios, max_steps, kpis=("survival", "violations", "latency")
        ):
            self.scenarios = scenarios
            self.max_steps = max_steps
            self.kpis = kpis

    def run_benchmark(algorithm, config):
        assert "build_agent" in algorithm
        assert config.max_steps == 7
        assert len(config.scenarios) == 1
        assert config.scenarios[0].env_name == "demo-env"
        assert config.scenarios[0].time_series_ids == (5, 9)
        assert config.kpis == ("survival", "latency")

        return {
            "scenarios": [
                {
                    "scenario_index": 0,
                    "environment": {"env_name": "demo-env", "fixed_environment": True},
                    "executed_time_series_ids": [0, 1],
                    "episodes": [
                        {
                            "episode_index": 0,
                            "steps": 7,
                            "overload_violations": 1,
                            "runtime_seconds": 0.5,
                            "terminated": True,
                        }
                    ],
                    "kpis": {"evaluation_backend": "manual_only"},
                }
            ],
            "summary": {"scenario_count": 1, "episode_count": 1, "kpis": {}},
        }

    fake_grid2benchmark.BenchmarkConfig = FakeBenchmarkConfig
    fake_grid2benchmark.ScenarioConfig = FakeScenarioConfig
    fake_grid2benchmark.TopologySource = FakeTopologySource
    fake_grid2benchmark.TimeSeriesSource = FakeTimeSeriesSource
    fake_grid2benchmark.run_benchmark = run_benchmark

    def fake_import_module(name: str):
        if name == "grid2benchmark":
            return fake_grid2benchmark
        raise ImportError(name)

    monkeypatch.setattr(
        benchmark_operations.importlib, "import_module", fake_import_module
    )

    result = _invoke_grid2benchmark(
        BenchmarkConfig(
            max_steps=7,
            scenarios=(
                ScenarioConfig(
                    env_name="demo-env",
                    time_series_ids=(5, 9),
                    topology=TopologySourceConfig(
                        format="pandapower", path="./grid.json"
                    ),
                    time_series=TimeSeriesSourceConfig(
                        format="grid2op_chronics_dir", path="./chronics"
                    ),
                    backend="lightsim2grid",
                ),
            ),
            kpis=("survival", "latency"),
        ),
        "def build_agent(env, context): pass",
    )

    assert result["environment"]["env_name"] == "demo-env"
    assert result["episodes"][0]["steps"] == 7
    assert result["kpis"]["evaluation_backend"] == "manual_only"
    assert result["metadata"]["scenario_count"] == 1


def test_invoke_grid2benchmark_requires_installed_package(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_import_module(name: str):
        raise ImportError(name)

    monkeypatch.setattr(
        benchmark_operations.importlib, "import_module", fake_import_module
    )

    with pytest.raises(RuntimeError, match="not installed or not importable"):
        _invoke_grid2benchmark(
            BenchmarkConfig(
                max_steps=7,
                scenarios=(
                    ScenarioConfig(env_name="demo-env", time_series_ids=(0, 1)),
                ),
            ),
            "source",
        )
