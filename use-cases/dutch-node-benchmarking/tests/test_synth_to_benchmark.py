"""Manual cross-service check: DataSynthesizer output → Benchmarking input

NOT an orchestrated pipeline test. The data synthesizer and the benchmark are
independent use cases with no connection between them, and neither export
declares one -- the orchestrator will never wire these two together.

What this still verifies, by driving both HTTP control planes by hand, is that
a DataReference produced by the synth service is directly consumable by the
benchmark service:

  1. ConfigureAndSynthesize  →  tef-synthetic-data  (localhost:8003)
  2. RunBenchmark            →  tef-benchmark-runner (localhost:8004)

The output DataReference produced by the synth service uses the Docker-network
address (synthetic-data:50051) so the benchmark container can reach it
directly over gRPC when we pass it as input.

Run it by hand; it is not part of any automated suite and requires both
containers to be up.

Usage:
    python tests/test_synth_to_benchmark.py [--steps N] [--env ENV]

Prerequisites:
    Both containers must be running:
        docker compose -f ../dutch-node-data-synthesizer/services/data_synthesizer/docker-compose-all.yml up -d
        docker compose -f services/benchmarking/docker-compose-all.yml up -d
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import httpx

SYNTH_URL = "http://localhost:8003"
BENCHMARK_URL = "http://localhost:8004"
HTTP_TIMEOUT = 300.0  # synthesis + benchmark can take a while
DEFAULT_ENV_NAME = "synthetic-grid-v0"


def _wait_for_health(client: httpx.Client, base_url: str, label: str) -> None:
    """Wait until /health responds successfully.

    Containers can briefly accept TCP and then reset/disconnect while app startup
    is still in progress; retry to avoid flaky first-call failures.
    """
    last_error: Exception | None = None
    for attempt in range(1, 16):
        try:
            resp = client.get(f"{base_url}/health", timeout=10.0)
            resp.raise_for_status()
            print(f"      {resp.json()}")
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 15:
                time.sleep(1.5)

    raise RuntimeError(f"{label} health check failed after retries: {last_error}")


# ── helpers ──────────────────────────────────────────────────────────────────

def _inline(payload: dict) -> dict:
    """Wrap a dict as a base64-encoded inline DataReference."""
    return {
        "protocol": "inline",
        "uri": base64.b64encode(json.dumps(payload).encode()).decode(),
        "format": "json",
    }


def _pp(label: str, data: object) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"{'─' * 60}")
    print(json.dumps(data, indent=2, default=str))


def _infer_env_name_from_synth_output(synth_output: dict) -> str:
    """Infer benchmark env from synthesized grid output.

    Priority:
      1. Explicit benchmark_env_name field in synth output JSON.
      2. DEFAULT_ENV_NAME fallback.
    """
    explicit = synth_output.get("benchmark_env_name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return DEFAULT_ENV_NAME


def fetch_synth_output(client: httpx.Client, workflow_id: str) -> dict:
    """Fetch synthesized grid JSON from the synth task-data endpoint."""
    task_id = f"{workflow_id}-synth"
    resp = client.get(f"{SYNTH_URL}/control/data/{task_id}", timeout=30.0)
    resp.raise_for_status()
    synth_data = resp.json()
    _pp("Synthesized grid output JSON", synth_data)
    return synth_data


# ── stage 1: synthesize ───────────────────────────────────────────────────────

def stage_synth(client: httpx.Client, workflow_id: str) -> dict:
    """Call ConfigureAndSynthesize on the data synthesizer service."""
    config_payload = {
        "level_specs": [
            {"n": 10, "avg_k": 2.5, "diam": 4, "dist_type": "dgln", "max_k": 8},
            {"n": 20, "avg_k": 2.0, "diam": 6, "dist_type": "dgln", "max_k": 6},
        ],
        "connection_specs": {"(0, 1)": {"type": "k-stars", "c": 0.174, "gamma": 4.15}},
        "seed": 42,
        "loading_level": "M",
        "ref_sys_id": 1,
    }

    print("\n[1/4] Health check – data synthesizer …")
    _wait_for_health(client, SYNTH_URL, "data synthesizer")

    print("[2/4] ConfigureAndSynthesize …")
    t0 = time.monotonic()
    resp = client.post(
        f"{SYNTH_URL}/control/execute",
        json={
            "method": "ConfigureAndSynthesize",
            "workflow_id": workflow_id,
            "task_id": f"{workflow_id}-synth",
            "inputs": [_inline(config_payload)],
        },
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    elapsed = time.monotonic() - t0

    _pp(f"ConfigureAndSynthesize  ({elapsed:.1f}s)", result)

    if result.get("status") != "complete":
        print(f"\nERROR: synth failed – {result.get('error')}", file=sys.stderr)
        sys.exit(1)

    return result["output"]  # DataReference  {protocol, uri, format}


# ── stage 2: benchmark ────────────────────────────────────────────────────────

def stage_benchmark(
    client: httpx.Client,
    workflow_id: str,
    grid_data_ref: dict,
    max_steps: int,
    env_name: str,
) -> dict:
    """Call RunBenchmark on the benchmarking service."""
    # The synthesized grid is passed via gRPC DataReference (grid_data_ref).
    # The benchmark service resolves that reference, writes scenario_*/grid.json
    # from GridData.pandapower_json, generates time_series CSVs, and builds a
    # fixed Grid2Op environment for benchmarking from those files.
    benchmark_payload = {
        "benchmark": {
            "max_steps": max_steps,
            "env_name": env_name,
            "time_series_ids": [0],
            "kpis": ["carbon_intensity", "operation_score", "topological_action_complexity"],
        }
        # algorithm is omitted → service falls back to algorithm_template.py
    }

    print("\n[3/4] Health check – benchmark runner …")
    _wait_for_health(client, BENCHMARK_URL, "benchmark runner")

    print(f"[4/4] RunBenchmark  (env={env_name}, max_steps={max_steps}) …")
    print(f"      grid data ref: {grid_data_ref}")
    t0 = time.monotonic()
    resp = client.post(
        f"{BENCHMARK_URL}/control/execute",
        json={
            "method": "RunBenchmark",
            "workflow_id": workflow_id,
            "task_id": f"{workflow_id}-benchmark",
            "inputs": [
                grid_data_ref,           # gRPC reference → synth container fetches GridData
                _inline(benchmark_payload),  # config + (optional) algorithm
            ],
        },
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    elapsed = time.monotonic() - t0

    _pp(f"RunBenchmark  ({elapsed:.1f}s)", result)

    if result.get("status") != "complete":
        print(f"\nERROR: benchmark failed – {result.get('error')}", file=sys.stderr)
        sys.exit(1)

    return result["output"]  # DataReference pointing to cached result


# ── fetch result ──────────────────────────────────────────────────────────────

def fetch_result(client: httpx.Client, workflow_id: str, expected_env_name: str) -> None:
    """Pull the stored benchmark result from the task-data endpoint."""
    task_id = f"{workflow_id}-benchmark"
    resp = client.get(f"{BENCHMARK_URL}/control/data/{task_id}", timeout=30.0)
    if resp.status_code == 404:
        print("\n  (result not yet available via /control/data – check gRPC endpoint)")
        return
    resp.raise_for_status()
    data = resp.json()
    _pp("Benchmark result (stored JSON)", data)

    environment = data.get("environment", {})
    actual_env_name = str(environment.get("env_name", ""))
    if actual_env_name != expected_env_name:
        print(
            "\nERROR: Benchmark env mismatch. "
            f"expected={expected_env_name}, actual={actual_env_name}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not bool(environment.get("fixed_environment", False)):
        print(
            "\nERROR: Benchmark did not run with a fixed synthesized-grid environment.",
            file=sys.stderr,
        )
        sys.exit(1)

    topology = environment.get("topology", {})
    time_series = environment.get("time_series", {})
    if str(topology.get("format", "")).lower() != "pandapower":
        print("\nERROR: Benchmark topology format is not pandapower.", file=sys.stderr)
        sys.exit(1)
    if str(time_series.get("format", "")).lower() != "csv":
        print("\nERROR: Benchmark time_series format is not csv.", file=sys.stderr)
        sys.exit(1)

    # Print a summary of the most important fields
    meta = data.get("metadata", {})
    print("\n┌─ Summary ───────────────────────────────────────────────────┐")
    print(f"│  Grid id    : {meta.get('upstream_grid_id', '?')}")
    print(f"│  Nodes      : {meta.get('upstream_grid_nodes', '?')}")
    print(f"│  Edges      : {meta.get('upstream_grid_edges', '?')}")
    kpis = data.get("kpis", {})
    for kpi_name, kpi_val in kpis.items():
        print(f"│  KPI {kpi_name:<30} {kpi_val}")
    episodes = data.get("episodes", [])
    if episodes:
        avg_steps = sum(e.get("steps", 0) for e in episodes) / len(episodes)
        print(f"│  Episodes   : {len(episodes)}  (avg {avg_steps:.1f} steps)")
    print("└─────────────────────────────────────────────────────────────┘")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=50, help="max_steps for benchmark (default 50)")
    parser.add_argument(
        "--env",
        default="",
        help=(
            "Optional override for Grid2Op env name. "
            "If omitted, env is inferred from the synth output."
        ),
    )
    parser.add_argument("--workflow-id", default="local-test-001", help="Workflow ID prefix")
    args = parser.parse_args()

    with httpx.Client() as client:
        grid_ref = stage_synth(client, args.workflow_id)
        synth_output = fetch_synth_output(client, args.workflow_id)

        inferred_env = _infer_env_name_from_synth_output(synth_output)
        selected_env = args.env.strip() or inferred_env
        print(
            f"\n      benchmark env selected: {selected_env} "
            f"(inferred={inferred_env}, override={'yes' if args.env.strip() else 'no'})"
        )

        _benchmark_ref = stage_benchmark(
            client, args.workflow_id, grid_ref, args.steps, selected_env
        )
        fetch_result(client, args.workflow_id, selected_env)

    print("\n✅  Pipeline completed successfully.\n")


if __name__ == "__main__":
    main()
