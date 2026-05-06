# Delft Node Benchmarking Use Case (Grid2Benchmark)

This use case runs a canonical protobuf/gRPC benchmark pipeline for network topology optimization algorithms.

## MVP scope
- Benchmark service compatible with AI-Effect control endpoints plus canonical gRPC data-plane artifacts.
- Input payload includes:
  - test case metadata (pandapower-formatted topology and time series metadata)
  - algorithm code template payload
- Algorithm submission model: Python file implementing build_agent(env, context).
- Benchmark engine: grid2benchmark (public repository dependency).
- KPI evaluation: delegated to grid2benchmark; results are serialized as canonical `BenchmarkRunResult` (structured scenarios + summary aggregates).
- Packaging:
  - Docker use-case deployment
  - Python package via pyproject.toml

## Key files
- main.py: service entrypoint
- common/benchmark_operations.py: RunBenchmark operation implementation
- algorithms/algorithm_template.py: user algorithm template
- algorithms/greedy_baseline.py: baseline algorithm (generic no-op action policy)
- blueprint.json and dockerinfo.json: orchestrator workflow metadata
- run_workflow.sh: end-to-end orchestration run script

## Run locally as service
1. cd use-cases/delft_node/benchmarking
2. docker network create ai-effect-services || true
3. docker compose -f docker-compose-all.yml up -d --build
4. curl http://localhost:8004/health

## Run benchmark via orchestrator
1. Start orchestrator stack from orchestrator/docker-compose.yml
2. Start this benchmark service
3. Run ./run_workflow.sh

## Local example smoke test with baseline algorithm
1. Start the service locally (or in Docker)
2. From this folder, run:
  - python scripts/local_test_greedy.py --base-url http://localhost:8004
3. The script submits an inline benchmark payload with algorithms/greedy_baseline.py,
  then fetches KPI output from /control/data/{task_id}.

## Python package flow
- Build package artifacts from this folder:
  - python -m pip install --upgrade build
  - python -m build
- Run local CLI:
  - python benchmark_cli.py --algorithm algorithms/algorithm_template.py
  - python benchmark_cli.py --algorithm algorithms/greedy_baseline.py

## Canonical Control Payload (inline JSON)
{
  "benchmark": {
    "max_steps": 100,
    "kpis": ["survival", "latency"],
    "scenarios": [
      {
        "env_name": "l2rpn_case14_sandbox",
        "time_series_ids": [0]
      }
    ]
  },
  "algorithm": {"source_b64": "<base64 python source>"}
}

## Scenario-aware benchmark payload
{
  "benchmark": {
    "max_steps": 200,
    "kpis": ["survival", "violations", "latency"],
    "scenarios": [
      {
        "env_name": "l2rpn_case14_sandbox",
        "topology": {
          "format": "pandapower",
          "path": "./data/grid.json"
        },
        "time_series": {
          "format": "grid2op_chronics_dir",
          "path": "./data/chronics"
        },
        "time_series_ids": [0, 1, 2]
      },
      {
        "env_name": "l2rpn_case14_sandbox",
        "backend": "lightsim2grid",
        "time_series_ids": [7]
      }
    ]
  },
  "algorithm": {"source_b64": "<base64 python source>"}
}

## Output shape notes
- The benchmark gRPC response is `GetBenchmarkResultResponse` with `result.structured` (`BenchmarkRunResult`).
- Scenario-level metrics are in `result.structured.scenarios[*].metrics`.
- Aggregate metrics are in `result.structured.summary.aggregates`.
- Scenario field names follow `grid2benchmark`: `env_name`, `time_series_ids`, `topology`, `time_series`, `backend`.

## Notes on dependencies
- `requirements.txt` installs `grid2benchmark` from the public GitHub repository at branch `main`.
- `requirements.txt` also installs `grid2evaluate` and a specific version of `grid2op` from GitHub because it is not currently published on PyPI.
- `pyproject.toml` references `grid2benchmark` directly for package metadata.

## Next extension
- give test grid and time series data as files for benchmarking
- CGMES input converter layer.
- Strong isolation mode (subprocess or per-run container) for untrusted code.
