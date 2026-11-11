"""Request decoding and validation (spec FR-02, FR-03, section 5.3, NFR-07)."""

from __future__ import annotations

import base64
import json

import pytest
from conftest import FAKE_CHRONIC_NAME, FAKE_ENVIRONMENT_NAME, valid_payload

from session_config import parse_session_config, read_request_payload


def parse(payload, hai_settings):
    """Validate a payload against the temporary node folders."""
    return parse_session_config(payload, hai_settings.algorithm_directory, hai_settings.scenario_directory)


def inline_input(payload) -> dict:
    """Encode a payload the way the orchestrator forwards it."""
    return {"protocol": "inline", "uri": base64.b64encode(json.dumps(payload).encode()).decode(), "format": "json"}


def test_valid_payload_fills_defaults(hai_settings):
    """A minimal valid request is accepted and the section 5.3 defaults are applied."""
    # Given / When
    config = parse(valid_payload(), hai_settings)

    # Then
    assert config.algorithm == "algorithm_template.py"
    assert config.scenario.env_name == FAKE_ENVIRONMENT_NAME
    assert config.scenario.scenario_name == FAKE_CHRONIC_NAME
    assert config.scenario.scenario_first_step == 0
    assert config.scenario.env_seed == 0
    assert config.scenario.step_duration_s == 0.5
    assert config.kpis == ()
    assert config.kpi_warnings() == []
    assert config.scenario_directory == hai_settings.scenario_directory / FAKE_ENVIRONMENT_NAME


def test_read_request_payload_decodes_inline_base64():
    """FR-02: the single inline base64 JSON input is decoded."""
    assert read_request_payload([inline_input({"a": 1})]) == {"a": 1}


@pytest.mark.parametrize(
    "inputs, message_fragment",
    [
        ([], "none was given"),
        ([{"protocol": "http", "uri": "x"}], "Expected 'inline'"),
        ([{"protocol": "inline", "uri": "not-base64!"}], "Invalid inline"),
        ([{"protocol": "inline", "uri": base64.b64encode(b"[1]").decode()}], "JSON object"),
    ],
)
def test_read_request_payload_rejects_unusable_input(inputs, message_fragment):
    """FR-02: anything but one inline JSON object is refused with a clear reason."""
    with pytest.raises(ValueError, match=message_fragment):
        read_request_payload(inputs)


def test_missing_session_duration_is_rejected(hai_settings):
    """Validation: missing required field (9.2) names the field."""
    payload = valid_payload()
    del payload["session_duration_seconds"]
    with pytest.raises(ValueError, match="session_duration_seconds"):
        parse(payload, hai_settings)


@pytest.mark.parametrize("duration", [59, 2_592_001, 600.0, True, "3600"])
def test_session_duration_out_of_bounds_is_rejected(hai_settings, duration):
    """NFR-04: durations outside 60 s to 30 days, or not integers, are refused."""
    with pytest.raises(ValueError, match="session_duration_seconds"):
        parse(valid_payload(session_duration_seconds=duration), hai_settings)


def test_algorithm_path_traversal_is_rejected(hai_settings):
    """FR-03 / NFR-07: ``algorithm: "../service.py"`` fails with an error naming ``algorithm``."""
    with pytest.raises(ValueError, match="algorithm"):
        parse(valid_payload(algorithm="../service.py"), hai_settings)


def test_env_name_path_traversal_is_rejected(hai_settings):
    """Validation: path traversal (9.2) on ``scenario.env_name``."""
    payload = valid_payload(scenario={"env_name": "../x", "scenario_name": FAKE_CHRONIC_NAME})
    with pytest.raises(ValueError, match="scenario.env_name"):
        parse(payload, hai_settings)


def test_unknown_algorithm_lists_available(hai_settings):
    """An algorithm not in the folder is refused, naming what is available."""
    with pytest.raises(ValueError, match="algorithm_template.py"):
        parse(valid_payload(algorithm="missing.py"), hai_settings)


def test_unknown_scenario_key_is_rejected(hai_settings):
    """Validation: unknown key (9.2) — ``scenario.max_steps`` is listed in the error."""
    payload = valid_payload(
        scenario={"env_name": FAKE_ENVIRONMENT_NAME, "scenario_name": FAKE_CHRONIC_NAME, "max_steps": 10}
    )
    with pytest.raises(ValueError, match="max_steps"):
        parse(payload, hai_settings)


def test_unknown_top_level_key_is_rejected(hai_settings):
    """Unknown top-level keys are refused rather than ignored."""
    with pytest.raises(ValueError, match="episode_length"):
        parse(valid_payload(episode_length=100), hai_settings)


def test_chronic_missing_forecast_series_is_rejected(hai_settings):
    """FR-03: a chronic lacking ``load_q_forecasted`` is refused, naming the field and the file."""
    # Given
    chronic_directory = hai_settings.scenario_directory / FAKE_ENVIRONMENT_NAME / "chronics" / FAKE_CHRONIC_NAME
    (chronic_directory / "load_q_forecasted.csv.bz2").unlink()

    # When / Then
    with pytest.raises(ValueError, match="scenario.scenario_name.*load_q_forecasted"):
        parse(valid_payload(), hai_settings)


def test_unknown_chronic_is_rejected(hai_settings):
    """A chronic folder that does not exist is refused."""
    payload = valid_payload(scenario={"env_name": FAKE_ENVIRONMENT_NAME, "scenario_name": "nope"})
    with pytest.raises(ValueError, match="scenario.scenario_name"):
        parse(payload, hai_settings)


def test_builtin_environment_name_is_rejected(hai_settings):
    """Built-in grid2op names are not supported: the simulator needs the env on disk."""
    payload = valid_payload(scenario={"env_name": "l2rpn_case14_sandbox", "scenario_name": "0"})
    with pytest.raises(ValueError, match="scenario.env_name"):
        parse(payload, hai_settings)


@pytest.mark.parametrize("step_duration", [0, -1, 61, "fast"])
def test_step_duration_out_of_bounds_is_rejected(hai_settings, step_duration):
    """``step_duration_s`` must be a number in (0, 60]."""
    payload = valid_payload(
        scenario={"env_name": FAKE_ENVIRONMENT_NAME, "scenario_name": FAKE_CHRONIC_NAME, "step_duration_s": step_duration}
    )
    with pytest.raises(ValueError, match="step_duration_s"):
        parse(payload, hai_settings)


def test_requested_kpis_are_accepted_with_warning(hai_settings):
    """Validation: kpis given (9.2) — accepted, and a warning names them."""
    config = parse(valid_payload(kpis=["operation_score"]), hai_settings)
    assert config.kpis == ("operation_score",)
    assert config.kpi_warnings() == ["kpis requested but not computed in this version: ['operation_score']"]


def test_kpis_must_be_strings(hai_settings):
    """``kpis`` must be a list of strings."""
    with pytest.raises(ValueError, match="kpis"):
        parse(valid_payload(kpis="operation_score"), hai_settings)


def test_result_dict_has_no_local_paths(hai_settings):
    """The recorded config is the request with defaults, without node paths."""
    result_config = parse(valid_payload(), hai_settings).to_result_dict()
    assert set(result_config) == {"algorithm", "scenario", "session_duration_seconds", "kpis"}
    assert "scenario_directory" not in json.dumps(result_config)
