"""Unit tests for the benchmark operation's request handling.

grid2benchmark's own config classes are used for real, so these tests also
check that its validation reaches the caller. Only ``run_benchmark`` is
replaced where a job is submitted, because a grid2op run is not what is tested.
"""

import base64
import json
import tempfile
import time
from pathlib import Path

import grid2benchmark
import pytest

import service
from service import (
    _build_benchmark_config,
    _decode_inline_json,
    _read_payload,
    _require_plain_name,
    _resolve_algorithm,
    _resolve_scenario_env,
    execute_RunBenchmark,
)
from common.artifacts import FileArtifactStore
from common.batch_jobs import BatchJobRunner
from common.concurrent import ExecuteRequest, task_manager

VALID_ALGORITHM = "def build_agent(env, context):\n    return None\n"


def _inline(payload: dict) -> dict:
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    return {"protocol": "inline", "uri": encoded, "format": "json"}


@pytest.fixture
def algorithm_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An algorithm directory holding one valid and one invalid algorithm."""
    (tmp_path / "valid_agent.py").write_text(VALID_ALGORITHM, encoding="utf-8")
    (tmp_path / "no_build_agent.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(service, "ALGORITHM_DIRECTORY", tmp_path)
    return tmp_path


@pytest.fixture
def scenario_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scenario directory holding one dumped scenario."""
    scenario_root = tmp_path / "scenarios"
    (scenario_root / "my_scenario").mkdir(parents=True)
    monkeypatch.setattr(service, "SCENARIO_DIRECTORY", scenario_root)
    return scenario_root


def test_decode_inline_json_roundtrip():
    payload = {"benchmark": {"max_steps": 2}}
    assert _decode_inline_json(_inline(payload)) == payload


def test_read_payload_without_inputs_means_defaults():
    assert _read_payload([]) == {}


def test_read_payload_rejects_non_inline_input():
    with pytest.raises(ValueError, match="Unsupported input protocol"):
        _read_payload([{"protocol": "http", "uri": "http://example.org/x", "format": "json"}])


@pytest.mark.parametrize("name", ["", ".", "..", "../secret.py", "a/b.py", "a\\b.py", 3])
def test_names_that_could_escape_the_directory_are_rejected(name):
    with pytest.raises(ValueError):
        _require_plain_name(name, "algorithm")


def test_shipped_default_algorithm_resolves():
    assert _resolve_algorithm(service.DEFAULT_ALGORITHM).is_file()


def test_unknown_algorithm_lists_what_is_available(algorithm_directory):
    with pytest.raises(ValueError, match="valid_agent.py"):
        _resolve_algorithm("missing.py")


def test_algorithm_must_be_a_python_file(algorithm_directory):
    with pytest.raises(ValueError, match=r"\.py"):
        _resolve_algorithm("valid_agent.txt")


def test_dumped_scenario_resolves_to_its_directory(scenario_directory):
    assert _resolve_scenario_env("my_scenario") == str(scenario_directory / "my_scenario")


def test_unknown_scenario_is_passed_through_as_builtin(scenario_directory):
    assert _resolve_scenario_env("l2rpn_case14_sandbox") == "l2rpn_case14_sandbox"


def test_empty_request_uses_grid2benchmark_defaults():
    config = _build_benchmark_config({})
    defaults = grid2benchmark.BenchmarkConfig()
    assert config.max_steps == defaults.max_steps
    assert config.kpis == defaults.kpis
    assert config.scenarios[0].env_name == defaults.scenarios[0].env_name


def test_explicit_scenarios_kpis_and_steps_are_passed_on(scenario_directory):
    config = _build_benchmark_config(
        {
            "benchmark": {
                "max_steps": 25,
                "kpis": ["operation_score"],
                "scenarios": [
                    {"env_name": "my_scenario", "time_series_ids": [2, 4], "backend": "lightsim2grid"},
                    {"env_name": "l2rpn_case14_sandbox"},
                ],
            }
        }
    )
    assert config.max_steps == 25
    assert config.kpis == ("operation_score",)
    assert config.scenarios[0].env_name == str(scenario_directory / "my_scenario")
    assert config.scenarios[0].time_series_ids == (2, 4)
    assert config.scenarios[0].backend == "lightsim2grid"
    assert config.scenarios[1].env_name == "l2rpn_case14_sandbox"


def test_single_scenario_fields_may_sit_in_the_benchmark_block():
    config = _build_benchmark_config(
        {"benchmark": {"env_name": "l2rpn_case14_sandbox", "time_series_ids": [1]}}
    )
    assert len(config.scenarios) == 1
    assert config.scenarios[0].time_series_ids == (1,)


@pytest.mark.parametrize(
    "benchmark_block",
    [
        {"kpis": ["survival"]},
        {"backend": "not-a-backend"},
        {"max_steps": 0},
        {"scenarios": []},
    ],
)
def test_values_grid2benchmark_rejects_reach_the_caller(benchmark_block):
    with pytest.raises(ValueError):
        _build_benchmark_config({"benchmark": benchmark_block})


