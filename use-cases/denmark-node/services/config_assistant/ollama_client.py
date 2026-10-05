"""Minimal client that asks a local Ollama model to extract a run config from
free-text prompts. Uses Ollama's native /api/chat JSON mode - no extra SDK.

Set OLLAMA_URL (default http://host.docker.internal:11434) and OLLAMA_MODEL
(default qwen2.5:1.5b) to point at a different server/model. If Ollama is
unreachable or the model does not exist, callers receive an empty dict and a
failure reason for the configuration service to report.
"""

from __future__ import annotations

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "240"))


# System prompt for the Ollama model, instructing it to convert free-text prompts into JSON run configurations. This is important for ensuring that the model's output adheres to the expected structure.

#TODO: Consider adding more detailed instructions or examples to the system prompt to improve the model's accuracy.
#TODO: Verify with Zahra's original source code
SYSTEM_PROMPT = """You turn a plain-language district-heating experiment request \
into a JSON run configuration for the SYSLAB testbed. Reply with ONLY a JSON \
object, no prose, matching this shape:

{
  "run_name": "string, optional",
  "supplies": ["SWITCHBOARD::Name", ...],
  "loads": ["SWITCHBOARD::Name", ...],
  "allow_hx": true/false,
  "allow_bypass": true/false,
  "max_extra_pipes": integer 0-3,
  "load_topology": "any" | "series" | "parallel"
}

Known switchboards: 310-D, 310-T, 117-D, 716-D, 330-D. Known terminals include \
310-D::CHP (a supply) and 716-D::Dumpload, 330-D::Heat dumpload (loads). If the \
prompt does not mention a field, omit it from the JSON rather than guessing."""



# Parse a free-text prompt into a run configuration. User input is supplied in the 'prompt' argument.
def parse_prompt_to_config(prompt: str) -> tuple[dict, str | None]:
    """Return (extracted_config, error). extracted_config is {} on any failure."""
    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "format": "json",
                "stream": False, # set true to receive partial responses as they are generated
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        extracted = json.loads(content)
        if not isinstance(extracted, dict):
            return {}, "ollama returned a non-object JSON value"
        return extracted, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ollama prompt parsing failed: %s", exc)
        return {}, f"ollama prompt parsing failed ({exc})"
