"""Data Generator service - generates synthetic energy data."""

import base64
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)


def _decode_inline_input(inputs: list[dict]) -> dict:
    """Decode inline JSON input from previous service."""
    if not inputs:
        return {}

    for inp in inputs:
        if inp.get("protocol") == "inline" and inp.get("format") == "json":
            b64_data = inp.get("uri", "")
            json_str = base64.b64decode(b64_data).decode()
            return json.loads(json_str)

    return {}


def execute_GenerateData(request: ExecuteRequest) -> ExecuteResponse:
    """Generate synthetic energy data based on input configuration.

    Input: Inline JSON with num_records and output_format.
    Output: File path to generated CSV.
    """
    # Get configuration from input
    config = _decode_inline_input(request.inputs)
    num_records = config.get("num_records", 10)
    output_format = config.get("output_format", "csv")

    logger.info(f"GenerateData: {num_records} records, format={output_format}")

    # Generate synthetic energy data
    data = {
        "timestamp": [f"2025-01-01T00:00:{i:02d}Z" for i in range(num_records)],
        "household_id": [f"HH-{i%3}" for i in range(num_records)],
        "power_consumption": [120.5 + i for i in range(num_records)],
        "voltage": [230.0] * num_records,
        "current": [5.1 + (i * 0.1) for i in range(num_records)],
    }

    # Save to CSV
    output_path = Path("data/raw_energy.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(data)
    df.to_csv(output_path, index=False)

    logger.info(f"Generated {num_records} records to {output_path}")

    return ExecuteResponse(
        status="complete",
        output=DataReference(
            protocol="file",
            uri=str(output_path.absolute()),
            format="csv",
        ),
    )


if __name__ == "__main__":
    run(sys.modules[__name__])
