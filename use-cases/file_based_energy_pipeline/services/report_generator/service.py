"""Report Generator service - generates summary reports from analyzed data."""

import logging
import sys
from pathlib import Path

import pandas as pd

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)


def _get_input_file_path(inputs: list[dict]) -> str:
    """Get file path from input DataReference."""
    for inp in inputs:
        if inp.get("protocol") == "file" and inp.get("format") == "csv":
            return inp.get("uri", "")
    return ""


def execute_GenerateReport(request: ExecuteRequest) -> ExecuteResponse:
    """Generate summary report from analyzed data.

    Input: File path to analyzed energy CSV.
    Parameters: report_format (default csv)
    Output: File path to report CSV.
    """
    # Get input file path
    input_path = _get_input_file_path(request.inputs)
    if not input_path:
        return ExecuteResponse(status="failed", error="No input file provided")

    # Get format from parameters (not used currently, always outputs CSV)
    report_format = request.parameters.get("report_format", "csv")

    logger.info(f"GenerateReport: input={input_path}, format={report_format}")

    # Read analyzed data
    input_file = Path(input_path)
    if not input_file.exists():
        return ExecuteResponse(status="failed", error=f"Input file not found: {input_path}")

    df = pd.read_csv(input_file)

    # Generate summary statistics
    total_records = len(df)
    anomaly_count = df["anomaly_detected"].sum()
    avg_efficiency = df["efficiency"].mean()
    max_power = df["power"].max()
    min_power = df["power"].min()

    # Create summary report
    report_data = {
        "metric": [
            "Total Records",
            "Anomalies Detected",
            "Average Efficiency",
            "Max Power (W)",
            "Min Power (W)",
            "Anomaly Rate (%)",
        ],
        "value": [
            total_records,
            int(anomaly_count),
            round(avg_efficiency, 3),
            max_power,
            min_power,
            round((anomaly_count / total_records) * 100, 2),
        ],
    }

    # Save report
    output_path = Path("data/energy_report.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    report_df = pd.DataFrame(report_data)
    report_df.to_csv(output_path, index=False)

    logger.info(
        f"Generated report: {total_records} records, {int(anomaly_count)} anomalies"
    )

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
