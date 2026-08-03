"""TEF operation handlers for AI-Effect orchestrator.

This module provides all TEF service operation handlers.
Data is exchanged via HTTP URL references.

All configuration comes through inputs:
- Start nodes: Receive inline JSON input with config
- Middle nodes: Receive HTTP URL reference to upstream data

Handlers (names match proto RPC definitions):
    - ExecuteQuery: Execute SQL query via Data Provision (start node with default query)
    - GetSchema: Get database schema via Data Provision
    - ApplyFunction: Apply feature engineering via Knowledge Store
    - TrainModel: Train DoppelGANger model via Synthetic Data (async)
    - GenerateData: Generate synthetic data via Synthetic Data

Usage:
    from common import data_provision_handlers, run

    if __name__ == "__main__":
        run(data_provision_handlers, "Data Provision Adapter")
"""

import base64
import json
import logging
import os
import threading
import time

import httpx

from .control_interface import DataReference, ExecuteRequest, ExecuteResponse, get_data_url
from .task_manager import get_task_manager

logger = logging.getLogger(__name__)

# Service URLs (configured via environment)
DATA_PROVISION_URL = os.environ.get("DATA_PROVISION_URL", "http://data-provision:600")
KNOWLEDGE_STORE_URL = os.environ.get("KNOWLEDGE_STORE_URL", "http://knowledge-store:8000")
SYNTHETIC_DATA_URL = os.environ.get("SYNTHETIC_DATA_URL", "http://synthetic-data:600")


def _decode_inline_input(input_ref: dict) -> dict:
    """Decode inline JSON input to dict."""
    if input_ref.get("protocol") == "inline":
        try:
            return json.loads(base64.b64decode(input_ref.get("uri", "")).decode())
        except Exception:
            return {}
    return {}


def fetch_http_data(uri: str, timeout: float = 60.0) -> str:
    """Fetch data from HTTP URL.

    Args:
        uri: HTTP URL to fetch
        timeout: Request timeout in seconds

    Returns:
        Response text content

    Raises:
        httpx.HTTPError: On request failure
    """
    logger.info(f"Fetching data from {uri}")
    resp = httpx.get(uri, timeout=timeout)
    resp.raise_for_status()
    return resp.text


# =============================================================================
# Data Provision Handlers
# =============================================================================


def execute_ExecuteQuery(request: ExecuteRequest) -> ExecuteResponse:
    """Load data from a file and serve via HTTP URL.

    Input (inline JSON):
        file_path: Path to CSV file to load
        max_rows: Maximum rows to load (default: 1000)
        rename_columns: Dict of column renames (default: {})

    Returns:
        DataReference with HTTP URL to CSV data.
    """
    if not request.inputs:
        return ExecuteResponse(status="failed", error="No input provided")

    params = _decode_inline_input(request.inputs[0])

    file_path = params.get("file_path")
    max_rows = params.get("max_rows", 1000)
    rename_columns = params.get("rename_columns", {})

    if not file_path:
        return ExecuteResponse(status="failed", error="No file_path provided")

    try:
        logger.info(f"Loading data from {file_path}")

        with open(file_path, "r") as f:
            lines = f.readlines()

        # Apply max_rows (including header)
        if len(lines) > max_rows + 1:
            lines = lines[:max_rows + 1]

        # Apply column renames to header
        if rename_columns:
            header = lines[0].strip().split(",")
            header = [rename_columns.get(col, col) for col in header]
            lines[0] = ",".join(header) + "\n"

        csv_data = "".join(lines)

        # Store for HTTP serving
        get_task_manager().store_data(request.task_id, csv_data, "csv")

        logger.info(f"Loaded {len(lines) - 1} rows")

        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="http",
                uri=get_data_url(request.task_id),
                format="csv",
            ),
        )

    except Exception as e:
        logger.error(f"LoadData failed: {e}")
        return ExecuteResponse(status="failed", error=str(e))


