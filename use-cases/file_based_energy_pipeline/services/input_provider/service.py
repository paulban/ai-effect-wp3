"""Input Provider service - provides initial configuration for the pipeline."""

import base64
import json
import logging
import sys

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)


def execute_GetConfiguration(request: ExecuteRequest) -> ExecuteResponse:
    """Return predefined configuration for the pipeline.

    Output is an inline JSON DataReference containing:
    - num_records: Number of records to generate
    - output_format: Output format (csv)
    """
    num_records = 10
    output_format = "csv"

    logger.info(f"GetConfiguration: num_records={num_records}, format={output_format}")

    # Encode configuration as inline JSON
    config = {
        "num_records": num_records,
        "output_format": output_format,
    }
    config_json = json.dumps(config)
    config_b64 = base64.b64encode(config_json.encode()).decode()

    return ExecuteResponse(
        status="complete",
        output=DataReference(
            protocol="inline",
            uri=config_b64,
            format="json",
        ),
    )


if __name__ == "__main__":
    run(sys.modules[__name__])
