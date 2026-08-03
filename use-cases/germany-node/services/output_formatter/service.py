"""Output formatter service for Germany node VILLASnode pipeline.

Validates chronics CSV files and produces a summary.
"""

import json
import logging
import os
import shutil

from common import DataReference, ExecuteResponse, run_sequential

logger = logging.getLogger(__name__)

SHARED_DIR = os.environ.get("SHARED_DIR", "/shared")

EXPECTED_FILES = [
    "load_p.csv",
    "load_q.csv",
    "prod_p.csv",
    "prod_q.csv",
    "prod_v.csv",
]


def _validate_csv(filepath: str) -> dict:
    """Validate a chronics CSV file and return info."""
    result = {
        "file": os.path.basename(filepath),
        "exists": os.path.exists(filepath),
        "rows": 0,
        "columns": 0,
        "valid": False,
        "error": None,
    }

    if not result["exists"]:
        result["error"] = "File not found"
        return result

    with open(filepath) as f:
        lines = f.readlines()

    if not lines:
        result["error"] = "File is empty"
        return result

    # Check header (first line)
    header = lines[0].strip()
    if ";" not in header and len(header) > 0:
        # Single-column file is still valid (e.g. prod files with no generators)
        result["columns"] = 1
    else:
        result["columns"] = len(header.split(";"))

    result["rows"] = len(lines) - 1  # exclude header
    result["valid"] = True
    return result


def execute_FormatOutput(request) -> ExecuteResponse:
    """Validate chronics output and produce summary."""
    if not request.inputs:
        return ExecuteResponse(
            status="failed", error="No inputs provided"
        )

    input_uri = request.inputs[0].get("uri", "")
    if not input_uri:
        return ExecuteResponse(
            status="failed", error="No input URI provided"
        )

    # Determine output directory
    workflow_id = request.workflow_id
    final_dir = os.path.join(SHARED_DIR, workflow_id, "final_output")
    os.makedirs(final_dir, exist_ok=True)

    # Validate and copy each expected file
    validation_results = []
    files_validated = 0

    for filename in EXPECTED_FILES:
        src_path = os.path.join(input_uri, filename)
        result = _validate_csv(src_path)
        validation_results.append(result)

        if result["valid"]:
            dst_path = os.path.join(final_dir, filename)
            shutil.copy2(src_path, dst_path)
            files_validated += 1
            logger.info(
                f"Validated {filename}: {result['rows']} rows, "
                f"{result['columns']} columns"
            )
        else:
            logger.warning(f"Validation failed for {filename}: {result['error']}")

    # Write summary
    summary = {
        "workflow_id": workflow_id,
        "total_files": len(EXPECTED_FILES),
        "files_validated": files_validated,
        "files": validation_results,
    }
    summary_path = os.path.join(final_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Wrote summary to {summary_path}")

    if files_validated == 0:
        return ExecuteResponse(
            status="failed",
            error="No valid chronics files found",
        )

    return ExecuteResponse(
        status="complete",
        output=DataReference(
            protocol="file", uri=final_dir, format="csv"
        ),
    )


if __name__ == "__main__":
    run_sequential(__import__(__name__))
