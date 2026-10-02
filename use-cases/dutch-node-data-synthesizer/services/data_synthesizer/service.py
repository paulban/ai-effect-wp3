"""Service implementation - add your methods here.

Each method should be named execute_<MethodName> where MethodName
matches the operation name in the blueprint.

For long-running operations, use task_manager to track progress.
"""

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")

from networkx.readwrite import json_graph

from powergrid_synth.transmission.generator import PowerGridGenerator
from powergrid_synth.transmission.input_configurator import InputConfigurator
from powergrid_synth.transmission.bus_type_allocator import BusTypeAllocator
from powergrid_synth.transmission.capacity_allocator import CapacityAllocator
from powergrid_synth.transmission.load_allocator import LoadAllocator
from powergrid_synth.transmission.generation_dispatcher import GenerationDispatcher
from powergrid_synth.transmission.transmission import TransmissionLineAllocator

from handler import (
    DataReference,
    ExecuteRequest,
    ExecuteResponse,
    TaskManager,
    run,
    run_in_background,
    task_manager,
)

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
DEFAULT_GRID2OP_ENV_NAME = "synthetic-grid-v0"

_DEFAULT_VOLTAGE_BY_LEVEL: dict[int, float] = {
    0: 220.0,
    1: 110.0,
    2: 20.0,
    3: 10.0,
}

def execute_QuickProcess(request: ExecuteRequest) -> ExecuteResponse:
    """Example: Quick operation that completes immediately.

    For fast operations, return complete status directly.
    """
    output_uri = f"s3://bucket/output/{request.task_id}.json"

    return ExecuteResponse(
        status="complete",
        output=DataReference(
            protocol="s3",
            uri=output_uri,
            format="json",
        ),
    )

def _decode_inline_input(input_ref: dict) -> dict:
    """Decode inline JSON input to dict."""
    if input_ref.get("protocol") == "inline":
        try:
            return json.loads(base64.b64decode(input_ref.get("uri", "")).decode())
        # //TODO Define Exception type
        except Exception:
            # //TODO Define clear return error
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

# //TODO Check if this method can become restricted since we don't need to call
#    it directly but it's only called by execute_ConfigureAndSynthesize()
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

        grpc_host = os.environ.get("GRPC_HOST", "synthetic-data")
        grpc_port = os.environ.get("GRPC_PORT", "50051")

        # Handed back inline rather than as a reference to this service's own
        # gRPC endpoint. The two steps run in the same process, so the previous
        # reference made the handoff a network round trip to itself — and broke
        # outright once the unused gRPC server was removed. Inline keeps the
        # config where its only consumer already is.
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


def execute_ConfigureAndSynthesize(request: ExecuteRequest) -> ExecuteResponse:
    """Example: Long-running operation with progress tracking.

    For slow operations, register task and process in background.
    Uses orchestrator's task_id for tracking.
    """
    task_manager.register_task(request.task_id, request)
    run_in_background(request.task_id, _synthesize_grid, request)

    return ExecuteResponse(
        status="running",
        task_id=request.task_id,
    )

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

def _grid_to_pandapower_json(graph_data: dict[str, Any]) -> str:
    """Convert a node-link graph dict (powergrid_synth output) to a pandapower
    network and return it as a JSON string via pp.to_json().

    This is the authoritative networkx → pandapower conversion.  The result is
    stored verbatim in GridData.pandapower_json so the benchmark service can call
    pp.from_json() directly without any lossy intermediate proto fields.
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

def _grid2op_env_name(_: int) -> str:
    """Return the fixed synthesized Grid2Op environment name."""
    return DEFAULT_GRID2OP_ENV_NAME

def _synthesize_grid(
    task_id: str,
    request: ExecuteRequest,
    manager: TaskManager,
    ) -> None:
    """Background worker for long-running task.

    Args:
        task_id: Task ID for progress updates.
        request: Original request with inputs/parameters.
        manager: TaskManager for updating progress.
    """

    progress = int(0)
    manager.update_progress(task_id, progress)

    try:
        if not request.inputs:
            raise ValueError("No input configuration provided")
        else:
            input_ref = request.inputs[0]

        protocol = input_ref.get("protocol", "")
        if protocol in ("http", "https"):
            config_json = _fetch_http_data(input_ref["uri"])
            config = json.loads(config_json)
        elif protocol == "inline":
            config = _decode_inline_input(input_ref)
        else:
            raise ValueError((
                f"Unsupported protocol: {protocol}. "
                "Expected 'grpc', 'http', 'https', or 'inline'."),
            )

        progress=+int(10)
        manager.update_progress(task_id, progress)

        seed = config.get("seed", DEFAULT_SEED)
        loading_level = config.get("loading_level", DEFAULT_LOADING_LEVEL)
        ref_sys_id = config.get("ref_sys_id", DEFAULT_REF_SYS_ID)
        degrees_by_level = config["degrees_by_level"]
        diameters_by_level = config["diameters_by_level"]

        progress = +int(10)

        # Reconstruct transformer_degrees with tuple keys
        transformer_degrees = {}
        for k, v in config["transformer_degrees"].items():
            # Keys are stored as "(0, 1)" strings in JSON
            nums = k.strip("()").split(",")
            key = (int(nums[0].strip()), int(nums[1].strip()))
            transformer_degrees[key] = v

        progress = +int(10)

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

        progress = +int(10)

        # 2. Apply physics pipeline
        logger.info("Applying bus type allocation...")
        BusTypeAllocator(grid).allocate(max_iter=20)

        progress = +int(10)

        logger.info(
            f"Applying capacity allocation (ref_sys_id={ref_sys_id})..."
            )
        CapacityAllocator(grid, ref_sys_id=ref_sys_id).allocate()

        progress = +int(10)

        logger.info(
            f"Applying load allocation (loading_level={loading_level})..."
            )
        LoadAllocator(grid, ref_sys_id=ref_sys_id).allocate(
            loading_level=loading_level
            )

        progress = +int(10)

        logger.info("Dispatching generation...")
        GenerationDispatcher(grid, ref_sys_id=ref_sys_id).dispatch()

        progress = +int(10)

        logger.info("Allocating transmission lines...")
        TransmissionLineAllocator(grid, ref_sys_id=ref_sys_id).allocate()

        progress = +int(10)

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

        progress = +int(10)

        # pandapower is the interchange format a consumer of this grid actually
        # wants, so it travels in the stored artifact rather than only inside a
        # protobuf message that nothing resolves.
        logger.info("Converting synthesized grid to pandapower network...")
        output["pandapower"] = json.loads(_grid_to_pandapower_json(graph_data))

        logger.info(
            f"Grid synthesis complete: {grid.number_of_nodes()} nodes, "
            f"{grid.number_of_edges()} edges"
        )

        # Complete with output
        manager.complete_task(
            task_id,
            {
                "protocol": protocol,
                "uri": "folder path",
                # "uri": f"s3://bucket/output/{task_id}.json",
                "format": "json",
            },
        )

    except Exception as e:
        manager.fail_task(task_id, str(e))


if __name__ == "__main__":
    import sys
    run(sys.modules[__name__])
