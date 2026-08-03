"""Data Analyzer service - HTTP control + gRPC data interface.

Receives data from upstream (data_generator) via gRPC.
Analyzes data and caches results for downstream gRPC calls.

Data flow:
  data_generator ──(gRPC)──> data_analyzer ──(gRPC)──> report_generator
"""

import logging
import os
import sys
import threading
from concurrent import futures
from statistics import mean, stdev

import grpc

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

# Import generated protobuf classes
import common_pb2
import data_analyzer_pb2
import data_analyzer_pb2_grpc
import data_generator_pb2
import data_generator_pb2_grpc

logger = logging.getLogger(__name__)

# Cache analyzed data for downstream gRPC calls
_cached_response: data_analyzer_pb2.AnalyzeDataResponse | None = None
_cache_lock = threading.Lock()


class DataAnalyzerServicer(data_analyzer_pb2_grpc.DataAnalyzerServiceServicer):
    """gRPC servicer for data analysis requests."""

    def AnalyzeData(self, request, context):
        """Return cached analysis results to downstream services."""
        with _cache_lock:
            if _cached_response is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details("No data available. Service not yet triggered.")
                return data_analyzer_pb2.AnalyzeDataResponse(
                    success=False,
                    message="No data available",
                )
            return _cached_response


def _analyze_data(
    records: list[common_pb2.EnergyRecord],
    anomaly_threshold: float = 2.0,
    verbose: bool = False,
) -> data_analyzer_pb2.AnalyzeDataResponse:
    """Analyze energy data."""
    logger.info(f"Analyzing {len(records)} records, threshold={anomaly_threshold}")

    response = data_analyzer_pb2.AnalyzeDataResponse()

    if not records:
        response.success = False
        response.message = "No records provided"
        return response

    power_consumptions = [r.power_consumption for r in records]
    mean_consumption = mean(power_consumptions)
    std_consumption = stdev(power_consumptions) if len(power_consumptions) > 1 else 0

    efficiency_scores = []
    anomaly_count = 0

    for record in records:
        analyzed = common_pb2.AnalyzedRecord()
        analyzed.original.CopyFrom(record)

        # Calculate efficiency
        expected_current = record.power_consumption / record.voltage
        actual_current = record.current
        efficiency = 1.0 - abs(expected_current - actual_current) / max(expected_current, 0.001)
        analyzed.efficiency_score = max(0, min(1.0, efficiency))
        efficiency_scores.append(analyzed.efficiency_score)

        # Detect anomalies
        deviation = abs(record.power_consumption - mean_consumption)
        if std_consumption > 0:
            z_score = deviation / std_consumption
            if z_score > anomaly_threshold:
                analyzed.is_anomaly = True
                analyzed.anomaly_reason = f"Power consumption deviation: z-score={z_score:.2f}"
                anomaly_count += 1

        if record.voltage < 220 or record.voltage > 240:
            analyzed.is_anomaly = True
            analyzed.anomaly_reason = f"Voltage out of range: {record.voltage:.1f}V"
            anomaly_count += 1

        response.analyzed_records.append(analyzed)

    response.total_records = len(records)
    response.anomalies_detected = anomaly_count
    response.average_efficiency = mean(efficiency_scores) if efficiency_scores else 0
    response.success = True
    response.message = f"Analyzed {response.total_records} records, found {response.anomalies_detected} anomalies"

    if verbose:
        logger.info("=" * 60)
        logger.info("DATA ANALYZER - Analysis Results")
        logger.info("=" * 60)
        logger.info(f"  Total records analyzed: {response.total_records}")
        logger.info(f"  Anomalies detected: {response.anomalies_detected}")
        logger.info(f"  Anomaly rate: {(response.anomalies_detected / max(response.total_records, 1)):.1%}")
        logger.info(f"  Average efficiency: {response.average_efficiency:.1%}")
        logger.info("=" * 60)
    else:
        logger.info(response.message)

    return response


def _fetch_data_from_upstream(grpc_uri: str) -> data_generator_pb2.GenerateDataResponse:
    """Fetch generated data from data_generator via gRPC."""
    logger.info(f"Calling GenerateData on {grpc_uri}")

    channel = grpc.insecure_channel(grpc_uri)
    stub = data_generator_pb2_grpc.DataGeneratorServiceStub(channel)

    try:
        # Call the actual method directly
        response = stub.GenerateData(data_generator_pb2.GenerateDataRequest())
        logger.info(f"Got {len(response.records)} records from upstream")
        return response
    finally:
        channel.close()


def start_grpc_server():
    """Start gRPC server in background thread."""
    grpc_port = os.environ.get("GRPC_PORT", "50051")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    data_analyzer_pb2_grpc.add_DataAnalyzerServiceServicer_to_server(
        DataAnalyzerServicer(), server
    )
    server.add_insecure_port(f"[::]:{grpc_port}")
    server.start()
    logger.info(f"gRPC server started on port {grpc_port}")
    return server


# --- HTTP Control Interface ---

def execute_AnalyzeData(request: ExecuteRequest) -> ExecuteResponse:
    """HTTP handler: Fetch data via gRPC, analyze it, cache for downstream."""
    global _cached_response

    records = []
    anomaly_threshold = request.parameters.get("anomaly_threshold", 2.0)

    for inp in request.inputs:
        if inp.get("protocol") == "grpc":
            # Fetch data from data_generator via gRPC
            upstream_uri = inp.get("uri")
            try:
                upstream_data = _fetch_data_from_upstream(upstream_uri)
                records = list(upstream_data.records)
            except grpc.RpcError as e:
                logger.error(f"Failed to fetch data: {e}")
                return ExecuteResponse(status="failed", error=f"Failed to fetch data: {e}")

    if not records:
        return ExecuteResponse(status="failed", error="No input data available")

    logger.info(f"AnalyzeData: {len(records)} records, threshold={anomaly_threshold}")

    # Analyze data with verbose output
    result = _analyze_data(records, anomaly_threshold, verbose=True)

    # Cache for downstream gRPC calls
    with _cache_lock:
        _cached_response = result

    if not result.success:
        return ExecuteResponse(status="failed", error=result.message)

    # Return gRPC endpoint for downstream to fetch analyzed data
    grpc_host = os.environ.get("GRPC_HOST", "data-analyzer")
    grpc_port = os.environ.get("GRPC_PORT", "50051")

    return ExecuteResponse(
        status="complete",
        output=DataReference(
            protocol="grpc",
            uri=f"{grpc_host}:{grpc_port}",
            format="AnalyzeData",
        ),
    )


if __name__ == "__main__":
    # Start gRPC server in background
    grpc_server = start_grpc_server()

    # Run HTTP control interface (blocking)
    run(sys.modules[__name__])
