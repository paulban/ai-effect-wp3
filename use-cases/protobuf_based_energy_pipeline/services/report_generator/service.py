"""Report Generator service - HTTP control + gRPC data interface.

Receives analyzed data from upstream (data_analyzer) via gRPC.
Generates report and caches results for downstream gRPC calls.

Data flow:
  data_analyzer ──(gRPC)──> report_generator ──(gRPC)──> [end]
"""

import logging
import os
import sys
import threading
from collections import defaultdict
from concurrent import futures

import grpc

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

# Import generated protobuf classes
import data_analyzer_pb2
import data_analyzer_pb2_grpc
import report_generator_pb2
import report_generator_pb2_grpc

logger = logging.getLogger(__name__)

# Cache generated report for downstream gRPC calls
_cached_response: report_generator_pb2.GenerateReportResponse | None = None
_cache_lock = threading.Lock()


class ReportGeneratorServicer(report_generator_pb2_grpc.ReportGeneratorServiceServicer):
    """gRPC servicer for report generation requests."""

    def GenerateReport(self, request, context):
        """Return cached report to downstream services."""
        with _cache_lock:
            if _cached_response is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details("No report available. Service not yet triggered.")
                return report_generator_pb2.GenerateReportResponse(
                    success=False,
                    message="No report available",
                )
            return _cached_response


def _generate_report(
    analyzed_records: list,
    total_records: int,
    anomalies_detected: int,
    average_efficiency: float,
    verbose: bool = False,
) -> report_generator_pb2.GenerateReportResponse:
    """Generate report."""
    logger.info(f"Generating report: {total_records} records, {anomalies_detected} anomalies")

    response = report_generator_pb2.GenerateReportResponse()

    # Executive Summary
    summary_section = report_generator_pb2.ReportSection()
    summary_section.title = "Executive Summary"
    summary_section.content.extend([
        f"Total Records Analyzed: {total_records}",
        f"Anomalies Detected: {anomalies_detected}",
        f"Average Efficiency Score: {average_efficiency:.2%}",
        f"Anomaly Rate: {(anomalies_detected / max(total_records, 1)):.2%}",
    ])
    response.sections.append(summary_section)

    # Household Analysis
    household_stats = defaultdict(lambda: {
        'count': 0, 'total_consumption': 0, 'anomalies': 0, 'efficiency_sum': 0
    })
    anomaly_types = defaultdict(int)
    efficiency_buckets = {'high': 0, 'medium': 0, 'low': 0}

    for record in analyzed_records:
        household_id = record.original.household_id
        stats = household_stats[household_id]
        stats['count'] += 1
        stats['total_consumption'] += record.original.power_consumption
        stats['efficiency_sum'] += record.efficiency_score

        if record.is_anomaly:
            stats['anomalies'] += 1
            if "Voltage" in record.anomaly_reason:
                anomaly_types["Voltage Issues"] += 1
            elif "Power consumption" in record.anomaly_reason:
                anomaly_types["Consumption Deviation"] += 1
            else:
                anomaly_types["Other"] += 1

        if record.efficiency_score >= 0.9:
            efficiency_buckets['high'] += 1
        elif record.efficiency_score >= 0.7:
            efficiency_buckets['medium'] += 1
        else:
            efficiency_buckets['low'] += 1

    household_section = report_generator_pb2.ReportSection()
    household_section.title = "Household Analysis"
    for household_id, stats in household_stats.items():
        avg_consumption = stats['total_consumption'] / max(stats['count'], 1)
        avg_efficiency = stats['efficiency_sum'] / max(stats['count'], 1)
        household_section.content.append(
            f"{household_id}: {stats['count']} records, "
            f"Avg Consumption: {avg_consumption:.1f}W, "
            f"Anomalies: {stats['anomalies']}, "
            f"Avg Efficiency: {avg_efficiency:.2%}"
        )
    response.sections.append(household_section)

    # Anomaly Breakdown
    if anomaly_types:
        anomaly_section = report_generator_pb2.ReportSection()
        anomaly_section.title = "Anomaly Breakdown"
        for anomaly_type, count in anomaly_types.items():
            percentage = (count / max(anomalies_detected, 1)) * 100
            anomaly_section.content.append(f"{anomaly_type}: {count} ({percentage:.1f}%)")
        response.sections.append(anomaly_section)

    # Efficiency Distribution
    efficiency_section = report_generator_pb2.ReportSection()
    efficiency_section.title = "Efficiency Distribution"
    total = max(total_records, 1)
    efficiency_section.content.extend([
        f"High Efficiency (>=90%): {efficiency_buckets['high']} ({efficiency_buckets['high']/total:.1%})",
        f"Medium Efficiency (70-90%): {efficiency_buckets['medium']} ({efficiency_buckets['medium']/total:.1%})",
        f"Low Efficiency (<70%): {efficiency_buckets['low']} ({efficiency_buckets['low']/total:.1%})",
    ])
    response.sections.append(efficiency_section)

    # Recommendations
    recommendations_section = report_generator_pb2.ReportSection()
    recommendations_section.title = "Recommendations"
    recommendations = []
    if anomalies_detected > total_records * 0.1:
        recommendations.append("High anomaly rate detected. Investigate system stability.")
    if average_efficiency < 0.8:
        recommendations.append("Overall efficiency below 80%. Consider maintenance or upgrades.")
    if anomaly_types.get("Voltage Issues", 0) > 0:
        recommendations.append("Voltage irregularities detected. Check power supply stability.")
    if not recommendations:
        recommendations.append("System operating within normal parameters.")
    recommendations_section.content.extend(recommendations)
    response.sections.append(recommendations_section)

    response.summary = (
        f"Report generated for {total_records} records. "
        f"Key findings: {anomalies_detected} anomalies "
        f"({(anomalies_detected / max(total_records, 1)):.1%}), "
        f"average efficiency {average_efficiency:.1%}."
    )
    response.success = True
    response.message = "Report generated successfully"

    if verbose:
        logger.info("")
        logger.info("=" * 60)
        logger.info("REPORT GENERATOR - Final Report")
        logger.info("=" * 60)
        for section in response.sections:
            logger.info("")
            logger.info(f"### {section.title}")
            logger.info("-" * 40)
            for line in section.content:
                logger.info(f"  {line}")
        logger.info("")
        logger.info("=" * 60)
        logger.info(f"SUMMARY: {response.summary}")
        logger.info("=" * 60)
    else:
        logger.info(f"Report generated with {len(response.sections)} sections")

    return response


