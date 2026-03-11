"""
End-to-end test: ConfigureGrid -> SynthesizeGrid
Run with: python test_pipeline.py

Assumes the container is running on http://localhost:8080
"""

import json
import base64
import httpx

BASE_URL = "http://localhost:8080"
TIMEOUT = 120.0  # synthesis can take a while


def main():
    client = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)

    # ── Health check ────────────────────────────────────────────────
    print("1. Health check...")
    resp = client.get("/health")
    resp.raise_for_status()
    print(f"   {resp.json()}\n")

    # ── Step 1: ConfigureGrid ───────────────────────────────────────
    print("2. ConfigureGrid...")

    # Inline input with small grid for quick testing
    config_input = {
        "level_specs": [
            {"n": 10, "avg_k": 2.5, "diam": 4, "dist_type": "dgln", "max_k": 8},
            {"n": 20, "avg_k": 2.0, "diam": 6, "dist_type": "dgln", "max_k": 6},
        ],
        "connection_specs": {"(0, 1)": {"type": "k-stars", "c": 0.174, "gamma": 4.15}},
        "seed": 42,
        "loading_level": "M",
        "ref_sys_id": 1,
    }

    inline_ref = {
        "protocol": "inline",
        "uri": base64.b64encode(json.dumps(config_input).encode()).decode(),
        "format": "json",
    }

    resp = client.post(
        "/control/execute",
        json={
            "method": "ConfigureGrid",
            "workflow_id": "test-workflow-001",
            "task_id": "test-configure-001",
            "inputs": [inline_ref],
        },
    )
    resp.raise_for_status()
    configure_result = resp.json()
    print(f"   Status: {configure_result['status']}")
    print(f"   Output: {json.dumps(configure_result.get('output'), indent=2)}\n")

    if configure_result["status"] != "complete":
        print(f"   ERROR: {configure_result.get('error')}")
        return

    # ── Step 2: SynthesizeGrid ──────────────────────────────────────
    print("3. SynthesizeGrid...")

    # Use the DataReference output from ConfigureGrid as input
    config_data_ref = configure_result["output"]

    resp = client.post(
        "/control/execute",
        json={
            "method": "SynthesizeGrid",
            "workflow_id": "test-workflow-001",
            "task_id": "test-synthesize-001",
            "inputs": [config_data_ref],
        },
    )
    resp.raise_for_status()
    synth_result = resp.json()
    print(f"   Status: {synth_result['status']}")

    if synth_result["status"] != "complete":
        print(f"   ERROR: {synth_result.get('error')}")
        return

    print(f"   Output: {json.dumps(synth_result.get('output'), indent=2)}\n")

    # ── Step 3: Fetch the actual grid data ──────────────────────────
    print("4. Fetching generated grid data...")
    data_ref = synth_result["output"]
    data_resp = client.get(data_ref["uri"])
    data_resp.raise_for_status()
    grid_data = data_resp.json()

    print(f"   Nodes: {grid_data['nodes']}")
    print(f"   Edges: {grid_data['edges']}")
    print(f"   Seed:  {grid_data['seed']}")
    print(f"   Loading level: {grid_data['loading_level']}")

    # Show a few node attributes
    graph = grid_data["graph_data"]
    print(
        f"\n   Graph format: node-link, {len(graph['nodes'])} nodes, {len(graph['links'])} links"
    )
    if graph["nodes"]:
        sample = graph["nodes"][0]
        print(f"   Sample node attributes: {list(sample.keys())}")

    # ── Step 4: Check task status ───────────────────────────────────
    print("\n5. Task status checks...")
    for tid in ["test-configure-001", "test-synthesize-001"]:
        resp = client.get(f"/control/status/{tid}")
        resp.raise_for_status()
        print(f"   {tid}: {resp.json()}")

    print("\n✅ Pipeline completed successfully!")


if __name__ == "__main__":
    main()
