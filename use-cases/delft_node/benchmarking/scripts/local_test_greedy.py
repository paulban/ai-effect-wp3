"""Local smoke test for benchmark service using a Grid2Op baseline algorithm.

Usage:
    python scripts/local_test_greedy.py

Optional:
    python scripts/local_test_greedy.py --base-url http://localhost:8005
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx


def _build_payload(algorithm_path: Path) -> dict:
    source = algorithm_path.read_text(encoding="utf-8")
    source_b64 = base64.b64encode(source.encode("utf-8")).decode("utf-8")

    return {
        "benchmark": {
            "env_name": "l2rpn_case14_sandbox",
            "episodes": 1,
            "max_steps": 50,
        },
        "grid_topology": {
            "format": "pandapower",
            "case": "case14",
        },
        "time_series": {
            "profile": "default",
        },
        "algorithm": {
            "source_b64": source_b64,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run local benchmark smoke test with Grid2Op baseline algorithm"
    )
    parser.add_argument("--base-url", default="http://localhost:8005")
    parser.add_argument(
        "--algorithm",
        default="algorithms/greedy_baseline.py",
        help="Relative path from benchmark root",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    algorithm_path = root / args.algorithm

    payload = _build_payload(algorithm_path)
    payload_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")

    execute_body = {
        "method": "RunBenchmark",
        "workflow_id": "local-greedy-workflow",
        "task_id": "local-greedy-task-001",
        "inputs": [
            {
                "protocol": "inline",
                "uri": payload_b64,
                "format": "json",
            }
        ],
    }

    try:
        with httpx.Client(base_url=args.base_url, timeout=300.0) as client:
            health = client.get("/health")
            health.raise_for_status()
            print("Health:", health.json())

            execute = client.post("/control/execute", json=execute_body)
            execute.raise_for_status()
            execute_data = execute.json()
            print("Execute:", json.dumps(execute_data, indent=2))

            if execute_data.get("status") != "complete":
                raise RuntimeError(f"Benchmark failed: {execute_data.get('error')}")

            output_ref = execute_data.get("output")
            if not output_ref:
                raise RuntimeError("Benchmark returned no output reference")

            output_uri = output_ref["uri"]
            try:
                output = client.get(output_uri)
                output.raise_for_status()
                result = output.json()
            except httpx.ConnectError:
                # When running from host against Docker, services may return
                # container-internal URLs (e.g., http://benchmark-runner:8080/...)
                parsed = urlparse(output_uri)
                if not parsed.path:
                    raise

                fallback_uri = parsed.path
                if parsed.query:
                    fallback_uri = f"{fallback_uri}?{parsed.query}"

                print(
                    "Output URI is not directly reachable from host; "
                    f"retrying via base URL path: {fallback_uri}"
                )
                output = client.get(fallback_uri)
                output.raise_for_status()
                result = output.json()
    except httpx.ConnectError as exc:
        raise SystemExit(
            f"Cannot connect to benchmark service at {args.base_url}. "
            "Start it with: PORT=8005 python main.py "
            "(or pass --base-url to a running endpoint)."
        ) from exc

    print("\nKPI summary:")
    print(json.dumps(result.get("kpis", {}), indent=2))
    print("\nEpisodes:")
    print(json.dumps(result.get("episodes", []), indent=2))


if __name__ == "__main__":
    main()
