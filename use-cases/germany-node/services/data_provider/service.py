"""Data provider service for Germany node VILLASnode pipeline.

Copies converted pandapower timeseries data to shared volume for processing.
"""

import glob
import json
import logging
import os
import shutil

from common import DataReference, ExecuteResponse, run_sequential

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SHARED_DIR = os.environ.get("SHARED_DIR", "/shared")
GRID_FILE = os.environ.get(
    "GRID_FILE", "ppnet_DW00.1.382280-VE_202412.json"
)


def execute_ProvideData(request) -> ExecuteResponse:
    """Copy input data to shared volume for downstream processing."""
    workflow_id = request.workflow_id
    work_dir = os.path.join(SHARED_DIR, workflow_id)
    loads_dir = os.path.join(work_dir, "loads")
    sgens_dir = os.path.join(work_dir, "sgens")

    os.makedirs(loads_dir, exist_ok=True)
    os.makedirs(sgens_dir, exist_ok=True)

    # Copy converted load files (sorted for deterministic ordering)
    load_files = sorted(glob.glob(os.path.join(DATA_DIR, "loads", "*_converted.csv")))
    for f in load_files:
        shutil.copy2(f, loads_dir)
    logger.info(f"Copied {len(load_files)} load files to {loads_dir}")

    # Copy converted sgen files (sorted for deterministic ordering)
    sgen_files = sorted(glob.glob(os.path.join(DATA_DIR, "sgens", "*_converted.csv")))
    for f in sgen_files:
        shutil.copy2(f, sgens_dir)
    logger.info(f"Copied {len(sgen_files)} sgen files to {sgens_dir}")

    # Copy grid model JSON
    grid_src = os.path.join(DATA_DIR, GRID_FILE)
    grid_dst = os.path.join(work_dir, GRID_FILE)
    shutil.copy2(grid_src, grid_dst)
    logger.info(f"Copied grid file to {grid_dst}")

    # Write manifest for downstream services
    # trigger_load/trigger_sgen: the first file VILLASnode should open to
    # kick off signal processing. The create_chronics hook globs the full
    # directories independently, so these are just entry points.
    manifest = {
        "loads_dir": loads_dir,
        "sgens_dir": sgens_dir,
        "grid_file": grid_dst,
        "load_files": [os.path.basename(f) for f in load_files],
        "sgen_files": [os.path.basename(f) for f in sgen_files],
        "trigger_load": os.path.basename(load_files[0]),
        "trigger_sgen": os.path.basename(sgen_files[0]),
    }
    manifest_path = os.path.join(work_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    logger.info(f"Wrote manifest to {manifest_path}")

    return ExecuteResponse(
        status="complete",
        output=DataReference(protocol="file", uri=work_dir, format="json"),
    )


if __name__ == "__main__":
    run_sequential(__import__(__name__))
