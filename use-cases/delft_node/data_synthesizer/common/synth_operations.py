"""Synthetic power grid operation handlers for AI-Effect orchestrator.

This module provides operation handlers for the Chung-Lu-Chain power grid synthesizer.
Control execution uses AI-Effect HTTP control endpoints, while inter-node data exchange
uses the canonical protobuf/gRPC data plane.

Pipeline:
    ConfigureGrid -> SynthesizeGrid

Handlers:
        - ConfigureGrid: Accept synthesis parameters and publish a
            delft.data_synthesizer.GridSynthesisConfig artifact via GetGridConfig.
        - SynthesizeGrid: Consume GridSynthesisConfig, generate the grid, and publish
            delft.data_synthesizer.GridData via GetGridData.

Usage:
    from common import synth_handlers, run

    if __name__ == "__main__":
        run(synth_handlers, "Synthetic Power Grid Service")
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from concurrent import futures
from typing import Any

import grpc
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

from .control_interface import (
    DataReference,
    ExecuteRequest,
    ExecuteResponse,
    get_data_url,
)
from .proto_runtime import ensure_generated
from .task_manager import get_task_manager

logger = logging.getLogger(__name__)

ensure_generated("data_synthesizer.proto")
import data_synthesizer_pb2  # type: ignore  # noqa: E402
import data_synthesizer_pb2_grpc  # type: ignore  # noqa: E402

_cache_lock = threading.Lock()
_cached_config_response: data_synthesizer_pb2.GetGridConfigResponse | None = None
_cached_grid_response: data_synthesizer_pb2.GetGridDataResponse | None = None

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

_LOADING_LEVEL_TO_PROTO = {
    "L": data_synthesizer_pb2.LOADING_LEVEL_LOW,
    "M": data_synthesizer_pb2.LOADING_LEVEL_MEDIUM,
    "H": data_synthesizer_pb2.LOADING_LEVEL_HIGH,
}

_LOADING_LEVEL_FROM_PROTO = {
    data_synthesizer_pb2.LOADING_LEVEL_LOW: "L",
    data_synthesizer_pb2.LOADING_LEVEL_MEDIUM: "M",
    data_synthesizer_pb2.LOADING_LEVEL_HIGH: "H",
}


class DataSynthesizerServicer(data_synthesizer_pb2_grpc.DataSynthesizerServiceServicer):
    """gRPC servicer exposing synthesized config and grid artifacts."""

    def GetGridConfig(self, request, context):
        with _cache_lock:
            if _cached_config_response is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details("No grid configuration available")
                return data_synthesizer_pb2.GetGridConfigResponse(
                    success=False,
                    message="No grid configuration available",
                )
            return _cached_config_response

    def GetGridData(self, request, context):
        with _cache_lock:
            if _cached_grid_response is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details("No synthesized grid available")
                return data_synthesizer_pb2.GetGridDataResponse(
                    success=False,
                    message="No synthesized grid available",
                )
            return _cached_grid_response

    def GetSynthesizedGrid(self, request, context):
        return self.GetGridData(request, context)


def start_grpc_server():
    """Start the synthesizer gRPC data plane server in background."""
    grpc_port = os.environ.get("GRPC_PORT", "50051")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    data_synthesizer_pb2_grpc.add_DataSynthesizerServiceServicer_to_server(
        DataSynthesizerServicer(), server
    )
    server.add_insecure_port(f"[::]:{grpc_port}")
    server.start()
    logger.info(f"Data synthesizer gRPC server started on port {grpc_port}")
    return server


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


def _fetch_grid_config_from_upstream(
    grpc_uri: str,
) -> data_synthesizer_pb2.GetGridConfigResponse:
    """Fetch synthesized configuration using gRPC from upstream service."""
    logger.info(f"Fetching grid config via gRPC from {grpc_uri}")
    channel = grpc.insecure_channel(grpc_uri)
    stub = data_synthesizer_pb2_grpc.DataSynthesizerServiceStub(channel)
    try:
        return stub.GetGridConfig(data_synthesizer_pb2.GetGridConfigRequest())
    finally:
        channel.close()


def _coerce_int_list(values: Any) -> list[int]:
    if values is None:
        return []
    if isinstance(values, (list, tuple)):
        return [int(v) for v in values]
    return [int(values)]


def _parse_connection_key(key: Any) -> tuple[int, int]:
    if isinstance(key, str) and key.startswith("("):
        nums = key.strip("()").split(",")
        return int(nums[0].strip()), int(nums[1].strip())
    if isinstance(key, (list, tuple)) and len(key) == 2:
        return int(key[0]), int(key[1])
    parts = str(key).replace("_", "-").split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid connection key: {key}")
    return int(parts[0]), int(parts[1])


def _loading_level_to_proto(value: str) -> int:
    return _LOADING_LEVEL_TO_PROTO.get(
        str(value).upper(), data_synthesizer_pb2.LOADING_LEVEL_MEDIUM
    )


def _loading_level_from_proto(value: int) -> str:
    return _LOADING_LEVEL_FROM_PROTO.get(value, DEFAULT_LOADING_LEVEL)


def _grid_config_to_proto(
    config_output: dict[str, Any],
) -> data_synthesizer_pb2.GridSynthesisConfig:
    """Convert generated synthesis config payload to protobuf GridSynthesisConfig."""
    config_msg = data_synthesizer_pb2.GridSynthesisConfig(
        seed=int(config_output.get("seed", DEFAULT_SEED)),
        loading_level=_loading_level_to_proto(
            str(config_output.get("loading_level", DEFAULT_LOADING_LEVEL))
        ),
        ref_sys_id=int(config_output.get("ref_sys_id", DEFAULT_REF_SYS_ID)),
    )

    for key, value in config_output.get("connection_specs", {}).items():
        from_level, to_level = _parse_connection_key(key)
        config_msg.connections.add(
            from_level=from_level,
            to_level=to_level,
            type=str(value.get("type", "")),
            c=float(value.get("c", 0.0)),
            gamma=float(value.get("gamma", 0.0)),
        )

    for degree_values in config_output.get("degrees_by_level", []):
        config_msg.degrees_by_level.add(values=_coerce_int_list(degree_values))

    for d in config_output.get("diameters_by_level", []):
        config_msg.diameters_by_level.append(int(d))

    for key, values in config_output.get("transformer_degrees", {}).items():
        from_level, to_level = _parse_connection_key(key)
        if not isinstance(values, (list, tuple)) or len(values) != 2:
            raise ValueError(
                f"transformer_degrees[{key}] must contain exactly two degree vectors"
            )
        source_degrees = _coerce_int_list(values[0])
        target_degrees = _coerce_int_list(values[1])
        config_msg.transformer_degrees.add(
            from_level=from_level,
            to_level=to_level,
            source_degrees=source_degrees,
            target_degrees=target_degrees,
        )

    return config_msg


def _proto_grid_config_to_dict(
    config_msg: data_synthesizer_pb2.GridSynthesisConfig,
) -> dict[str, Any]:
    """Convert protobuf GridSynthesisConfig to dictionary used by synthesis logic."""
    transformer_degrees: dict[str, Any] = {
        str((pair.from_level, pair.to_level)): [
            list(pair.source_degrees),
            list(pair.target_degrees),
        ]
        for pair in config_msg.transformer_degrees
    }

    connection_specs: dict[str, dict[str, Any]] = {}
    for conn in config_msg.connections:
        connection_specs[str((conn.from_level, conn.to_level))] = {
            "type": str(conn.type),
            "c": float(conn.c),
            "gamma": float(conn.gamma),
        }

    return {
        "seed": int(config_msg.seed),
        "loading_level": _loading_level_from_proto(config_msg.loading_level),
        "ref_sys_id": int(config_msg.ref_sys_id),
        "connection_specs": connection_specs,
        "degrees_by_level": [
            [int(v) for v in seq.values] for seq in config_msg.degrees_by_level
        ],
        "diameters_by_level": list(config_msg.diameters_by_level),
        "transformer_degrees": transformer_degrees,
    }


def _grid_data_to_proto(
    output: dict[str, Any],
    config_output: dict[str, Any],
) -> data_synthesizer_pb2.GridData:
    """Map node-link JSON payload to protobuf GridData."""
    graph_data = output.get("graph_data", {})
    topology = data_synthesizer_pb2.GridTopology(
        directed=bool(graph_data.get("directed", False)),
        multigraph=bool(graph_data.get("multigraph", False)),
    )

    for node in graph_data.get("nodes", []):
        node_msg = topology.nodes.add(
            id=str(node.get("id", "")),
            bus_type=str(node.get("bus_type", "")),
            voltage=float(node.get("v_nom", node.get("voltage", 0.0)) or 0.0),
            load_p=float(node.get("p_load", 0.0) or 0.0),
            load_q=float(node.get("q_load", 0.0) or 0.0),
            gen_p=float(node.get("p_set", 0.0) or 0.0),
            gen_q=float(node.get("q_set", 0.0) or 0.0),
        )
        for key, value in node.items():
            if key not in {
                "id",
                "bus_type",
                "v_nom",
                "voltage",
                "p_load",
                "q_load",
                "p_set",
                "q_set",
            }:
                node_msg.metadata[str(key)] = str(value)

    for edge in graph_data.get("links", []):
        edge_msg = topology.edges.add(
            source=str(edge.get("source", "")),
            target=str(edge.get("target", "")),
            resistance=float(edge.get("r", 0.0) or 0.0),
            reactance=float(edge.get("x", 0.0) or 0.0),
            susceptance=float(edge.get("b", 0.0) or 0.0),
            thermal_limit=float(
                edge.get("snom", edge.get("thermal_limit", 0.0)) or 0.0
            ),
        )
        for key, value in edge.items():
            if key not in {"source", "target", "r", "x", "b", "snom", "thermal_limit"}:
                edge_msg.metadata[str(key)] = str(value)

    grid_data = data_synthesizer_pb2.GridData(
        grid_id="delft-synthesized-grid",
        topology=topology,
        seed=int(output.get("seed", DEFAULT_SEED)),
        loading_level=str(output.get("loading_level", DEFAULT_LOADING_LEVEL)),
        ref_sys_id=int(output.get("ref_sys_id", DEFAULT_REF_SYS_ID)),
        source_config=_grid_config_to_proto(config_output),
    )

    grid_data.metadata["status"] = str(output.get("status", "success"))
    grid_data.metadata["nodes"] = str(output.get("nodes", 0))
    grid_data.metadata["edges"] = str(output.get("edges", 0))
    return grid_data


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
        DataReference(protocol="grpc", format="GetGridConfig") with a canonical
        GridSynthesisConfig payload served by this node.
    """
    global _cached_config_response

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

        config_json = json.dumps(config_output, default=_json_default)

        # Store for HTTP serving
        get_task_manager().store_data(request.task_id, config_json, "json")

        with _cache_lock:
            _cached_config_response = data_synthesizer_pb2.GetGridConfigResponse(
                success=True,
                message="Grid configuration generated",
                config=_grid_config_to_proto(config_output),
            )

        logger.info(f"Grid configuration complete: {len(level_specs)} levels")

        grpc_host = os.environ.get("GRPC_HOST", "synthetic-data")
        grpc_port = os.environ.get("GRPC_PORT", "50051")

        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="grpc",
                uri=f"{grpc_host}:{grpc_port}",
                format="GetGridConfig",
            ),
        )

    except Exception as e:
        logger.exception("ConfigureGrid failed")
        return ExecuteResponse(status="failed", error=str(e))


