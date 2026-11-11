"""
Simulator config — the CONFIG.toml that selects the participant's scenario.

The PowerGrid simulator rereads ``config/CONFIG.toml`` every time "Load
simulation" is clicked (``Simulator.load_and_edit_config``). This service and
the simulator share that file through a single-file bind mount, so writing it
is all it takes to set the next run's scenario. The simulator is never called.

Two details keep the mount intact:

- the file is rewritten **in place** (truncate and write). A temp file renamed
  over it would replace the inode, and the simulator's mount would keep
  pointing at the old one;
- every key of the shipped file is written, not only the ones a request sets,
  because the simulator indexes keys such as ``assistant_path`` directly.

Spec coverage: FR-13, section 5.6
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import toml

from session_config import ScenarioSelection
from settings import (
    SIMULATOR_ASSISTANT_PATH,
    SIMULATOR_ASSISTANT_SEED,
    SIMULATOR_CONFIG_TITLE,
    SIMULATOR_DURATION_STEP_FORECAST,
    SIMULATOR_REFRESH_FREQUENCY_STEP,
    SIMULATOR_STEP_START_SECURITY_ANALYSIS,
    SIMULATOR_TIME_STEP_FORECAST,
)


def render_simulator_config(scenario: ScenarioSelection, simulator_env_prefix: str) -> dict[str, Any]:
    """
    Build the full CONFIG.toml content for a scenario (section 5.6).

    Args:
        scenario: The validated scenario selection.
        simulator_env_prefix: Path of the scenarios mount as the simulator sees
            it, relative to its working directory, e.g. ``Ressources/hai_scenarios``.

    Returns:
        A mapping with every key of the simulator's shipped CONFIG.toml, in its order.
    """
    return {
        "titre": SIMULATOR_CONFIG_TITLE,
        "env_name": f"{simulator_env_prefix}/{scenario.env_name}",
        "env_seed": scenario.env_seed,
        "scenario_name": scenario.scenario_name,
        "time_step_forecast": SIMULATOR_TIME_STEP_FORECAST,
        "duration_step_forecast": SIMULATOR_DURATION_STEP_FORECAST,
        "refresh_frequency_step": SIMULATOR_REFRESH_FREQUENCY_STEP,
        "step_start_security_analysis": SIMULATOR_STEP_START_SECURITY_ANALYSIS,
        "assistant_path": SIMULATOR_ASSISTANT_PATH,
        "assistant_seed": SIMULATOR_ASSISTANT_SEED,
        "stepDuration_s": scenario.step_duration_s,
        "scenario_first_step": scenario.scenario_first_step,
    }


def write_simulator_config_in_place(config_path: Path, config_values: dict[str, Any]) -> None:
    """
    Overwrite the shared CONFIG.toml without replacing the file.

    Args:
        config_path: The bind-mounted CONFIG.toml.
        config_values: Content from ``render_simulator_config``.

    Raises:
        FileNotFoundError: If the file does not exist. The setup script creates
            it before the simulator starts (FR-23a); a missing file means the
            mount is not in place and writing a new one would not reach the
            simulator.
    """
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Simulator config {config_path} does not exist. Run "
            "scripts/setup_interactiveai.sh, which creates it and mounts it into the simulator."
        )
    # "w" truncates the existing inode; never write-and-rename (see module docstring).
    with open(config_path, "w", encoding="utf-8") as config_file:
        toml.dump(config_values, config_file)
