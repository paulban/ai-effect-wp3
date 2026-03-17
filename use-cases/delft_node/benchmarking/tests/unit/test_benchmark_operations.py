import base64
import json
from types import SimpleNamespace

from common.benchmark_operations import (
    _decode_inline_json,
    _parse_benchmark_config,
    _validate_algorithm_module,
)


def _inline(payload: dict) -> dict:
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    return {"protocol": "inline", "uri": encoded, "format": "json"}


def test_decode_inline_json_roundtrip():
    payload = {"benchmark": {"episodes": 2}}
    decoded = _decode_inline_json(_inline(payload))
    assert decoded == payload


def test_parse_benchmark_config_defaults():
    cfg = _parse_benchmark_config({})
    assert cfg.max_steps > 0
    assert cfg.episodes > 0


def test_validate_algorithm_module_requires_build_agent():
    module = SimpleNamespace()
    try:
        _validate_algorithm_module(module)
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "build_agent" in str(exc)


def test_validate_algorithm_module_accepts_callable():
    module = SimpleNamespace(build_agent=lambda env, context: None)
    _validate_algorithm_module(module)