def execute_GetSchema(request: ExecuteRequest) -> ExecuteResponse:
    """Execute SQL query against ClickHouse via Data Provision.

    Input (inline JSON):
        sql_query: SQL query to execute
        output_format: Output format json or csv (default: "json")

    Returns:
        DataReference with HTTP URL to query result.
    """
    if not request.inputs:
        return ExecuteResponse(status="failed", error="No input provided")

    params = _decode_inline_input(request.inputs[0])

    sql_query = params.get("sql_query")
    output_format = params.get("output_format", "json")

    if not sql_query:
        return ExecuteResponse(status="failed", error="No sql_query provided")

    try:
        logger.info(f"Executing query: {sql_query[:100]}...")

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{DATA_PROVISION_URL}/query",
                params={"format": output_format},
                json={"sql_query": sql_query},
            )

        if response.status_code != 200:
            return ExecuteResponse(status="failed", error=f"Query error: {response.text}")

        # Store for HTTP serving
        get_task_manager().store_data(request.task_id, response.content, output_format)

        logger.info("Query complete")

        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="http",
                uri=get_data_url(request.task_id),
                format=output_format,
            ),
        )

    except Exception as e:
        logger.error(f"QueryDatabase failed: {e}")
        return ExecuteResponse(status="failed", error=str(e))


# =============================================================================
# Knowledge Store Handlers
# =============================================================================


def execute_ApplyFunction(request: ExecuteRequest) -> ExecuteResponse:
    """Apply feature engineering function via Knowledge Store.

    Input:
        inputs[0]: HTTP URL to CSV data

    Uses default feature function (DatetimeFeatures with hour).

    Returns:
        DataReference with HTTP URL to CSV result.
    """
    # Default feature configuration
    function_name = "DatetimeFeatures"
    function_kwargs = {"season": "hour"}

    if not request.inputs:
        return ExecuteResponse(status="failed", error="No input data provided")

    input_ref = request.inputs[0]

    try:
        # Fetch input data from HTTP URL
        uri = input_ref.get("uri", "")
        protocol = input_ref.get("protocol", "")

        if protocol not in ("http", "https"):
            return ExecuteResponse(
                status="failed",
                error=f"Expected HTTP protocol, got: {protocol}"
            )

        csv_data = fetch_http_data(uri)

        # Call Knowledge Store API
        logger.info(f"Calling Knowledge Store: {function_name}")

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{KNOWLEDGE_STORE_URL}/functions/apply",
                params={
                    "feature_function_name": function_name,
                    "feature_function_kwargs_str": json.dumps(function_kwargs),
                },
                files={"file": ("data.csv", csv_data, "text/csv")},
            )

        if response.status_code != 200:
            return ExecuteResponse(status="failed", error=f"API error: {response.text}")

        # Convert JSON response to CSV
        json_data = response.json()
        if json_data:
            headers = list(json_data[0].keys())
            csv_lines = [",".join(headers)]
            for row in json_data:
                csv_lines.append(",".join(str(row.get(h, "")) for h in headers))
            csv_output = "\n".join(csv_lines)
        else:
            csv_output = ""

        # Store for HTTP serving
        get_task_manager().store_data(request.task_id, csv_output, "csv")

        logger.info(f"Feature engineering complete: {function_name}")

        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="http",
                uri=get_data_url(request.task_id),
                format="csv",
            ),
        )

    except Exception as e:
        logger.error(f"ApplyFeatures failed: {e}")
        return ExecuteResponse(status="failed", error=str(e))


# =============================================================================
# Synthetic Data Handlers
# =============================================================================


def _poll_training(task_id: str, username: str, model_name: str) -> None:
    """Background thread to poll training status."""
    tm = get_task_manager()
    max_wait = 600  # 10 minutes
    poll_interval = 5
    elapsed = 0

    while elapsed < max_wait:
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    f"{SYNTHETIC_DATA_URL}/training_info",
                    params={"username": username, "model_name": model_name},
                )

            if response.status_code == 200:
                info = response.json()

                # Check for completion: trained flag, status field, or epoch count
                epoch = info.get("epoch", 0)
                total_epochs = info.get("total_epochs", 0)
                is_complete = (
                    info.get("trained", False)
                    or info.get("status") == "completed"
                    or (total_epochs > 0 and epoch >= total_epochs)
                )

                if is_complete:
                    # Return model info as inline (small metadata, not data)
                    model_info = json.dumps({
                        "username": username,
                        "model_name": model_name,
                    })
                    output = DataReference(
                        protocol="inline",
                        uri=base64.b64encode(model_info.encode()).decode(),
                        format="json",
                    )
                    tm.complete(task_id, output)
                    logger.info(f"Training complete: {model_name}")
                    return

                elif info.get("status") == "failed":
                    tm.fail(task_id, info.get("error", "Training failed"))
                    return

                tm.update_progress(task_id, info.get("progress", 0))

        except Exception as e:
            logger.warning(f"Poll error: {e}")

        time.sleep(poll_interval)
        elapsed += poll_interval

    tm.fail(task_id, "Training timeout")