@pytest.fixture
def job_runner() -> BatchJobRunner:
    """A job runner writing to a throwaway artifact store."""
    runner = BatchJobRunner(
        FileArtifactStore(tempfile.mkdtemp(prefix="benchmark-artifacts-")),
        self_url="http://benchmark-runner:8080",
        max_concurrent_jobs=1,
    )
    service.set_job_runner(runner)
    yield runner
    service.set_job_runner(None)


def _request(payload: dict, task_id: str) -> ExecuteRequest:
    return ExecuteRequest(
        method="RunBenchmark", workflow_id="workflow-1", task_id=task_id, inputs=[_inline(payload)]
    )


def test_unknown_algorithm_is_rejected_synchronously(algorithm_directory, job_runner):
    response = execute_RunBenchmark(_request({"algorithm": "missing.py"}, "task-unknown"))
    assert response.status == "failed"
    assert "Unknown algorithm" in response.error


def test_algorithm_without_build_agent_is_rejected_synchronously(algorithm_directory, job_runner):
    response = execute_RunBenchmark(_request({"algorithm": "no_build_agent.py"}, "task-invalid"))
    assert response.status == "failed"
    assert "build_agent" in response.error


def test_valid_request_stores_the_grid2benchmark_result_with_metadata(
    algorithm_directory, job_runner, monkeypatch: pytest.MonkeyPatch
):
    received = {}

    def fake_run_benchmark(algorithm, config):
        received["algorithm"] = algorithm
        received["config"] = config
        return {"scenarios": [], "summary": {"scenario_count": 0}}

    monkeypatch.setattr(grid2benchmark, "run_benchmark", fake_run_benchmark)

    response = execute_RunBenchmark(
        _request({"algorithm": "valid_agent.py", "benchmark": {"max_steps": 7}}, "task-ok")
    )
    assert response.status == "running"

    deadline = time.time() + 5
    while task_manager.get_status(response.task_id)["status"] == "running":
        assert time.time() < deadline, "job did not finish"
        time.sleep(0.01)
    assert task_manager.get_status(response.task_id)["status"] == "complete"

    stored = json.loads(job_runner.artifact_store.load(response.task_id).content)
    assert stored["summary"] == {"scenario_count": 0}
    assert stored["metadata"]["algorithm"] == "valid_agent.py"
    assert received["algorithm"] == algorithm_directory / "valid_agent.py"
    assert received["config"].max_steps == 7


def test_library_calling_sys_exit_fails_the_job_instead_of_hanging(
    algorithm_directory, job_runner, monkeypatch: pytest.MonkeyPatch
):
    def exiting_run_benchmark(algorithm, config):
        raise SystemExit(1)

    monkeypatch.setattr(grid2benchmark, "run_benchmark", exiting_run_benchmark)

    response = execute_RunBenchmark(_request({"algorithm": "valid_agent.py"}, "task-exit"))
    assert response.status == "running"

    deadline = time.time() + 5
    while task_manager.get_status(response.task_id)["status"] == "running":
        assert time.time() < deadline, "job stayed running after sys.exit"
        time.sleep(0.01)
    status = task_manager.get_status(response.task_id)
    assert status["status"] == "failed"
    assert "sys.exit(1)" in status["error"]


def test_network_utilization_is_accepted_and_carries_its_warning(
    algorithm_directory, job_runner, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        grid2benchmark, "run_benchmark",
        lambda algorithm, config: {"scenarios": [], "summary": {}},
    )

    response = execute_RunBenchmark(_request(
        {"algorithm": "valid_agent.py", "benchmark": {"kpis": ["network_utilization"]}},
        "task-n1",
    ))
    assert response.status == "running"

    deadline = time.time() + 5
    while task_manager.get_status(response.task_id)["status"] == "running":
        assert time.time() < deadline, "job did not finish"
        time.sleep(0.01)
    assert task_manager.get_status(response.task_id)["status"] == "complete"

    stored = json.loads(job_runner.artifact_store.load(response.task_id).content)
    assert stored["metadata"]["warnings"] == [service.KPI_WARNINGS["network_utilization"]]


def test_default_kpis_carry_no_warnings():
    assert service._kpi_warnings(service._build_benchmark_config({})) == []


def test_failing_network_utilization_run_names_the_known_issue(
    algorithm_directory, job_runner, monkeypatch: pytest.MonkeyPatch
):
    def failing_run_benchmark(algorithm, config):
        raise RuntimeError("grid2evaluate KPI evaluation failed for ['network_utilization']")

    monkeypatch.setattr(grid2benchmark, "run_benchmark", failing_run_benchmark)

    response = execute_RunBenchmark(_request(
        {"algorithm": "valid_agent.py", "benchmark": {"kpis": ["network_utilization"]}},
        "task-n1-fail",
    ))

    deadline = time.time() + 5
    while task_manager.get_status(response.task_id)["status"] == "running":
        assert time.time() < deadline, "job did not finish"
        time.sleep(0.01)
    status = task_manager.get_status(response.task_id)
    assert status["status"] == "failed"
    assert "KPI evaluation failed" in status["error"]
    assert "Known issue: network_utilization (N-1)" in status["error"]
