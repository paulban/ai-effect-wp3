"""Draw Topology service for the Danish Node workflow.

Pipeline role: final visualization step after topology generation.
Operation exposed to the orchestrator: RenderTopologies.
"""

import base64
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from draw_network import NetworkDrawing
from handler import (
    DataReference,
    ExecuteRequest,
    ExecuteResponse,
    TaskManager,
    run,
    run_in_background,
    task_manager,
)

logger = logging.getLogger(__name__)
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))


def resolve_input(inputs: list[dict], default: Any = None) -> Any:
    if not inputs:
        return default

    reference = inputs[0]
    protocol = reference.get("protocol")
    uri = reference.get("uri", "")
    if protocol == "inline":
        return json.loads(base64.b64decode(uri))
    if protocol == "file":
        path = Path(uri)
        if not path.is_file():
            raise ValueError(f"Input file not found on shared volume: {uri}")
        return json.loads(path.read_text(encoding="utf-8"))
    raise ValueError(f"Unsupported input protocol '{protocol}'")


def publish_output(workflow_id: str, task_id: str, payload: dict) -> dict:
    output_dir = DATA_DIR / workflow_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{task_id}.json"
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"protocol": "file", "uri": str(output_path), "format": "json"}


def execute_RenderTopologies(request: ExecuteRequest) -> ExecuteResponse:
    """Render SVG drawings for each generated network candidate."""
    task_manager.register_task(request.task_id, request)
    run_in_background(request.task_id, _render_topologies, request)
    return ExecuteResponse(status="running", task_id=request.task_id)


def _render_topologies(
    task_id: str,
    request: ExecuteRequest,
    manager: TaskManager,
) -> None:
    """Background worker: render all topology candidate drawings to SVG."""
    try:
        topology_output = resolve_input(request.inputs, default={}) or {}
        if not topology_output or "networks" not in topology_output:
            raise ValueError(
                "RenderTopologies expects topology_generator output "
                "(a JSON object containing 'networks')."
            )

        networks = topology_output.get("networks", [])
        if not networks:
            raise ValueError("topology output contains no network candidates to render")

        manager.update_progress(task_id, 20)
        data_dir = DATA_DIR / request.workflow_id
        data_dir.mkdir(parents=True, exist_ok=True)

        drawings = []
        for index, network in enumerate(networks, 1):
            drawing = NetworkDrawing(network, index, len(networks)).render()
            drawing_path = data_dir / f"{task_id}_network_{index:03d}.svg"
            drawing_path.write_text(drawing, encoding="utf-8")
            drawings.append(
                {
                    "network_index": index,
                    "signature": network.get("signature"),
                    "protocol": "file",
                    "uri": str(drawing_path),
                    "format": "svg",
                }
            )

        manager.update_progress(task_id, 80)
        payload = {
            "run_config": topology_output.get("run_config", {}),
            "config_report": topology_output.get("config_report", {}),
            "topology_count": len(networks),
            "drawing_count": len(drawings),
            "drawings": drawings,
            "recommended_network": topology_output.get("recommended_network"),
            "report": {
                "summary": (
                    f"Rendered {len(drawings)} topology drawing(s) for review and confirmation."
                ),
                "recommended_network": topology_output.get("recommended_network", {}).get("signature"),
                "reason": topology_output.get("report", {}).get("reason"),
                "selection": topology_output.get("report", {}).get("selection"),
                "user_confirmation_required": True,
            },
            "generated_by": "draw_topology",
        }

        manager.complete_task(
            task_id,
            publish_output(request.workflow_id, task_id, payload),
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("RenderTopologies failed")
        manager.fail_task(task_id, str(exc))


if __name__ == "__main__":
    run(sys.modules[__name__])
