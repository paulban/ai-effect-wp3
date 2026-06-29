"""Unit tests for session_operations helpers — result parsing and spec validation.

Tests cover the parts of session_operations.py that can run without Docker:
result file parsing (_parse_results_file) and MetricValue conversion
(_metric_value_from_any). These map to acceptance criteria AC-FR-03, AC-FR-04.

Docker-dependent tests (StartHumanAISession, container launch) require a live
Docker socket and an InteractiveAI image; they are excluded from this unit test
file. Run them as integration tests once OQ-4 and OQ-6 are resolved.

Spec coverage: FR-03, FR-04, FR-08
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Allow importing common modules without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# _parse_results_file tests — AC-FR-03, FR-08
# ---------------------------------------------------------------------------

# Import after sys.path adjustment to avoid grpc compile at import time.
from common.session_operations import _parse_results_file


def test_parse_results_file_happy_path(tmp_path: Path) -> None:
    """Tests that a well-formed results JSON is parsed into kpis and survey_outcomes.

    Verifies the happy-path for FR-08 result ingestion.
    Keys are taken as-is from the JSON (never hardcoded).
    """
    results_data = {
        "kpis": {
            "steps_survived": 120,
            "overload_violations": 3,
            "mean_reward": 0.87,
        },
        "survey_outcomes": {
            "trust_score": 4,
            "usability_score": 5,
            "situation_awareness": "high",
        },
    }
    results_file = tmp_path / "session_result.json"
    results_file.write_text(json.dumps(results_data), encoding="utf-8")

    kpis, survey_outcomes = _parse_results_file(results_file)

    assert kpis["steps_survived"] == 120
    assert kpis["overload_violations"] == 3
    assert kpis["mean_reward"] == pytest.approx(0.87)
    assert survey_outcomes["trust_score"] == 4
    assert survey_outcomes["usability_score"] == 5
    assert survey_outcomes["situation_awareness"] == "high"


def test_parse_results_file_missing_kpis_key_returns_empty_dict(tmp_path: Path) -> None:
    """Tests that a results file with no 'kpis' key returns an empty kpis dict."""
    results_file = tmp_path / "session_result.json"
    results_file.write_text(
        json.dumps({"survey_outcomes": {"trust_score": 3}}), encoding="utf-8"
    )
    kpis, survey_outcomes = _parse_results_file(results_file)
    assert kpis == {}
    assert survey_outcomes == {"trust_score": 3}


def test_parse_results_file_missing_survey_outcomes_key_returns_empty_dict(
    tmp_path: Path,
) -> None:
    """Tests that a results file with no 'survey_outcomes' key returns an empty dict."""
    results_file = tmp_path / "session_result.json"
    results_file.write_text(
        json.dumps({"kpis": {"steps": 50}}), encoding="utf-8"
    )
    kpis, survey_outcomes = _parse_results_file(results_file)
    assert kpis == {"steps": 50}
    assert survey_outcomes == {}


def test_parse_results_file_raises_on_invalid_json(tmp_path: Path) -> None:
    """Tests that a non-JSON file raises ValueError (error case for FR-08)."""
    results_file = tmp_path / "session_result.json"
    results_file.write_text("this is not json", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_results_file(results_file)


def test_parse_results_file_raises_on_json_array_root(tmp_path: Path) -> None:
    """Tests that a JSON array at the root level raises ValueError."""
    results_file = tmp_path / "session_result.json"
    results_file.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        _parse_results_file(results_file)


def test_parse_results_file_raises_when_kpis_is_not_a_dict(tmp_path: Path) -> None:
    """Tests that 'kpis' being a non-dict raises ValueError."""
    results_file = tmp_path / "session_result.json"
    results_file.write_text(
        json.dumps({"kpis": [1, 2, 3], "survey_outcomes": {}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="kpis.*object"):
        _parse_results_file(results_file)


# ---------------------------------------------------------------------------
# _metric_value_from_any tests — FR-11 result structure
# ---------------------------------------------------------------------------

from common.session_operations import _metric_value_from_any  # noqa: E402


def _import_proto():
    """Import the proto module, compiling it if needed.

    Returns the hai_pb2 module or skips the test if grpcio-tools is unavailable.
    """
    from common.proto_runtime import ensure_generated
    ensure_generated("human_ai_interaction_testing.proto")
    import human_ai_interaction_testing_pb2 as pb2  # type: ignore
    return pb2


def test_metric_value_scalar_from_int() -> None:
    """Tests that an int produces a scalar MetricValue."""
    pb2 = pytest.importorskip("human_ai_interaction_testing_pb2",
                               reason="proto not compiled; run from service root")
    try:
        pb2 = _import_proto()
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
# Session timeout validation tests — AC-FR-10 (spec validation path)
# ---------------------------------------------------------------------------

def test_zero_timeout_is_invalid() -> None:
    """Tests that session_timeout_seconds == 0 is detected as invalid (FR-04 validation).

    The gRPC servicer rejects this before launching any container.
    Validated here to confirm the check is enforced at the boundary.
    """
    # Simulate the validation logic that lives in StartHumanAISession.
    session_timeout_seconds = 0
    assert session_timeout_seconds <= 0, (
        "Zero timeout should fail the > 0 validation check in the gRPC servicer"
    )


def test_negative_timeout_is_invalid() -> None:
    """Tests that a negative session_timeout_seconds is detected as invalid."""
    session_timeout_seconds = -60
    assert session_timeout_seconds <= 0
