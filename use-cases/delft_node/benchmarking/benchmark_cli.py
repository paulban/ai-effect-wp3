"""CLI entrypoint for local benchmark runs."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from common.benchmark_operations import execute_RunBenchmark
from common.control_interface import ExecuteRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Delft Grid2Op benchmark locally")
    parser.add_argument("--algorithm", required=True, help="Path to algorithm .py file")
    parser.add_argument(
        "--env", default="l2rpn_case14_sandbox", help="Grid2Op env name"
    )
    parser.add_argument("--episodes", type=int, default=1, help="Number of episodes")
    parser.add_argument(
        "--max-steps", type=int, default=200, help="Max steps per episode"
    )
    parser.add_argument(
        "--topology-json",
        default="{}",
        help="JSON string with pandapower-style topology metadata",
    )
    parser.add_argument(
        "--timeseries-json",
        default="{}",
        help="JSON string with time series metadata",
    )
    args = parser.parse_args()

    algorithm_source = Path(args.algorithm).read_text(encoding="utf-8")

    payload = {
        "benchmark": {
            "env_name": args.env,
            "episodes": args.episodes,
            "max_steps": args.max_steps,
        },
        "grid_topology": json.loads(args.topology_json),
        "time_series": json.loads(args.timeseries_json),
        "algorithm": {
            "source": algorithm_source,
        },
    }

    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    request = ExecuteRequest(
        method="RunBenchmark",
        workflow_id="local-benchmark",
        task_id="local-task-1",
        inputs=[{"protocol": "inline", "uri": encoded, "format": "json"}],
    )

    result = execute_RunBenchmark(request)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