def execute_TrainModel(request: ExecuteRequest) -> ExecuteResponse:
    """Train a DoppelGANger model via Synthetic Data (async).

    Input:
        inputs[0]: HTTP URL to CSV training data

    Uses default training configuration.

    Returns:
        status=running with task_id for polling.
    """
    # Default training configuration
    username = "demo_user"
    model_name = f"model_{request.task_id}"
    index_col = "timestamp"
    epochs = 5
    batch_size = 100

    if not request.inputs:
        return ExecuteResponse(status="failed", error="No input data provided")

    input_ref = request.inputs[0]

    try:
        # Fetch input data from HTTP URL
        uri = input_ref.get("uri", "")
        protocol = input_ref.get("protocol", "")

        if protocol not in ("http", "https"):
            return ExecuteResponse(
                status="failed",
                error=f"Expected HTTP protocol, got: {protocol}"
            )

        csv_data = fetch_http_data(uri)

        # Start training
        logger.info(f"Starting training: {model_name}")

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{SYNTHETIC_DATA_URL}/train",
                params={
                    "model_name": model_name,
                    "username": username,
                    "index_col": index_col,
                    "max_epochs": epochs,
                    "batch_size": batch_size,
                    "overwrite": "true",
                },
                files={"uploaded_file": ("data.csv", csv_data, "text/csv")},
            )

        if response.status_code not in (200, 202):
            return ExecuteResponse(status="failed", error=f"Train error: {response.text}")

        # Register task and start polling
        get_task_manager().register(request.task_id, status="running")

        thread = threading.Thread(
            target=_poll_training,
            args=(request.task_id, username, model_name),
            daemon=True,
        )
        thread.start()

        return ExecuteResponse(status="running", task_id=request.task_id)

    except Exception as e:
        logger.error(f"TrainModel failed: {e}")
        return ExecuteResponse(status="failed", error=str(e))


def execute_GenerateData(request: ExecuteRequest) -> ExecuteResponse:
    """Generate synthetic data using trained model.

    Input:
        inputs[0]: Inline model info (from TrainModel output)

    Uses default generation configuration.

    Returns:
        DataReference with HTTP URL to generated data.
    """
    # Default generation configuration
    num_examples = 10
    output_format = "csv"

    if not request.inputs:
        return ExecuteResponse(status="failed", error="No model info provided")

    input_ref = request.inputs[0]

    try:
        # Parse model info (inline from TrainModel)
        if input_ref.get("protocol") != "inline":
            return ExecuteResponse(
                status="failed",
                error="Expected inline model info from TrainModel"
            )

        model_info = json.loads(
            base64.b64decode(input_ref.get("uri", "")).decode()
        )
        username = model_info.get("username", "demo_user")
        model_name = model_info.get("model_name")

        if not model_name:
            return ExecuteResponse(status="failed", error="No model_name in input")

        # Generate data
        logger.info(f"Generating {num_examples} samples from {model_name}")

        with httpx.Client(timeout=120.0) as client:
            response = client.get(
                f"{SYNTHETIC_DATA_URL}/generate",
                params={
                    "model_name": model_name,
                    "username": username,
                    "number_of_examples": num_examples,
                    "output_format": output_format,
                },
            )

        if response.status_code != 200:
            return ExecuteResponse(status="failed", error=f"Generate error: {response.text}")

        # Store for HTTP serving
        get_task_manager().store_data(request.task_id, response.content, output_format)

        logger.info(f"Generated {num_examples} samples")

        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="http",
                uri=get_data_url(request.task_id),
                format=output_format,
            ),
        )

    except Exception as e:
        logger.error(f"GenerateData failed: {e}")
        return ExecuteResponse(status="failed", error=str(e))


# =============================================================================
# Handler exports for each service
# =============================================================================

data_provision_handlers = {
    "ExecuteQuery": execute_ExecuteQuery,
    "GetSchema": execute_GetSchema,
}

knowledge_store_handlers = {
    "ApplyFunction": execute_ApplyFunction,
}

synthetic_data_handlers = {
    "TrainModel": execute_TrainModel,
    "GenerateData": execute_GenerateData,
}
