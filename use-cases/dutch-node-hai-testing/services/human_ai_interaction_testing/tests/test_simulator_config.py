"""CONFIG.toml rendering and in-place write (spec FR-13, section 5.6)."""

from __future__ import annotations

import os

import pytest
import toml

from session_config import ScenarioSelection
from simulator_config import render_simulator_config, write_simulator_config_in_place

# Every key of the simulator's shipped CONFIG.toml at InteractiveAI 600a1ce.
SHIPPED_CONFIG_KEYS = {
    "titre",
    "env_name",
    "env_seed",
    "scenario_name",
    "time_step_forecast",
    "duration_step_forecast",
    "refresh_frequency_step",
    "step_start_security_analysis",
    "assistant_path",
    "assistant_seed",
    "stepDuration_s",
    "scenario_first_step",
}

SCENARIO = ScenarioSelection(
    env_name="X", scenario_name="Y", scenario_first_step=752, env_seed=7, step_duration_s=1.5
)


def test_render_sets_request_values_and_keeps_every_key():
    """Section 5.6: the request's values are placed, every shipped key is present."""
    config_values = render_simulator_config(SCENARIO, "Ressources/hai_scenarios")

    assert set(config_values) == SHIPPED_CONFIG_KEYS
    assert config_values["env_name"] == "Ressources/hai_scenarios/X"
    assert config_values["scenario_name"] == "Y"
    assert config_values["scenario_first_step"] == 752
    assert config_values["env_seed"] == 7
    assert config_values["stepDuration_s"] == 1.5
    assert config_values["assistant_path"] == "Ressources/XD_silly_repo"


def test_render_matches_shipped_config_keys_of_the_submodule(hai_settings):
    """The generated keys match the simulator's own CONFIG.toml, if the submodule is checked out."""
    from conftest import SERVICE_DIRECTORY

    shipped_config_path = SERVICE_DIRECTORY / "InteractiveAI" / "usecases_examples" / "PowerGrid" / "config" / "CONFIG.toml"
    if not shipped_config_path.is_file():
        pytest.skip("InteractiveAI submodule not checked out")
    assert set(toml.load(shipped_config_path)) == SHIPPED_CONFIG_KEYS


def test_write_is_in_place(node_directories):
    """CONFIG write in place (9.2): the inode is unchanged and all 12 keys are present."""
    # Given
    config_path = node_directories["simulator_config"]
    inode_before = os.stat(config_path).st_ino

    # When
    write_simulator_config_in_place(config_path, render_simulator_config(SCENARIO, "Ressources/hai_scenarios"))

    # Then
    assert os.stat(config_path).st_ino == inode_before
    written_values = toml.load(config_path)
    assert set(written_values) == SHIPPED_CONFIG_KEYS
    assert written_values["env_name"] == "Ressources/hai_scenarios/X"


def test_write_refuses_missing_file(tmp_path):
    """FR-23a: a missing file means the mount is not in place; nothing is created."""
    missing_path = tmp_path / "CONFIG.toml"
    with pytest.raises(FileNotFoundError, match="setup_interactiveai.sh"):
        write_simulator_config_in_place(missing_path, {"titre": "x"})
    assert not missing_path.exists()
