"""Synthetic power grid service for the AI-Effect orchestrator.

Exposes one operation, ``ConfigureAndSynthesize``. It runs powergrid_synth's
complete synthesis pipeline (``synthesize(mode="synthetic")``) and exports the
grid with the package's own pandapower JSON export. The result is a single
pandapower JSON file that ``pp.from_json()`` opens directly; the synthesis
metadata travels inside it at ``net["synthesis_metadata"]``.

Synthesis takes minutes, so it runs as a batch job (``common.batch_jobs``):
``/control/execute`` returns a job id immediately, the number of simultaneous
jobs is capped, and the finished grid is stored in the artifact store and
served at ``/control/data/{job_id}``. ``/control/output/{job_id}`` returns an
HTTP reference to that URL. A copy is also written to
``./results/<job_id>.json``, bind-mounted to the host.

Each method named ``execute_<MethodName>`` is exposed as an operation, so
helpers here are kept private with a leading underscore.
"""

import base64
import json
import logging
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandapower as pp

from powergrid_synth.transmission.synthesize import synthesize

from common.batch_jobs import BatchJobRunner, build_job_id, build_runner
from handler import ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)

# Default grid configuration
DEFAULT_LEVEL_SPECS = [
    {"n": 20, "avg_k": 3.0, "diam": 6, "dist_type": "dgln", "max_k": 15},
    {"n": 60, "avg_k": 2.2, "diam": 10, "dist_type": "dgln", "max_k": 10},
    {"n": 100, "avg_k": 2.0, "diam": 15, "dist_type": "dgln", "max_k": 10},
]

DEFAULT_CONNECTION_SPECS = {
    "(0, 1)": {"type": "k-stars", "c": 0.174, "gamma": 4.15},
    "(1, 2)": {"type": "k-stars", "c": 0.150, "gamma": 4.15},
}

DEFAULT_SEED = 42
DEFAULT_LOADING_LEVEL = "M"
DEFAULT_REF_SYS_ID = 1
DEFAULT_SYNTHETIC_GRID_NAME = "synthetic-grid-v0"

# Nominal voltage (kV) per hierarchy level, passed to synthesize() as base_kv_map.
DEFAULT_VOLTAGE_BY_LEVEL: dict[int, float] = {
    0: 220.0,
    1: 110.0,
    2: 20.0,
    3: 10.0,
}

# Directory bind-mounted to the host; see docker-compose.yml.
RESULTS_DIRECTORY = Path("./results")

# Logical format recorded on the stored artifact and carried in the
# DataReference's metadata.
GRID_DATA_FORMAT = "GridData"

# Built on first use so importing this module reads no environment.
_job_runner: BatchJobRunner | None = None


def _get_job_runner() -> BatchJobRunner:
    """Return the process-wide job runner, building it on first use.

    Configured from the environment: ARTIFACT_DIR, SELF_URL and
    MAX_CONCURRENT_JOBS (see docker-compose.yml).
    """
    global _job_runner
    if _job_runner is None:
        _job_runner = build_runner()
    return _job_runner


def _decode_inline_input(input_ref: dict) -> dict:
    """Decode inline JSON input to dict.

    Raises:
        ValueError: If the payload is not base64-encoded JSON.
    """
    try:
        return json.loads(base64.b64decode(input_ref.get("uri", "")).decode())
    except (ValueError, UnicodeDecodeError) as decode_error:
        raise ValueError(f"Inline input is not base64-encoded JSON: {decode_error}")


