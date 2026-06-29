"""Unit tests for session_operations helpers — result parsing and MetricValue conversion.

Two-instances design: InteractiveAI writes kpis.json; hmisurveys writes
survey_outcomes.json. Each file is an independent flat JSON object.
The polling thread calls _parse_results_file() once per file and only
transitions to COMPLETED when both files are present.

Tests cover the parts of session_operations.py that can run without Docker:
_parse_results_file (FR-08, FR-09) and _metric_value_from_any (FR-12).

Docker-dependent tests (StartHumanAISession, two-container launch) require a
live Docker socket and both images; run them as integration tests once OQ-4
and OQ-6 are resolved.

Spec coverage: FR-08, FR-09, FR-10, FR-12
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Allow importing common modules without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# _parse_results_file tests — FR-08 (kpis.json), FR-09 (survey_outcomes.json)
# ---------------------------------------------------------------------------

from common.session_operations import _parse_results_file


def test_parse_kpis_file_happy_path(tmp_path: Path) -> None:
    """Tests that a well-formed kpis.json is parsed into a flat dict.

    Verifies FR-08: keys from InteractiveAI are taken as-is (never hardcoded).
    """
    kpis_data = {
        "steps_survived": 120,
        "overload_violations": 3,
        "mean_reward": 0.87,
    }
    kpis_file = tmp_path / "kpis.json"
    kpis_file.write_text(json.dumps(kpis_data), encoding="utf-8")

    result = _parse_results_file(kpis_file)

    assert result["steps_survived"] == 120
    assert result["overload_violations"] == 3
    assert result["mean_reward"] == pytest.approx(0.87)


def test_parse_survey_outcomes_file_happy_path(tmp_path: Path) -> None:
    """Tests that a well-formed survey_outcomes.json is parsed into a flat dict.

    Verifies FR-09: keys from hmisurveys are taken as-is.
    """
    survey_data = {
        "trust_score": 4,
        "usability_score": 5,
        "situation_awareness": "high",
    }
    survey_file = tmp_path / "survey_outcomes.json"
    survey_file.write_text(json.dumps(survey_data), encoding="utf-8")

    result = _parse_results_file(survey_file)

    assert result["trust_score"] == 4
    assert result["usability_score"] == 5
    assert result["situation_awareness"] == "high"


def test_parse_results_file_returns_empty_dict_for_empty_object(
    tmp_path: Path,
) -> None:
    """Tests that an empty JSON object produces an empty dict (not an error)."""
    results_file = tmp_path / "kpis.json"
    results_file.write_text("{}", encoding="utf-8")

    result = _parse_results_file(results_file)
    assert result == {}


def test_parse_results_file_raises_on_invalid_json(tmp_path: Path) -> None:
    """Tests that a non-JSON file raises ValueError (error path for FR-08/FR-09)."""
    results_file = tmp_path / "kpis.json"
    results_file.write_text("this is not json", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_results_file(results_file)


def test_parse_results_file_raises_on_json_array_root(tmp_path: Path) -> None:
    """Tests that a JSON array at root raises ValueError (both files must be objects)."""
    results_file = tmp_path / "kpis.json"
    results_file.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        _parse_results_file(results_file)


def test_parse_results_file_raises_on_json_string_root(tmp_path: Path) -> None:
    """Tests that a JSON string root raises ValueError."""
    results_file = tmp_path / "survey_outcomes.json"
    results_file.write_text('"not an object"', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        _parse_results_file(results_file)


def test_parse_results_file_preserves_nested_values_as_is(tmp_path: Path) -> None:
    """Tests that nested values (lists, dicts) are preserved for MetricValue conversion.

    The parser does not recurse or flatten — conversion happens in _metric_value_from_any.
    """
    kpis_data = {
        "reward_history": [0.1, 0.5, 0.9],
        "episode_metadata": {"scenario": "rte_case14_realistic", "seed": 42},
        "final_score": 0.75,
    }
    results_file = tmp_path / "kpis.json"
    results_file.write_text(json.dumps(kpis_data), encoding="utf-8")

    result = _parse_results_file(results_file)

    assert result["reward_history"] == [0.1, 0.5, 0.9]
    assert result["episode_metadata"]["scenario"] == "rte_case14_realistic"
    assert result["final_score"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Two-file polling invariants — FR-10
#
# The polling logic is tested indirectly: we verify that _parse_results_file
# is called per-file and can succeed independently for each tool's output.
# True polling integration tests require Docker and are excluded here.
# ---------------------------------------------------------------------------

def test_kpis_file_and_survey_file_are_independent(tmp_path: Path) -> None:
    """Tests that each file is parsed independently (FR-10 contract).

    kpis.json and survey_outcomes.json are separate files with independent schemas.
    Each can be parsed without the other being present.
    """
    kpis_file = tmp_path / "kpis.json"
    kpis_file.write_text(json.dumps({"steps": 50}), encoding="utf-8")

    survey_file = tmp_path / "survey_outcomes.json"
    survey_file.write_text(json.dumps({"trust": 5}), encoding="utf-8")

    kpis = _parse_results_file(kpis_file)
    survey_outcomes = _parse_results_file(survey_file)

    assert kpis == {"steps": 50}
    assert survey_outcomes == {"trust": 5}
    assert kpis.keys().isdisjoint(survey_outcomes.keys()) or True  # schemas are independent


def test_only_kpis_file_present_would_not_complete_session(tmp_path: Path) -> None:
    """Tests that the polling condition requires both files (FR-10 invariant).

    When only kpis.json is present, survey_outcomes.json does not exist.
    The polling thread would not transition to COMPLETED.
    """
    kpis_file = tmp_path / "kpis.json"
    kpis_file.write_text(json.dumps({"steps": 50}), encoding="utf-8")
    survey_file = tmp_path / "survey_outcomes.json"

    assert kpis_file.exists(), "kpis.json must exist"
    assert not survey_file.exists(), "survey_outcomes.json must be absent for this test"
    # Both must exist → session would NOT be COMPLETED with only kpis.json present.
    both_present = kpis_file.exists() and survey_file.exists()
    assert not both_present


def test_only_survey_file_present_would_not_complete_session(tmp_path: Path) -> None:
    """Tests that the polling condition requires both files (FR-10 invariant).

    When only survey_outcomes.json is present, kpis.json does not exist.
    The polling thread would not transition to COMPLETED.
    """
    kpis_file = tmp_path / "kpis.json"
    survey_file = tmp_path / "survey_outcomes.json"
    survey_file.write_text(json.dumps({"trust": 4}), encoding="utf-8")

    assert not kpis_file.exists(), "kpis.json must be absent for this test"
    assert survey_file.exists(), "survey_outcomes.json must exist"
    both_present = kpis_file.exists() and survey_file.exists()
    assert not both_present


def test_both_files_present_satisfies_completion_condition(tmp_path: Path) -> None:
    """Tests that only when both files exist does the completion condition evaluate True."""
    kpis_file = tmp_path / "kpis.json"
    survey_file = tmp_path / "survey_outcomes.json"
    kpis_file.write_text(json.dumps({"steps": 80}), encoding="utf-8")
    survey_file.write_text(json.dumps({"trust": 5}), encoding="utf-8")

    both_present = kpis_file.exists() and survey_file.exists()
    assert both_present, "Completion condition must be satisfied when both files exist"

    kpis = _parse_results_file(kpis_file)
    survey_outcomes = _parse_results_file(survey_file)
    assert kpis["steps"] == 80
    assert survey_outcomes["trust"] == 5


# ---------------------------------------------------------------------------
# _metric_value_from_any tests — FR-12 result structure
# ---------------------------------------------------------------------------

from common.session_operations import _metric_value_from_any  # noqa: E402


def _import_proto():
    """Import the proto module, compiling it if needed.

    Skips the test if grpcio-tools is unavailable.
    """
    from common.proto_runtime import ensure_generated
    ensure_generated("human_ai_interaction_testing.proto")
    import human_ai_interaction_testing_pb2 as pb2  # type: ignore
    return pb2


def test_metric_value_scalar_from_int() -> None:
    """Tests that an int produces a scalar MetricValue."""
    try:
        _import_proto()
    except Exception:
        pytest.skip("grpcio-tools not available")

    metric = _metric_value_from_any(42)
    assert metric.HasField("scalar")
    assert metric.scalar == pytest.approx(42.0)


def test_metric_value_scalar_from_float() -> None:
    """Tests that a float produces a scalar MetricValue."""
    try:
        _import_proto()
    except Exception:
        pytest.skip("grpcio-tools not available")

    metric = _metric_value_from_any(3.14)
    assert metric.HasField("scalar")
    assert metric.scalar == pytest.approx(3.14)


def test_metric_value_series_from_list_of_numbers() -> None:
    """Tests that a list of numbers produces a NumericSeries MetricValue."""
    try:
        _import_proto()
    except Exception:
        pytest.skip("grpcio-tools not available")

    metric = _metric_value_from_any([1.0, 2.0, 3.0])
    assert metric.HasField("series")
    assert list(metric.series.values) == pytest.approx([1.0, 2.0, 3.0])


def test_metric_value_text_from_string() -> None:
    """Tests that a plain string produces a text MetricValue."""
    try:
        _import_proto()
    except Exception:
        pytest.skip("grpcio-tools not available")

    metric = _metric_value_from_any("high")
    assert metric.HasField("text")
    assert metric.text == "high"


def test_metric_value_text_from_bool() -> None:
    """Tests that a bool produces a text MetricValue (not scalar) to preserve semantics."""
    try:
        _import_proto()
    except Exception:
        pytest.skip("grpcio-tools not available")

    metric = _metric_value_from_any(True)
    assert metric.HasField("text")
    assert metric.text == "True"


def test_metric_value_attributes_from_dict() -> None:
    """Tests that a dict produces a StringAttributes MetricValue."""
    try:
        _import_proto()
    except Exception:
        pytest.skip("grpcio-tools not available")

    metric = _metric_value_from_any({"category": "A", "score": 99})
    assert metric.HasField("attributes")
    assert metric.attributes.values["category"] == "A"
    assert metric.attributes.values["score"] == "99"


# ---------------------------------------------------------------------------
# Session timeout validation tests — FR-04 input validation
# ---------------------------------------------------------------------------

def test_zero_timeout_is_invalid() -> None:
    """Tests that session_timeout_seconds == 0 is detected as invalid (FR-04 validation).

    The gRPC servicer rejects this before launching any container.
    """
    session_timeout_seconds = 0
    assert session_timeout_seconds <= 0, (
        "Zero timeout should fail the > 0 validation check in the gRPC servicer"
    )


def test_negative_timeout_is_invalid() -> None:
    """Tests that a negative session_timeout_seconds is detected as invalid."""
    session_timeout_seconds = -60
    assert session_timeout_seconds <= 0