def _fetch_data_from_upstream(grpc_uri: str) -> data_analyzer_pb2.AnalyzeDataResponse:
    """Fetch analyzed data from data_analyzer via gRPC."""
    logger.info(f"Calling AnalyzeData on {grpc_uri}")

    channel = grpc.insecure_channel(grpc_uri)
    stub = data_analyzer_pb2_grpc.DataAnalyzerServiceStub(channel)

    try:
        # Call the actual method directly
        response = stub.AnalyzeData(data_analyzer_pb2.AnalyzeDataRequest())
        logger.info(f"Got {response.total_records} analyzed records from upstream")
        return response
    finally:
        channel.close()


def start_grpc_server():
    """Start gRPC server in background thread."""
    grpc_port = os.environ.get("GRPC_PORT", "50051")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    report_generator_pb2_grpc.add_ReportGeneratorServiceServicer_to_server(
        ReportGeneratorServicer(), server
    )
    server.add_insecure_port(f"[::]:{grpc_port}")
    server.start()
    logger.info(f"gRPC server started on port {grpc_port}")
    return server


# --- HTTP Control Interface ---

def execute_GenerateReport(request: ExecuteRequest) -> ExecuteResponse:
    """HTTP handler: Fetch analyzed data via gRPC, generate report, cache for downstream."""
    global _cached_response

    analyzed_data = None

    for inp in request.inputs:
        if inp.get("protocol") == "grpc":
            # Fetch data from data_analyzer via gRPC
            upstream_uri = inp.get("uri")
            try:
                analyzed_data = _fetch_data_from_upstream(upstream_uri)
            except grpc.RpcError as e:
                logger.error(f"Failed to fetch data: {e}")
                return ExecuteResponse(status="failed", error=f"Failed to fetch data: {e}")

    if not analyzed_data:
        return ExecuteResponse(status="failed", error="No input data available")

    logger.info(f"GenerateReport: {analyzed_data.total_records} records")

    # Generate report with verbose output
    result = _generate_report(
        list(analyzed_data.analyzed_records),
        analyzed_data.total_records,
        analyzed_data.anomalies_detected,
        analyzed_data.average_efficiency,
        verbose=True,
    )

    # Cache for downstream gRPC calls
    with _cache_lock:
        _cached_response = result

    if not result.success:
        return ExecuteResponse(status="failed", error=result.message)

    # Return gRPC endpoint for downstream to fetch report
    grpc_host = os.environ.get("GRPC_HOST", "report-generator")
    grpc_port = os.environ.get("GRPC_PORT", "50051")

    return ExecuteResponse(
        status="complete",
        output=DataReference(
            protocol="grpc",
            uri=f"{grpc_host}:{grpc_port}",
            format="GenerateReport",
        ),
    )


if __name__ == "__main__":
    # Start gRPC server in background
    grpc_server = start_grpc_server()

    # Run HTTP control interface (blocking)
    run(sys.modules[__name__])
