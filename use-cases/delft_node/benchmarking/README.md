# Delft Node Benchmarking Use Case (Grid2Op)

This use case adds a benchmarking service for network topology optimization algorithms.

## MVP scope
- Single benchmark service compatible with AI-Effect orchestrator control endpoints.
- Input payload includes:
  - test case metadata (pandapower-formatted topology and time series metadata)
  - algorithm code template payload
- Algorithm submission model: Python file implementing build_agent(env, context).
- Benchmark engine: grid2op.
- KPI evaluation: grid2evaluate (with fallback adapter if runtime API differs).
- Packaging:
  - Docker use-case deployment
  - Python package via pyproject.toml

## Key files
- main.py: service entrypoint
- common/benchmark_operations.py: RunBenchmark operation implementation
- algorithms/algorithm_template.py: user algorithm template
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

## Python package flow
- Build package artifacts from this folder:
  - python -m pip install --upgrade build
  - python -m build
- Run local CLI:
  - python benchmark_cli.py --algorithm algorithms/algorithm_template.py

## Input payload shape (inline JSON)
{
  "benchmark": {"env_name": "l2rpn_case14_sandbox", "episodes": 1, "max_steps": 100},
  "grid_topology": {"format": "pandapower", "case": "case14"},
  "time_series": {"profile": "default"},
  "algorithm": {"source_b64": "<base64 python source>"}
}

## Next extension
- CGMES input converter layer.
- Strong isolation mode (subprocess or per-run container) for untrusted code.