# =============================================================================
# SynthesizeGrid Handler
# =============================================================================


def execute_SynthesizeGrid(request: ExecuteRequest) -> ExecuteResponse:
    """Generate a synthetic power grid using configuration from ConfigureGrid.

    Input:
        inputs[0]: DataReference from ConfigureGrid. Canonical path is
        protocol="grpc" and format="GetGridConfig".

    Runs the full generation pipeline:
        1. Generate base topology with PowerGridGenerator
        2. Allocate bus types
        3. Allocate capacity
        4. Allocate loads
        5. Dispatch generation
        6. Allocate transmission lines

    Returns:
        DataReference(protocol="grpc", format="GetGridData") with canonical
        GridData payload.
    """
    global _cached_grid_response

    if not request.inputs:
        return ExecuteResponse(status="failed", error="No input configuration provided")

    input_ref = request.inputs[0]

    try:
        # Fetch configuration from upstream
        protocol = input_ref.get("protocol", "")
        if protocol == "grpc":
            upstream_uri = input_ref.get("uri", "")
            if not upstream_uri:
                return ExecuteResponse(
                    status="failed",
                    error="Missing grpc uri for SynthesizeGrid input",
                )
            config_response = _fetch_grid_config_from_upstream(upstream_uri)
            if not config_response.success:
                return ExecuteResponse(
                    status="failed",
                    error=f"Upstream gRPC config fetch failed: {config_response.message}",
                )
            config = _proto_grid_config_to_dict(config_response.config)
        elif protocol in ("http", "https"):
            config_json = fetch_http_data(input_ref["uri"])
            config = json.loads(config_json)
        elif protocol == "inline":
            config = _decode_inline_input(input_ref)
        else:
            return ExecuteResponse(
                status="failed",
                error=(
                    f"Unsupported protocol: {protocol}. "
                    "Expected 'grpc', 'http', 'https', or 'inline'."
                ),
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

        with _cache_lock:
            _cached_grid_response = data_synthesizer_pb2.GetGridDataResponse(
                success=True,
                message="Synthesized grid available",
                grid_data=_grid_data_to_proto(output, config),
            )

        logger.info(
            f"Grid synthesis complete: {grid.number_of_nodes()} nodes, "
            f"{grid.number_of_edges()} edges"
        )

        grpc_host = os.environ.get("GRPC_HOST", "synthetic-data")
        grpc_port = os.environ.get("GRPC_PORT", "50051")

        return ExecuteResponse(
            status="complete",
            output=DataReference(
                protocol="grpc",
                uri=f"{grpc_host}:{grpc_port}",
                format="GetGridData",
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
