"""Input Provider service - passes configuration to downstream via inline data.

This is a config-only service. It does NOT expose a gRPC server.
Instead, it passes configuration inline to downstream services through the orchestrator.

Data flow:
  input_provider ──(inline config)──> data_generator ──(gRPC)──> ...
"""

import base64
import json
import logging
import sys

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)


def execute_GetConfiguration(request: ExecuteRequest) -> ExecuteResponse:
    """Return configuration as inline data for downstream services."""
    num_records = request.parameters.get("num_records", 10)

    logger.info("=" * 60)
    logger.info("INPUT PROVIDER - Configuration")
    logger.info("=" * 60)
    logger.info(f"  Number of records to generate: {num_records}")
    logger.info("=" * 60)

    # Pass config inline to downstream (no gRPC callback needed)
    config = {"num_records": num_records}
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
    # No gRPC server - just HTTP control interface
    run(sys.modules[__name__])
