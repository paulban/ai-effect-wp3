"""VILLASnode chronics generation service.

Loads a config template, substitutes workflow-specific paths, and pushes
the config to VILLASnode via its REST API to convert pandapower timeseries
data into Grid2Op chronics format.

Uses the concurrent handler — returns "running" immediately and processes
in a background thread. The orchestrator polls /control/status/{task_id}
until completion.
"""

import json
import logging
import os
import time

import requests

from common import (
    DataReference,
    AsyncExecuteResponse as ExecuteResponse,
    TaskManager,
    task_manager,
    run_in_background,
    run_concurrent,
)

logger = logging.getLogger(__name__)

VILLAS_NODE_URL = os.environ.get("VILLAS_NODE_URL", "http://villas-node:80")
SHARED_DIR = os.environ.get("SHARED_DIR", "/shared")
CONFIG_TEMPLATE = os.environ.get("CONFIG_TEMPLATE", "/app/chronics.conf.template")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2.0"))
POLL_TIMEOUT = float(os.environ.get("POLL_TIMEOUT", "120.0"))

EXPECTED_OUTPUT_FILES = [
    "load_p.csv",
    "load_q.csv",
    "prod_p.csv",
    "prod_q.csv",
    "prod_v.csv",
]


def execute_GenerateChronics(request) -> ExecuteResponse:
    """Start chronics generation in background thread."""
    if not request.inputs:
        return ExecuteResponse(status="failed", error="No inputs provided")

    input_uri = request.inputs[0].get("uri", "")
    if not input_uri:
        return ExecuteResponse(status="failed", error="No input URI provided")

    # Register task and start background processing
    task_manager.register_task(request.task_id, request)
    run_in_background(request.task_id, _generate_chronics, request)

    return ExecuteResponse(status="running", task_id=request.task_id)


def _generate_chronics(
    task_id: str,
    request,
    manager: TaskManager,
) -> None:
    """Background worker: push config to VILLASnode and wait for output."""
    try:
        input_uri = request.inputs[0]["uri"]

        # Read manifest from data provider
        manifest_path = os.path.join(input_uri, "manifest.json")
        with open(manifest_path) as f:
            manifest = json.load(f)

        loads_dir = manifest["loads_dir"]
        sgens_dir = manifest["sgens_dir"]
        grid_file = manifest["grid_file"]

        # Trigger URIs: the file VILLASnode opens to kick off processing.
        # Provided by data_provider in the manifest.
        first_load = os.path.join(loads_dir, manifest["trigger_load"])
        first_sgen = os.path.join(sgens_dir, manifest["trigger_sgen"])

        # Create output directory
        output_dir = os.path.join(input_uri, "chronics_output")
        os.makedirs(output_dir, exist_ok=True)

        # Get parameters with defaults
        params = request.parameters
        round_decimals = params.get("round_decimals", 3)
        compress = params.get("compress", False)

        # Load template and substitute placeholders
        with open(CONFIG_TEMPLATE) as f:
            config = f.read()

        config = (
            config
            .replace("{{LOAD_URI}}", first_load)
            .replace("{{SGEN_URI}}", first_sgen)
            .replace("{{LOADS_DIR}}", loads_dir)
            .replace("{{SGENS_DIR}}", sgens_dir)
            .replace("{{GRID_FILE}}", grid_file)
            .replace("{{OUTPUT_DIR}}", output_dir)
            .replace("{{ROUND_DECIMALS}}", str(round_decimals))
            .replace("{{COMPRESS}}", "true" if compress else "false")
        )

        # Write rendered config to shared volume for VILLASnode to read
        config_path = os.path.join(input_uri, "villas_config.conf")
        with open(config_path, "w") as f:
            f.write(config)
        logger.info(f"Wrote VILLASnode config to {config_path}")
        manager.update_progress(task_id, 10)

        # Push config file path to VILLASnode via REST API
        restart_url = f"{VILLAS_NODE_URL}/api/v2/restart"
        resp = requests.post(
            restart_url,
            json={"config": config_path},
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"VILLASnode restart response: {resp.status_code} {resp.text}")
        manager.update_progress(task_id, 20)

        # Poll for completion — wait for all 5 output files to appear
        start_time = time.time()
        while time.time() - start_time < POLL_TIMEOUT:
            existing = [
                f
                for f in EXPECTED_OUTPUT_FILES
                if os.path.exists(os.path.join(output_dir, f))
            ]
            progress = 20 + int(80 * len(existing) / len(EXPECTED_OUTPUT_FILES))
            manager.update_progress(task_id, progress)

            if len(existing) == len(EXPECTED_OUTPUT_FILES):
                logger.info(
                    f"All {len(EXPECTED_OUTPUT_FILES)} output files found"
                )
                break
            logger.info(
                f"Waiting for output files: {len(existing)}/{len(EXPECTED_OUTPUT_FILES)} "
                f"({time.time() - start_time:.0f}s elapsed)"
            )
            time.sleep(POLL_INTERVAL)
        else:
            missing = [
                f
                for f in EXPECTED_OUTPUT_FILES
                if not os.path.exists(os.path.join(output_dir, f))
            ]
            manager.fail_task(
                task_id,
                f"Timeout waiting for VILLASnode output. Missing: {missing}",
            )
            return

        manager.complete_task(
            task_id,
            {
                "protocol": "file",
                "uri": output_dir,
                "format": "csv",
            },
        )

    except Exception as e:
        logger.error(f"Chronics generation failed: {e}")
        manager.fail_task(task_id, str(e))


if __name__ == "__main__":
    run_concurrent(__import__(__name__))
