"""Configuration assistant for the Danish Node topology workflow."""

import base64
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from ollama_client import parse_prompt_to_config
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


TERMINAL_ALIASES = {
    "switchboard::310-d": "310-D::CHP",
    "switchboard::716-d": "716-D::Dumpload",
    "716-d::heat load": "716-D::Dumpload",
    "716-d::heat dumpload": "716-D::Dumpload",
    "716-d::dump load": "716-D::Dumpload",
    "330-d::dumpload": "330-D::Heat dumpload",
    "330-d::heat load": "330-D::Heat dumpload",
}

SUPPLY_TERMINALS = {
    "310-D::CHP",
    "310-D::Gas boiler",
    "310-T::Solar",
    "310-T::Diesel",
    "310-T::PTX A",
    "310-T::PTX B",
    "117-D::Booster heater",
    "117-D::Heatpump",
    "117-D::Solar",
    "716-D::Booster heater",
    "716-D::Heatpump",
    "330-D::Booster heater",
    "330-D::HP W2W output",
    "330-D::HP A2W",
}

LOAD_TERMINALS = {
    "310-D::Building 319",
    "117-D::Lab 117",
    "117-D::Building 117",
    "117-D::Dumpload",
    "716-D::Dumpload",
    "716-D::Flexhouse 2",
    "716-D::Flexhouse 3",
    "330-D::Workshop",
    "330-D::Building 330",
    "330-D::Demo lab 1",
    "330-D::Demo lab 2",
    "330-D::HP W2W input",
    "330-D::Heat dumpload",
}

PROMPT_GUIDANCE = (
    "Please specify at least one known heat source and one known heat load. "
    "For example: 'Use the CHP at 310-D as supply and feed the heat dumpload on 716-D.'"
)


# Minimal configuration used when a user request omits a source or load.
MINIMAL_CONFIG = {
    "run_name": "guided-danish-node-run",
    "supplies": ["310-D::CHP"],
    "loads": ["716-D::Dumpload"],
    "allow_hx": True,
    "allow_bypass": False,
    "max_extra_pipes": 1,
    "load_topology": "any",
}

#TODO: Take input from user, and supply it to LLM to generate the run configuration

#TODO: Also allow user to supply their own config.yaml file


def _normalize_terminals(config: dict) -> None:
    def normalize_terminal(terminal: object) -> object:
        normalized = str(terminal).strip()
        normalized = TERMINAL_ALIASES.get(normalized.lower(), normalized)
        if normalized.lower().startswith("switchboard::"):
            normalized = normalized[len("switchboard::"):]
        return TERMINAL_ALIASES.get(normalized.lower(), normalized)

    for field in ("supplies", "loads"):
        terminals = config.get(field)
        if isinstance(terminals, list):
            config[field] = [normalize_terminal(terminal) for terminal in terminals]


def _complete_minimal_config(config: dict, warnings: list[str]) -> None:
    supplies = [terminal for terminal in config.get("supplies", []) if terminal in SUPPLY_TERMINALS]
    loads = [terminal for terminal in config.get("loads", []) if terminal in LOAD_TERMINALS]

    if not supplies:
        supplies = MINIMAL_CONFIG["supplies"]
        warnings.append("No recognized heat source was specified; using 310-D::CHP.")
    if not loads:
        loads = MINIMAL_CONFIG["loads"]
        warnings.append("No recognized heat load was specified; using 716-D::Dumpload.")

    config["supplies"] = supplies
    config["loads"] = loads
    for field, value in MINIMAL_CONFIG.items():
        config.setdefault(field, value)


# Entry point for building the run configuration based on user input or defaults.
def execute_BuildRunConfig(request: ExecuteRequest) -> ExecuteResponse:
    task_manager.register_task(request.task_id, request)
    run_in_background(request.task_id, _build_run_config, request)
    return ExecuteResponse(status="running", task_id=request.task_id)


# an internal function to build the run configuration based on user input or defaults.
def _build_run_config(task_id: str, request: ExecuteRequest, manager: TaskManager) -> None:
    try:
        supplied = resolve_input(request.inputs, default={}) or {}
        if isinstance(supplied, str):
            supplied = {"prompt": supplied}
        if not isinstance(supplied, dict):
            raise ValueError("configuration input must be a JSON object or text prompt")

        warnings: list[str] = []
        #TODO: Implement logic to handle user-supplied config.yaml file if provided or user input prompt
        prompt = supplied.pop("prompt", None)
        config: dict = {}
        if prompt:
            manager.update_progress(task_id, 20)
            extracted, error = parse_prompt_to_config(prompt)
            config.update(extracted)
            if error:
                warnings.append(error)
        config.update(supplied)
        config.update(request.parameters)
        _normalize_terminals(config)
        _complete_minimal_config(config, warnings)

        manager.update_progress(task_id, 60)
        output = {
            "run_config": config,
            "plant": {
                "source": "embedded-syslab-heat-topology",
                "note": "Consumed by the integrated dh_network_generator solver.",
                "switchboards": ["310-D", "310-T", "117-D", "716-D", "330-D"],
            },
            "valid": True,
            "warnings": warnings,
            "report": {
                "summary": (
                    f"Generated a run configuration for {len(config.get('supplies', []))} "
                    f"supply and {len(config.get('loads', []))} load definition(s)."
                ),
                "assumptions": warnings,
                "next_step": "GenerateTopology",
                "user_confirmation_required": True,
            },
            "generated_by": "config_assistant",
        }
        manager.complete_task(task_id, publish_output(request.workflow_id, task_id, output))
    except Exception as exc:  # noqa: BLE001
        logger.exception("BuildRunConfig failed")
        manager.fail_task(task_id, str(exc))


if __name__ == "__main__":
    run(sys.modules[__name__])