def _fetch_http_data(uri: str, timeout: float = 60.0) -> str:
    """Fetch data from HTTP URL.

    Args:
        uri: HTTP URL to fetch
        timeout: Request timeout in seconds

    Returns:
        Response text content
    """
    import httpx

    logger.info(f"Fetching data from {uri}")
    resp = httpx.get(uri, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _read_parameters(request: ExecuteRequest) -> dict:
    """Read the synthesis parameters from the request's first input.

    No input means all defaults.

    Raises:
        ValueError: If the input's protocol is unsupported or its payload
            cannot be decoded.
    """
    if not request.inputs:
        return {}

    input_ref = request.inputs[0]
    protocol = input_ref.get("protocol", "")
    if protocol == "inline":
        return _decode_inline_input(input_ref)
    if protocol in ("http", "https"):
        return json.loads(_fetch_http_data(input_ref["uri"]))
    raise ValueError(
        f"Unsupported protocol: {protocol}. Expected 'inline', 'http' or 'https'."
    )


def _parse_connection_specs(raw: dict) -> dict:
    """Parse connection specs from JSON-safe format to tuple-keyed dict.

    Accepts either string tuple keys like "(0, 1)" or list keys like [0, 1].
    """
    parsed = {}
    for key, val in raw.items():
        if isinstance(key, str) and key.startswith("("):
            # Parse "(0, 1)" format
            nums = key.strip("()").split(",")
            k = (int(nums[0].strip()), int(nums[1].strip()))
        elif isinstance(key, (list, tuple)):
            k = tuple(key)
        else:
            # Try "0-1" or "0_1" format
            parts = key.replace("_", "-").split("-")
            k = (int(parts[0]), int(parts[1]))
        parsed[k] = val
    return parsed


def _configure_grid(params: dict) -> dict:
    """Fill in defaults and normalise the synthesis parameters.

    Input (all optional - defaults used if not provided):
        level_specs: List of level specifications, each with:
            n: Number of nodes
            avg_k: Average degree
            diam: Target diameter
            dist_type: Distribution type ('dgln', 'dpl', 'poisson')
            max_k: Maximum degree (optional)
        connection_specs: Dict of inter-level connections, keyed as "(i, j)":
            type: Connection type ('k-stars')
            c: Proportionality constant
            gamma: Exponent parameter
        seed: Random seed (default: 42)
        loading_level: Grid loading level 'L', 'M', 'H' (default: 'M')
        ref_sys_id: Reference system ID (default: 1)
        synthetic_grid_name: Name given to the synthesized network
            (default: 'synthetic-grid-v0')

    Returns:
        The configuration, with connection specs keyed by (i, j) tuples as
        synthesize() expects.

    Raises:
        ValueError: If the connection specs cannot be parsed.
    """
    try:
        connection_specs = _parse_connection_specs(
            params.get("connection_specs", DEFAULT_CONNECTION_SPECS)
        )
    except (ValueError, IndexError) as parse_error:
        raise ValueError(f"Invalid connection_specs: {parse_error}")

    return {
        "synthetic_grid_name": params.get("synthetic_grid_name", DEFAULT_SYNTHETIC_GRID_NAME),
        "seed": params.get("seed", DEFAULT_SEED),
        "loading_level": params.get("loading_level", DEFAULT_LOADING_LEVEL),
        "ref_sys_id": params.get("ref_sys_id", DEFAULT_REF_SYS_ID),
        "level_specs": params.get("level_specs", DEFAULT_LEVEL_SPECS),
        "connection_specs": connection_specs,
    }


def _build_synthesis_metadata(config: dict, net) -> dict[str, Any]:
    """Collect what a consumer needs to know about how the grid was made.

    Stored on the pandapower network under ``net["synthesis_metadata"]``, so it
    travels inside the one file ``pp.from_json()`` opens. Values must be plain
    JSON: string keys, no numpy types.
    """
    try:
        powergrid_synth_version = version("powergrid_synth")
    except PackageNotFoundError:
        powergrid_synth_version = "unknown"

    return {
        "synthetic_grid_name": config["synthetic_grid_name"],
        "seed": config["seed"],
        "loading_level": config["loading_level"],
        "ref_sys_id": config["ref_sys_id"],
        "level_specs": config["level_specs"],
        "connection_specs": {str(k): v for k, v in config["connection_specs"].items()},
        "voltage_by_level_kv": {str(k): v for k, v in DEFAULT_VOLTAGE_BY_LEVEL.items()},
        "buses": len(net.bus),
        "lines": len(net.line),
        "transformers": len(net.trafo),
        "powergrid_synth_version": powergrid_synth_version,
        "pandapower_version": pp.__version__,
    }


def _check_power_flow(net) -> bool:
    """Run an AC power flow once as a sanity check.

    The outcome is recorded rather than failing the task, because an AC power
    flow on a synthetic grid may legitimately not converge.
    """
    try:
        pp.runpp(net, numba=False)
        return bool(net.converged)
    except Exception as power_flow_error:
        logger.warning(f"Power flow on synthesized grid failed: {power_flow_error}")
        return False


def _synthesize_grid(config: dict) -> str:
    """Run powergrid_synth's synthesis pipeline and export it as pandapower JSON.

    ``synthesize()`` generates the topology, allocates bus types, capacities,
    loads, dispatch and line parameters, and exports through the package's
    ``GridExporter`` (``nx_to_pandapower`` + ``pp.to_json``). The export lands
    in a temporary directory, together with a perturbation report that a
    synthetic-mode run has no use for; the network is read back from there so
    the metadata can be attached.

    Returns:
        The pandapower network as a JSON string, with the synthesis metadata at
        ``net["synthesis_metadata"]``.
    """
    # The exporter draws generator reactive limits from numpy's global RNG,
    # so it is seeded too, keeping the output reproducible for a given seed.
    np.random.seed(config["seed"])

    with tempfile.TemporaryDirectory() as export_directory:
        synthesize(
            mode="synthetic",
            level_specs=config["level_specs"],
            connection_specs=config["connection_specs"],
            seed=config["seed"],
            ref_sys_id=config["ref_sys_id"],
            loading_level=config["loading_level"],
            base_kv_map=DEFAULT_VOLTAGE_BY_LEVEL,
            output_dir=export_directory,
            output_name="grid",
            export_formats=["json"],
        )
        net = pp.from_json(str(Path(export_directory) / "grid.json"))

    net.name = config["synthetic_grid_name"]
    metadata = _build_synthesis_metadata(config, net)
    metadata["power_flow_converged"] = _check_power_flow(net)
    net["synthesis_metadata"] = metadata

    logger.info(
        f"Grid synthesis complete: {metadata['buses']} buses, "
        f"{metadata['lines']} lines, {metadata['transformers']} transformers, "
        f"power flow converged: {metadata['power_flow_converged']}"
    )
    return pp.to_json(net)


def execute_ConfigureAndSynthesize(request: ExecuteRequest) -> ExecuteResponse:
    """Start a grid synthesis and return immediately with its task id.

    Configuration is cheap and its failures are the caller's mistakes, so it
    runs synchronously: a bad request is rejected now rather than becoming a
    background task that fails a minute later. Synthesis takes minutes and
    runs in the background; callers poll ``/control/status/{job_id}``, read
    the reference from ``/control/output/{job_id}`` and fetch the grid from
    ``/control/data/{job_id}``.

    Returns:
        An ExecuteResponse with status ``running`` and the job id, or
        ``failed`` when the parameters are rejected or the service is
        already at capacity.
    """
    try:
        config = _configure_grid(_read_parameters(request))
    except Exception as e:
        logger.exception("ConfigureGrid failed")
        return ExecuteResponse(status="failed", task_id=request.task_id, error=str(e))

    job_id = build_job_id(request.workflow_id, request.task_id)

    def synthesize_grid(report_progress) -> dict:
        """Generate the grid, keep a copy on the host, hand it to the store."""
        report_progress(10)
        grid_json = _synthesize_grid(config)
        report_progress(90)

        RESULTS_DIRECTORY.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIRECTORY / f"{job_id}.json").write_text(grid_json, encoding="utf-8")
        return json.loads(grid_json)

    return _get_job_runner().submit(request, synthesize_grid, data_format=GRID_DATA_FORMAT)


if __name__ == "__main__":
    import sys
    run(sys.modules[__name__], artifact_store=_get_job_runner().artifact_store)
