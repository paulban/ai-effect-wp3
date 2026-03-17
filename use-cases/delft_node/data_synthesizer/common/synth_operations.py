"""Synthetic power grid operation handlers for AI-Effect orchestrator.

This module provides operation handlers for the Chung-Lu-Chain power grid synthesizer.
Data is exchanged via HTTP URL references following the AI-Effect protocol.

Pipeline:
    ConfigureGrid -> SynthesizeGrid

Handlers:
    - ConfigureGrid: Accept grid configuration parameters, generate InputConfigurator
      params, and serve them via HTTP URL.
    - SynthesizeGrid: Take configuration params, generate grid topology, apply full
      physics pipeline, and output the enriched grid data as JSON.

Usage:
    from common import synth_handlers, run

    if __name__ == "__main__":
        run(synth_handlers, "Synthetic Power Grid Service")
"""

from __future__ import annotations

import base64
import json
import logging

import matplotlib

matplotlib.use("Agg")

import networkx as nx
from networkx.readwrite import json_graph

from powergrid_synth.generator import PowerGridGenerator
from powergrid_synth.input_configurator import InputConfigurator
from powergrid_synth.bus_type_allocator import BusTypeAllocator
from powergrid_synth.capacity_allocator import CapacityAllocator
from powergrid_synth.load_allocator import LoadAllocator
from powergrid_synth.generation_dispatcher import GenerationDispatcher
from powergrid_synth.transmission import TransmissionLineAllocator

from .control_interface import (
    DataReference,
    ExecuteRequest,
    ExecuteResponse,
    get_data_url,
)
from .task_manager import get_task_manager

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
        DataReference with HTTP URL to JSON configuration.
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

        config_json = json.dumps(config_output, default=_json_default)

        # Store for HTTP serving
        get_task_manager().store_data(request.task_id, config_json, "json")

        logger.info(f"Grid configuration complete: {len(level_specs)} levels")

        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="http",
                uri=get_data_url(request.task_id),
                format="json",
            ),
        )

    except Exception as e:
        logger.error(f"ConfigureGrid failed: {e}")
        return ExecuteResponse(status="failed", error=str(e))


# =============================================================================
# SynthesizeGrid Handler
# =============================================================================


def execute_SynthesizeGrid(request: ExecuteRequest) -> ExecuteResponse:
    """Generate a synthetic power grid using configuration from ConfigureGrid.

    Input:
        inputs[0]: HTTP URL reference to JSON configuration (from ConfigureGrid)

    Runs the full generation pipeline:
        1. Generate base topology with PowerGridGenerator
        2. Allocate bus types
        3. Allocate capacity
        4. Allocate loads
        5. Dispatch generation
        6. Allocate transmission lines

    Returns:
        DataReference with HTTP URL to JSON graph data (node-link format).
    """
    if not request.inputs:
        return ExecuteResponse(status="failed", error="No input configuration provided")

    input_ref = request.inputs[0]

    try:
        # Fetch configuration from upstream
        protocol = input_ref.get("protocol", "")
        if protocol in ("http", "https"):
            config_json = fetch_http_data(input_ref["uri"])
            config = json.loads(config_json)
        elif protocol == "inline":
            config = _decode_inline_input(input_ref)
        else:
            return ExecuteResponse(
                status="failed",
                error=f"Unsupported protocol: {protocol}. Expected 'http' or 'inline'.",
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
            "graph_data": graph_data,
        }

        output_json = json.dumps(output, default=_json_default)

        # Store for HTTP serving
        get_task_manager().store_data(request.task_id, output_json, "json")

        logger.info(
            f"Grid synthesis complete: {grid.number_of_nodes()} nodes, "
            f"{grid.number_of_edges()} edges"
        )

        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="http",
                uri=get_data_url(request.task_id),
                format="json",
            ),
        )

    except Exception as e:
        logger.error(f"SynthesizeGrid failed: {e}")
        return ExecuteResponse(status="failed", error=str(e))


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

synth_handlers = {
    "ConfigureGrid": execute_ConfigureGrid,
    "SynthesizeGrid": execute_SynthesizeGrid,
}
