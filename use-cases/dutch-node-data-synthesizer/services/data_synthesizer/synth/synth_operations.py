"""Synthetic power grid operation handlers for AI-Effect orchestrator.

This module provides operation handlers for the Chung-Lu-Chain power grid synthesizer.
Control execution uses AI-Effect HTTP control endpoints; the synthesized grid is
stored as a JSON artifact and served over HTTP.

Pipeline:
    ConfigureGrid -> SynthesizeGrid

Handlers:
        - ConfigureGrid: Accept synthesis parameters and return the derived
            synthesis configuration inline as JSON.
        - SynthesizeGrid: Consume that configuration, generate the grid, and
            return it as a JSON payload including a pandapower network.

Usage:
    from common import synth_handlers, run

    if __name__ == "__main__":
        run(synth_handlers, "Synthetic Power Grid Service")
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import networkx as nx
from networkx.readwrite import json_graph

from powergrid_synth.transmission.generator import PowerGridGenerator
from powergrid_synth.transmission.input_configurator import InputConfigurator
from powergrid_synth.transmission.bus_type_allocator import BusTypeAllocator
from powergrid_synth.transmission.capacity_allocator import CapacityAllocator
from powergrid_synth.transmission.load_allocator import LoadAllocator
from powergrid_synth.transmission.generation_dispatcher import GenerationDispatcher
from powergrid_synth.transmission.transmission import TransmissionLineAllocator

from common.batch_jobs import BatchJobRunner, build_runner
from common.concurrent import DataReference, ExecuteRequest, ExecuteResponse

logger = logging.getLogger(__name__)

# Logical format recorded on the stored artifact and carried in the
# DataReference handed back to the caller.
GRID_DATA_FORMAT = "GridData"

# Built on first use so importing this module reads no environment.
_job_runner: BatchJobRunner | None = None


def set_job_runner(runner: BatchJobRunner | None) -> None:
    """
    Install a job runner for this process.

    Args:
        runner: Runner to use, or None to clear it so the next call builds a
            fresh one. Tests inject a runner writing to a temporary directory.
    """
    global _job_runner
    _job_runner = runner


def get_job_runner() -> BatchJobRunner:
    """
    Return the process-wide job runner, building it on first use.

    Returns:
        The shared BatchJobRunner.
    """
    global _job_runner
    if _job_runner is None:
        _job_runner = build_runner()
    return _job_runner


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
DEFAULT_GRID2OP_ENV_NAME = "synthetic-grid-v0"


def _grid2op_env_name(_: int) -> str:
    """Return the fixed synthesized Grid2Op environment name."""
    return DEFAULT_GRID2OP_ENV_NAME


def _decode_inline_input(input_ref: dict) -> dict:
    """Decode inline JSON input to dict."""
    if input_ref.get("protocol") == "inline":
        try:
            return json.loads(base64.b64decode(input_ref.get("uri", "")).decode())
        except Exception:
            return {}
    return {}


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


def fetch_http_data(uri: str, timeout: float = 60.0) -> str:
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


_DEFAULT_VOLTAGE_BY_LEVEL: dict[int, float] = {
    0: 220.0,
    1: 110.0,
    2: 20.0,
    3: 10.0,
}


def _grid_to_pandapower_json(graph_data: dict[str, Any]) -> str:
    """Convert a node-link graph dict (powergrid_synth output) to a pandapower
    network and return it as a JSON string via pp.to_json().

    This is the authoritative networkx → pandapower conversion.  The result is
    stored in the grid artifact so a consumer can call pp.from_json() directly.
    """
    import pandapower as pp  # deferred: not available at module import time in tests

    net = pp.create_empty_network(sn_mva=100.0)
    bus_index_by_id: dict[str, int] = {}

    for node in graph_data.get("nodes", []):
        node_id = str(node.get("id", ""))
        v_nom = float(node.get("v_nom", node.get("voltage", 0.0)) or 0.0)
        if v_nom <= 0.0:
            level = int(node.get("voltage_level", 0))
            v_nom = _DEFAULT_VOLTAGE_BY_LEVEL.get(level, 110.0)
        bus_idx = pp.create_bus(net, vn_kv=max(v_nom, 0.1), name=node_id)
        bus_index_by_id[node_id] = int(bus_idx)

    if not bus_index_by_id:
        raise ValueError("Graph has no nodes – cannot create pandapower network")

    slack_node_id = next(iter(bus_index_by_id))
    created_loads = 0
    created_gens = 0
    total_load_p = 0.0

    for node in graph_data.get("nodes", []):
        node_id = str(node.get("id", ""))
        bus_idx = bus_index_by_id[node_id]
        bus_type = str(node.get("bus_type", "")).upper()
        load_p = float(node.get("p_load", 0.0) or 0.0)
        load_q = float(node.get("q_load", 0.0) or 0.0)
        gen_p = float(node.get("p_set", 0.0) or 0.0)
        gen_q = float(node.get("q_set", 0.0) or 0.0)

        if bus_type == "REF":
            slack_node_id = node_id

        if load_p or load_q:
            pp.create_load(net, bus_idx, p_mw=load_p, q_mvar=load_q, name=f"load_{node_id}")
            created_loads += 1
            total_load_p += load_p

        if gen_p or gen_q:
            pp.create_gen(
                net, bus_idx, p_mw=max(gen_p, 0.1), vm_pu=1.0,
                name=f"gen_{node_id}", type="thermal",
            )
            created_gens += 1

    bus_ids = list(bus_index_by_id)
    non_slack = [b for b in bus_ids if b != slack_node_id] or [slack_node_id]

    while len(net.load) < max(created_loads, 1):
        fb = non_slack[len(net.load) % len(non_slack)]
        pp.create_load(net, bus_index_by_id[fb], p_mw=10.0, q_mvar=2.0,
                       name=f"load_{fb}_{len(net.load)}")
        total_load_p += 10.0

    tgt_gen = max(created_gens, 1)
    while len(net.gen) < tgt_gen:
        fb = bus_ids[len(net.gen) % len(bus_ids)]
        pp.create_gen(
            net, bus_index_by_id[fb],
            p_mw=max(total_load_p / tgt_gen, 10.0),
            vm_pu=1.0, name=f"gen_{fb}_{len(net.gen)}", type="thermal",
        )

    pp.create_ext_grid(net, bus_index_by_id[slack_node_id], vm_pu=1.0,
                       name=f"slack_{slack_node_id}")

    for edge in graph_data.get("links", []):
        src = str(edge.get("source", ""))
        tgt = str(edge.get("target", ""))
        from_bus = bus_index_by_id.get(src)
        to_bus = bus_index_by_id.get(tgt)
        if from_bus is None or to_bus is None or from_bus == to_bus:
            continue
        r_ohm = abs(float(edge.get("r", 0.0) or 0.0))
        x_ohm = abs(float(edge.get("x", 0.0) or 0.0))
        b_val = abs(float(edge.get("b", 0.0) or 0.0))
        snom = float(edge.get("snom", edge.get("thermal_limit", 0.0)) or 0.0)
        pp.create_line_from_parameters(
            net, from_bus=from_bus, to_bus=to_bus, length_km=1.0,
            r_ohm_per_km=max(r_ohm, 1e-6),
            x_ohm_per_km=max(x_ohm, 1e-6),
            c_nf_per_km=b_val * 1e3,
            max_i_ka=snom if snom > 0 else 1.0,
            name=f"line-{src}-{tgt}",
        )

    pp_json = pp.to_json(net)
    if not isinstance(pp_json, str):
        # Older pandapower versions return None when no filename given; fall back.
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            pp.to_json(net, f.name)
            tmp = f.name
        pp_json = Path(tmp).read_text(encoding="utf-8")
        Path(tmp).unlink(missing_ok=True)
    return pp_json


# =============================================================================
# ConfigureGrid Handler
# =============================================================================


def execute_ConfigureGrid(request: ExecuteRequest) -> ExecuteResponse:
    """Configure grid parameters for the Chung-Lu-Chain synthesizer.

    Input (inline JSON, optional - defaults used if not provided):
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

    Returns:
        DataReference(protocol="inline", format="json") carrying the derived
        synthesis configuration.
    """
    # Parse input parameters or use defaults
    params = {}
    if request.inputs:
        params = _decode_inline_input(request.inputs[0])

    level_specs = params.get("level_specs", DEFAULT_LEVEL_SPECS)
    connection_specs_raw = params.get("connection_specs", DEFAULT_CONNECTION_SPECS)
    seed = params.get("seed", DEFAULT_SEED)
    loading_level = params.get("loading_level", DEFAULT_LOADING_LEVEL)
    ref_sys_id = params.get("ref_sys_id", DEFAULT_REF_SYS_ID)

    try:
        # Parse connection specs to tuple-keyed dict
        connection_specs = _parse_connection_specs(connection_specs_raw)

        # Generate input parameters using InputConfigurator
        logger.info(f"Configuring grid: {len(level_specs)} levels, seed={seed}")
        configurator = InputConfigurator(seed=seed)
        config_params = configurator.create_params(level_specs, connection_specs)

        # Serialize configuration (convert numpy arrays to lists for JSON)
        config_output = {
            "seed": seed,
            "loading_level": loading_level,
            "ref_sys_id": ref_sys_id,
            "level_specs": level_specs,
            "connection_specs": {
                str(k): {
                    "type": str(v.get("type", "")),
                    "c": float(v.get("c", 0.0)),
                    "gamma": float(v.get("gamma", 0.0)),
                }
                for k, v in connection_specs.items()
            },
            "degrees_by_level": [
                arr.tolist() if hasattr(arr, "tolist") else list(arr)
                for arr in config_params["degrees_by_level"]
            ],
            "diameters_by_level": [
                int(d) if hasattr(d, "item") else d
                for d in config_params["diameters_by_level"]
            ],
            "transformer_degrees": {
                str(k): (v.tolist() if hasattr(v, "tolist") else list(v))
                for k, v in config_params["transformer_degrees"].items()
            },
        }

        # The configuration is an intermediate step, consumed in this same
        # process by the synthesis that follows it. It is deliberately not
        # stored as a task artifact: the artifact a caller receives is the
        # synthesized grid, and publishing the config under the same task id
        # would overwrite it.
        config_json = json.dumps(config_output, default=_json_default)

        logger.info(f"Grid configuration complete: {len(level_specs)} levels")

        # Handed back inline: the two steps run in the same process, so the
        # config stays where its only consumer already is.
        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="inline",
                uri=base64.b64encode(config_json.encode("utf-8")).decode("ascii"),
                format="json",
            ),
        )

    except Exception as e:
        logger.exception("ConfigureGrid failed")
        return ExecuteResponse(status="failed", error=str(e))


# =============================================================================
# SynthesizeGrid Handler
# =============================================================================


def _synthesize_grid(request: ExecuteRequest) -> dict:
    """Generate a synthetic power grid using configuration from ConfigureGrid.

    Input:
        inputs[0]: DataReference to the configuration from ConfigureGrid,
        either inline JSON or an HTTP(S) URL returning it.

    Runs the full generation pipeline:
        1. Generate base topology with PowerGridGenerator
        2. Allocate bus types
        3. Allocate capacity
        4. Allocate loads
        5. Dispatch generation
        6. Allocate transmission lines

    Returns:
        The synthesized grid as a JSON-serializable dict.
    """
    if not request.inputs:
        raise ValueError("No input configuration provided")

    input_ref = request.inputs[0]

    try:
        protocol = input_ref.get("protocol", "")
        if protocol in ("http", "https"):
            config_json = fetch_http_data(input_ref["uri"])
            config = json.loads(config_json)
        elif protocol == "inline":
            config = _decode_inline_input(input_ref)
        else:
            raise ValueError((
                    f"Unsupported protocol: {protocol}. "
                    "Expected 'http', 'https', or 'inline'."),
            )

        seed = config.get("seed", DEFAULT_SEED)
        loading_level = config.get("loading_level", DEFAULT_LOADING_LEVEL)
        ref_sys_id = config.get("ref_sys_id", DEFAULT_REF_SYS_ID)
        degrees_by_level = config["degrees_by_level"]
        diameters_by_level = config["diameters_by_level"]

        # Reconstruct transformer_degrees with tuple keys
        transformer_degrees = {}
        for k, v in config["transformer_degrees"].items():
            # Keys are stored as "(0, 1)" strings in JSON
            nums = k.strip("()").split(",")
            key = (int(nums[0].strip()), int(nums[1].strip()))
            transformer_degrees[key] = v

        # 1. Generate base topology
        logger.info(f"Generating grid topology: seed={seed}")
        gen = PowerGridGenerator(seed=seed)
        grid = gen.generate_grid(
            degrees_by_level=degrees_by_level,
            diameters_by_level=diameters_by_level,
            transformer_degrees=transformer_degrees,
            keep_lcc=True,
        )
        logger.info(
            f"Topology: {grid.number_of_nodes()} nodes, {grid.number_of_edges()} edges"
        )

        # 2. Apply physics pipeline
        logger.info("Applying bus type allocation...")
        BusTypeAllocator(grid).allocate(max_iter=20)

        logger.info(f"Applying capacity allocation (ref_sys_id={ref_sys_id})...")
        CapacityAllocator(grid, ref_sys_id=ref_sys_id).allocate()

        logger.info(f"Applying load allocation (loading_level={loading_level})...")
        LoadAllocator(grid, ref_sys_id=ref_sys_id).allocate(loading_level=loading_level)

        logger.info("Dispatching generation...")
        GenerationDispatcher(grid, ref_sys_id=ref_sys_id).dispatch()

        logger.info("Allocating transmission lines...")
        TransmissionLineAllocator(grid, ref_sys_id=ref_sys_id).allocate()

        # 3. Serialize the enriched grid
        graph_data = json_graph.node_link_data(grid)

        # Convert any numpy types for JSON serialization
        output = {
            "status": "success",
            "nodes": grid.number_of_nodes(),
            "edges": grid.number_of_edges(),
            "seed": seed,
            "loading_level": loading_level,
            "ref_sys_id": ref_sys_id,
            "benchmark_env_name": _grid2op_env_name(int(ref_sys_id)),
            "graph_data": graph_data,
        }

        # pandapower is the interchange format a consumer of this grid actually
        # wants, so it travels in the stored artifact.
        logger.info("Converting synthesized grid to pandapower network...")
        output["pandapower"] = json.loads(_grid_to_pandapower_json(graph_data))

        logger.info(
            f"Grid synthesis complete: {grid.number_of_nodes()} nodes, "
            f"{grid.number_of_edges()} edges"
        )
        return output

    except Exception as e:
        logger.exception("Grid synthesis failed")
        raise


def execute_ConfigureAndSynthesize(request: ExecuteRequest) -> ExecuteResponse:
    """Start a grid synthesis and return immediately with its task id.

    This is the operation the exported package declares, because the two
    logical steps are backed by one physical service and a workflow designer
    cannot reliably model them as separate nodes.

    Synthesis is minutes of work, so it runs on a background thread rather than
    holding the orchestrator's request open (FR-24). Callers poll
    ``/control/status/{task_id}`` and read ``/control/output/{task_id}``, which
    returns an HTTP URL to the stored grid, fetchable by whoever submitted the
    workflow (FR-25).

    Args:
        request: Orchestrator execute request carrying the synthesis parameters.

    Returns:
        An ExecuteResponse with status ``running``, or ``failed`` when the
        configuration is rejected or the service is already at capacity.
    """
    # Configuration is cheap and its failures are the caller's mistakes, so it
    # runs synchronously: a bad request is rejected now rather than becoming a
    # background task that fails a minute later.
    configure_response = execute_ConfigureGrid(request)
    if configure_response.status != "complete" or configure_response.output is None:
        return ExecuteResponse(
            status="failed",
            task_id=request.task_id,
            error=(
                configure_response.error
                or "ConfigureGrid failed during ConfigureAndSynthesize"
            ),
        )

    synthesis_request = ExecuteRequest(
        method="SynthesizeGrid",
        workflow_id=request.workflow_id,
        task_id=request.task_id,
        inputs=[
            {
                "protocol": configure_response.output.protocol,
                "uri": configure_response.output.uri,
                "format": configure_response.output.format,
            }
        ],
    )

    def synthesize_grid(report_progress) -> dict:
        """Generate the grid, reporting coarse progress as it goes."""
        report_progress(20)
        synthesized_grid = _synthesize_grid(synthesis_request)
        report_progress(90)
        return synthesized_grid

    return get_job_runner().submit(request, synthesize_grid, data_format=GRID_DATA_FORMAT)


def _json_default(obj):
    """JSON serializer for numpy types."""
    import numpy as np

    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


# =============================================================================
# Handler exports
# =============================================================================

# Only ConfigureAndSynthesize is exported to the portal: the two steps are
# backed by one service, and the export's operations allowlist names this one.
synth_handlers = {
    "ConfigureAndSynthesize": execute_ConfigureAndSynthesize,
}
