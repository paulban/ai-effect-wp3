"""CLI entrypoint for local benchmark runs."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from common.benchmark_operations import execute_RunBenchmark
from common.control_interface import ExecuteRequest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Delft Grid2Benchmark benchmark locally"
    )
    parser.add_argument("--algorithm", required=True, help="Path to algorithm .py file")
    parser.add_argument(
        "--env", default="l2rpn_case14_sandbox", help="Benchmark environment name"
    )
    parser.add_argument("--episodes", type=int, default=1, help="Number of episodes")
    parser.add_argument(
        "--time-series-ids",
        default=None,
        help="Comma-separated time series ids for single-scenario mode",
    )
    parser.add_argument(
        "--topology-path",
        default=None,
        help="Optional topology file path for single-scenario mode",
    )
    parser.add_argument(
        "--topology-format",
        default="pandapower",
        help="Topology source format for single-scenario mode",
    )
    parser.add_argument(
        "--time-series-path",
        default=None,
        help="Optional time series source path for single-scenario mode",
    )
    parser.add_argument(
        "--time-series-format",
        default="grid2op_chronics_dir",
        help="Time series source format for single-scenario mode",
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="Optional backend override for the scenario",
    )
    parser.add_argument(
        "--kpis",
        default=None,
        help="Comma-separated KPI names to request from grid2benchmark",
    )
    parser.add_argument(
        "--scenarios-json",
        default=None,
        help="JSON array of scenario objects; overrides --env/--episodes/--time-series-ids",
    )
    parser.add_argument(
        "--max-steps", type=int, default=200, help="Max steps per episode"
    )
    args = parser.parse_args()

    algorithm_source = Path(args.algorithm).read_text(encoding="utf-8")

    benchmark_payload = {
        "max_steps": args.max_steps,
    }

    if args.kpis:
        benchmark_payload["kpis"] = [
            part.strip() for part in args.kpis.split(",") if part.strip()
        ]

    if args.scenarios_json:
        benchmark_payload["scenarios"] = json.loads(args.scenarios_json)
    else:
        scenario: dict[str, object] = {
            "env_name": args.env,
        }

        if args.time_series_ids:
            scenario["time_series_ids"] = [
                int(part.strip())
                for part in args.time_series_ids.split(",")
                if part.strip()
            ]
        else:
            scenario["time_series_ids"] = list(range(args.episodes))

        if args.topology_path:
            scenario["topology"] = {
                "format": args.topology_format,
                "path": args.topology_path,
            }

        if args.time_series_path:
            scenario["time_series"] = {
                "format": args.time_series_format,
                "path": args.time_series_path,
            }

        if args.backend:
            scenario["backend"] = args.backend

        benchmark_payload["scenarios"] = [scenario]

    payload = {
        "benchmark": benchmark_payload,
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
