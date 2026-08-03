"""Data Generator service - HTTP control + gRPC data interface.

Receives configuration from upstream (input_provider) via inline data.
Generates energy data and exposes it via gRPC for downstream services.

Data flow:
  input_provider ──(inline config)──> data_generator ──(gRPC)──> data_analyzer
"""

import base64
import json
import logging
import os
import random
import sys
import threading
from concurrent import futures
from datetime import datetime, timedelta

import grpc

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

# Import generated protobuf classes
import common_pb2
import data_generator_pb2
import data_generator_pb2_grpc

logger = logging.getLogger(__name__)

# Cache generated data for downstream gRPC calls
_cached_response: data_generator_pb2.GenerateDataResponse | None = None
_cache_lock = threading.Lock()


class DataGeneratorServicer(data_generator_pb2_grpc.DataGeneratorServiceServicer):
    """gRPC servicer for data generation requests."""

    def GenerateData(self, request, context):
        """Return cached generated data to downstream services."""
        with _cache_lock:
            if _cached_response is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details("No data available. Service not yet triggered.")
                return data_generator_pb2.GenerateDataResponse(
                    success=False,
                    message="No data available",
                )
            return _cached_response


def _generate_data(num_records: int) -> data_generator_pb2.GenerateDataResponse:
    """Generate synthetic energy data."""
    logger.info(f"Generating {num_records} records")

    response = data_generator_pb2.GenerateDataResponse()
    base_time = datetime.now()
    households = ["HH-001", "HH-002", "HH-003", "HH-004", "HH-005"]

    for i in range(num_records):
        record = common_pb2.EnergyRecord()
        timestamp = base_time + timedelta(minutes=i)
        record.timestamp = timestamp.isoformat() + "Z"
        record.household_id = households[i % len(households)]

        base_consumption = 120.0 + (i % 10) * 5
        record.power_consumption = base_consumption + random.uniform(-10, 10)
        record.voltage = 230.0 + random.uniform(-5, 5)
        record.current = record.power_consumption / record.voltage

        response.records.append(record)

    response.success = True
    response.message = f"Successfully generated {len(response.records)} energy records"

    logger.info("=" * 60)
    logger.info("DATA GENERATOR - Generated Records")
    logger.info("=" * 60)
    logger.info(f"  Total records: {len(response.records)}")
    logger.info(f"  Households: {', '.join(households)}")
    logger.info("  Sample records:")
    for i, rec in enumerate(response.records[:3]):
        logger.info(f"    [{i+1}] {rec.household_id}: {rec.power_consumption:.1f}W @ {rec.voltage:.1f}V")
    if len(response.records) > 3:
        logger.info(f"    ... and {len(response.records) - 3} more")
    logger.info("=" * 60)

    return response


def _parse_inline_config(inputs: list[dict]) -> dict:
    """Parse inline config from input DataReferences."""
    for inp in inputs:
        if inp.get("protocol") == "inline":
            try:
                config_b64 = inp.get("uri", "")
                config_json = base64.b64decode(config_b64).decode()
                return json.loads(config_json)
            except Exception as e:
                logger.warning(f"Failed to parse inline config: {e}")
    return {}


def start_grpc_server():
    """Start gRPC server in background thread."""
    grpc_port = os.environ.get("GRPC_PORT", "50051")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    data_generator_pb2_grpc.add_DataGeneratorServiceServicer_to_server(
        DataGeneratorServicer(), server
    )
    server.add_insecure_port(f"[::]:{grpc_port}")
    server.start()
    logger.info(f"gRPC server started on port {grpc_port}")
    return server


# --- HTTP Control Interface ---

def execute_GenerateData(request: ExecuteRequest) -> ExecuteResponse:
    """HTTP handler: Read config from input, generate data, cache for downstream."""
    global _cached_response

    # Parse config from inline input (from input_provider)
    config = _parse_inline_config(request.inputs)
    num_records = config.get("num_records", 10)

    # Override with parameters if provided
    num_records = request.parameters.get("num_records", num_records)

    logger.info(f"GenerateData: num_records={num_records}")

    # Generate data and cache for downstream gRPC calls
    result = _generate_data(num_records)

    with _cache_lock:
        _cached_response = result

    if not result.success:
        return ExecuteResponse(status="failed", error=result.message)

    # Return gRPC endpoint for downstream to fetch the generated data
    grpc_host = os.environ.get("GRPC_HOST", "data-generator")
    grpc_port = os.environ.get("GRPC_PORT", "50051")

    return ExecuteResponse(
        status="complete",
        output=DataReference(
            protocol="grpc",
            uri=f"{grpc_host}:{grpc_port}",
            format="GenerateData",
        ),
    )


if __name__ == "__main__":
    # Start gRPC server in background
    grpc_server = start_grpc_server()

    # Run HTTP control interface (blocking)
    run(sys.modules[__name__])
