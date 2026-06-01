"""Build (or rebuild) the AI4EU Design Studio submission package.

Assembles the canonical directory structure required by the AI4EU Design Studio
and zips it for upload:

    ai4eu_pipeline_package/
        blueprint.json
        dockerinfo.json
        microservice/
            data_synthesizer.proto
            benchmarking.proto

    ai4eu_pipeline_package.zip          ← upload this file
    

Sources are always read from the live workspace files so the package stays in
sync with code changes.  The output folder and zip are written to
  use-cases/dutch_node/
and git-ignored via the .gitignore in that directory (if present).

Usage (from repo root or from use-cases/dutch_node/):
    python test_pipelines/build_ai4eu_package.py
    python test_pipelines/build_ai4eu_package.py --out-dir /tmp/my_package
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path


# ── locate source files ───────────────────────────────────────────────────────

# This script lives in  dutch_node/test_pipelines/
_SCRIPT_DIR = Path(__file__).resolve().parent
_DUTCH_NODE = _SCRIPT_DIR.parent          # use-cases/dutch_node/

_PROTO_SOURCES: dict[str, Path] = {
    # proto files (always read from the canonical service directories)
    "microservice/data_synthesizer.proto": (
        _DUTCH_NODE / "data_synthesizer" / "proto" / "data_synthesizer.proto"
    ),
    "microservice/benchmarking.proto": (
        _DUTCH_NODE / "benchmarking" / "proto" / "benchmarking.proto"
    ),
}

_DEFAULT_OUT_DIR = _DUTCH_NODE / "ai4eu_pipeline_package"
_DEFAULT_ZIP     = _DUTCH_NODE / "ai4eu_pipeline_package.zip"


# ── core logic ────────────────────────────────────────────────────────────────

def _check_sources() -> list[str]:
    missing = []
    for dest, src in _PROTO_SOURCES.items():
        if not src.exists():
            missing.append(f"  {dest}  ←  {src}")
    return missing


def _build_package_blueprint() -> dict[str, object]:
    """Build the AI4EU pipeline blueprint matching the tested local flow."""
    return {
        "name": "Dutch Node Synth-Benchmark Pipeline",
        "pipeline_id": "dutch-node-ai4eu-2svc-001",
        "creation_date": str(date.today()),
        "type": "pipeline-topology/v2",
        "version": "1.0",
        "nodes": [
            {
                "container_name": "grid_synth_service",
                "proto_uri": "microservice/data_synthesizer.proto",
                "image": "paulban/aieffect-dutchnode-datasynth:latest",
                "node_type": "DataSource",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "ConfigureAndSynthesize",
                            "output_message_name": "GetGridDataResponse",
                        },
                        "connected_to": [
                            {
                                "container_name": "benchmark-runner",
                                "operation_signature": {
                                    "operation_name": "RunBenchmark"
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "container_name": "benchmark-runner",
                "proto_uri": "microservice/benchmarking.proto",
                "image": "paulban/aieffect-dutchnode-benchmarking:latest",
                "node_type": "MLModel",
                "operation_signature_list": [
                    {
                        "operation_signature": {
                            "operation_name": "RunBenchmark",
                            "input_message_name": "BenchmarkExecutionSpec",
                            "output_message_name": "GetBenchmarkResultResponse",
                            "input_message_stream": False,
                            "output_message_stream": False,
                        },
                        "connected_to": [],
                    }
                ],
            },
        ],
    }


def _build_package_dockerinfo() -> dict[str, object]:
    """Build dockerinfo matching the local ports used by the passing test."""
    return {
        "docker_info_list": [
            {
                "container_name": "grid_synth_service",
                "ip_address": "host.docker.internal",
                "port": "8003",
            },
            {
                "container_name": "benchmark-runner",
                "ip_address": "host.docker.internal",
                "port": "8004",
            },
        ]
    }


def build_package(out_dir: Path, zip_path: Path, *, overwrite: bool = True) -> None:
    """Write the package folder structure and the zip archive.

    Args:
        out_dir:   Destination folder (will be created/overwritten).
        zip_path:  Path of the output zip file.
        overwrite: If True, any existing out_dir is replaced.
    """
    missing = _check_sources()
    if missing:
        print("ERROR: The following source files are missing:", file=sys.stderr)
        for m in missing:
            print(m, file=sys.stderr)
        sys.exit(1)

    # -- assemble directory -------------------------------------------------
    if overwrite and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Writing package to:  {out_dir}")

    blueprint_path = out_dir / "blueprint.json"
    blueprint_path.write_text(
        json.dumps(_build_package_blueprint(), indent=2) + "\n",
        encoding="utf-8",
    )
    print("  wrote   blueprint.json                           →  blueprint.json")

    dockerinfo_path = out_dir / "dockerinfo.json"
    dockerinfo_path.write_text(
        json.dumps(_build_package_dockerinfo(), indent=2) + "\n",
        encoding="utf-8",
    )
    print("  wrote   dockerinfo.json                          →  dockerinfo.json")

    for dest_rel, src in _PROTO_SOURCES.items():
        dest = out_dir / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"  copied  {src.name:<40}  →  {dest_rel}")

    # -- create zip ---------------------------------------------------------
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dest_rel in ("blueprint.json", "dockerinfo.json", *_PROTO_SOURCES.keys()):
            full = out_dir / dest_rel
            # Zip entries use forward-slash paths, no leading component
            zf.write(full, arcname=dest_rel)

    size_kb = zip_path.stat().st_size / 1024
    print(f"\nZip archive:         {zip_path}  ({size_kb:.1f} KB)")
    print("\n✅  AI4EU package ready.\n")
    print("Upload  ai4eu_pipeline_package.zip  to the AI4EU Design Studio.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_DEFAULT_OUT_DIR,
        help=f"Output folder (default: {_DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--zip",
        type=Path,
        default=_DEFAULT_ZIP,
        help=f"Output zip path (default: {_DEFAULT_ZIP})",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Do not remove and recreate the output folder if it already exists",
    )
    args = parser.parse_args()

    build_package(args.out_dir, args.zip, overwrite=not args.no_overwrite)


if __name__ == "__main__":
    main()
